"""消息调度器（阶段1任务1.3 / 1.4 / 1.5 / 1.14；阶段2任务2.8 ~ 2.12；阶段3任务3.10 / 3.11）。

职责：路由消息，决定消息去向。

阶段1只做写死回复；阶段2接入完整管线：
    收到消息 → 更新状态 → 状态决定回复策略 → 调API生成 → 结构化输出 → 延迟队列 → WebSocket推送
阶段3补充：记忆注入、已读不回、double_text 后续消息调度。

- 五、核心数据流 5.1
- send 服务端处理
- 六、与数据库的映射 / 七、与 WebSocket 推送的映射
-
"""
from __future__ import annotations

import json
import logging
import random
import re
from typing import Any, Optional

import api_pool
import custom_persona
import database as db
import memory as mem
import performance_layer
import personality as persona
import prompt_builder
import state_machine as sm
import vision
import web_search
from config import (
    DEFAULT_USER_ID,
    DOUBLE_TEXT_MAX_DELAY_SECONDS,
    DOUBLE_TEXT_MIN_DELAY_SECONDS,
    FIXED_REPLY_CONTENT,
    FIXED_REPLY_DELAY_SECONDS,
    FIXED_REPLY_ENABLED,
    IGNORE_BLOCK_BIAS,
    URGE_FORCE_REPLY_AFTER,
    URGE_MAX_ADVANCE_SECONDS,
    URGE_MIN_ADVANCE_SECONDS,
    USE_LLM,
)
from schemas import AIResponse, fallback_response, parse_ai_response

logger = logging.getLogger("dispatcher")

USER_DISPLAY = "用户"

# ---------------------------------------------------------------- 入口

async def handle_user_send(
    *,
    user_id: int,
    conversation_id: int,
    content: str,
    content_type: str,
    timestamp: str,
    client_msg_id: str,
) -> None:
    """处理一条用户 send 消息（WS-001 3.2 服务端处理 1~5 步）。"""
    # 3. 写入 messages 表，sender_type = "user"
    user_row = db.insert_message(
        conversation_id=conversation_id,
        sender_type="user",
        sender_id=user_id,
        content=content,
        timestamp=timestamp,
        client_msg_id=client_msg_id,
        content_type=content_type,
        state="sent",
    )
    logger.info(
        "收到用户消息：conversation_id=%s client_msg_id=%s msg_id=%s",
        conversation_id,
        client_msg_id,
        user_row["msg_id"] if user_row else "(重复)",
    )

    # 5. 更新 conversations.last_message_at（用户消息不计未读）
    db.touch_conversation(conversation_id, timestamp, unread_delta=0)

    conversation = db.get_conversation(conversation_id)
    if conversation is not None and conversation["type"] == "group":
        # 群聊消息分发给所有 AI（延迟导入避免循环依赖）
        import group_chat

        await group_chat.handle_group_message(
            user_id=user_id,
            conversation_id=conversation_id,
            user_text=content,
            content_type=content_type,
        )
        return

    if conversation is not None and conversation["type"] == "human":
        # 人类好友会话（用户 2026-09-27 第七轮）：不经 AI、不走延迟队列，
        # 直接投递给对方。对方不在线就落库，等他上线 resume 时补发。
        # 这里必须 return —— 否则会继续往下走 AI 管线，给人类好友自动生成回复。
        import human_chat

        await human_chat.deliver_human_message(
            conversation_id=conversation_id,
            sender_user_id=user_id,
            content=content,
            content_type=content_type,
            timestamp=timestamp,
            msg_id=str(user_row["msg_id"]) if user_row else "",
        )
        return

    # 4. 用户可能在催（「说话」「哑巴了吗」「赶紧回我」）：
    #    先把排队中那条很久以后才发的消息提前，再决定要不要重新生成。
    import urge as urge_mod

    urg = await urge_mod.handle_urge(
        user_id=user_id,
        conversation_id=conversation_id,
        user_text=content,
    )
    if urg is not None:
        # 三条分支都不该再走一次正常管线，否则用户催一句会收到两遍回复：
        #   advanced -> 那条「本来要等 3 分钟」的消息已在 1~3 秒后发出；
        #   rerun / forced -> 已经带「用户在催」重新生成了一条。
        return

    # 4b. 连发合并（2026-09-29 第十轮）：
    #
    merged_text = _merge_consecutive_user_texts(conversation_id, content)
    if merged_text is not None:
        logger.info(
            "连发合并：conversation=%s 合并后文本=%r",
            conversation_id,
            merged_text[:80],
        )

    # 5. 调用消息调度器（单聊）
    await schedule_single_reply(
        user_id=user_id,
        conversation_id=conversation_id,
        user_text=merged_text if merged_text is not None else content,
        content_type=content_type,
        is_urge=urg == "advanced",
    )

def _merge_consecutive_user_texts(conversation_id: int, latest: str) -> Optional[str]:
    """用户连发时，把「还没被回复的」几条合并成一段文本；不需要合并返回 None。

    判定「还没被回复」：这个会话还有排队中的 AI 回复任务（status pending/processing）。
    有就说明 AI 一条都还没发出来，用户这几条是连着说的，应该当成**一段话**看。

    为什么要连库里的历史一起拼、而不是只用最新那条：
    模型看到的是「用户：<合并后的一句话>」，如果只给最新那条，
    前一条的内容就彻底丢了 —— 用户会觉得「我说的话它没看见」。

    返回 None 表示不必合并（上一条已经回过、或没有历史可拼），
    调用方照旧用最新那条走正常流程。
    """
    if db.count_pending_tasks(conversation_id) <= 0:
        return None

    # 取最近的消息，从末尾往前收集连续的 user 消息。
    # 遇到**已经发出去**的 AI 消息就停 —— 那说明上一轮 AI 已经回过话了，
    # 用户现在说的是新一轮，不该跟上一条合并。
    rows = db.list_messages(conversation_id, limit=20)
    unsent = db.list_unsent_message_ids(conversation_id)
    texts: list[str] = []
    for row in reversed(rows):
        # 还没发出去的 AI 消息不算「回过话」，跳过它继续往前找用户消息。
        if row["sender_type"] == "ai" and int(row["id"]) in unsent:
            continue
        if row["sender_type"] != "user":
            break
        # display_parts：图片换成 [图片] 占位。
        # 这里必须过它 —— 否则「图片 + 紧接着发一句话」会被合并成
        # 「data:image/jpeg;base64,/9j/4AAQ...\n在吗」几万字符塞进模型，
        # 既烧 token 又让模型完全看不见那句真正的话（第十三轮）。
        for part in mem.display_parts(row):
            if part.strip():
                texts.append(part.strip())
    texts.reverse()
    if not texts:
        return None

    # texts 里最后一条就是刚写进库的 latest（handle_user_send 先落库再调这里）。
    if len(texts) == 1:
        return None

    # 撤销上一条触发的、还没发出去的回复，避免出现两串回复。
    cancelled = db.cancel_pending_replies(conversation_id)
    logger.info(
        "连发合并：撤销 %s 条排队中的回复，改为一并回复 %s 条用户消息",
        cancelled,
        len(texts),
    )
    # 用换行分隔：Prompt 的上下文层会显示成「用户：第一条\n第二条」，
    # 模型能看出这是连着说的两句，而不是一个长句子。
    return "\n".join(texts)

# ---------------------------------------------------------------- 单聊管线

def _resolve_ai_profile(conversation_id: int) -> Optional[Any]:
    """取会话中的 AI 档案。单聊时 participants 只有一个元素。"""
    participants = db.conversation_participants(conversation_id)
    if not participants:
        return None
    return db.get_ai_profile(participants[0])

async def schedule_single_reply(
    *,
    user_id: int,
    conversation_id: int,
    user_text: str,
    content_type: str = "text",
    proactive_topic: Optional[str] = None,
    is_proactive: bool = False,
    is_urge: bool = False,
    force_reply: bool = False,
) -> None:
    """单聊回复管线。阶段1（USE_LLM=False）走写死逻辑，阶段2起走完整 Prompt + API。

    :param is_urge: 用户在催（见 urge.py）。Prompt 里会明确告诉 AI 这件事，
        并把「本次必须立刻回」写进去 —— 用户催了还让他等三分钟是最气人的。
    :param force_reply: 连着催了太多次，这次不允许再选 ignore。
    """
    ai_profile = _resolve_ai_profile(conversation_id)
    if ai_profile is None:
        logger.warning("会话 %s 没有绑定 AI 档案，跳过回复", conversation_id)
        return
    ai_profile_id = int(ai_profile["id"])

    if not USE_LLM:
        if FIXED_REPLY_ENABLED:
            await _schedule_fixed_reply(
                conversation_id=conversation_id,
                ai_profile_id=ai_profile_id,
                ai_profile_name=ai_profile["name"],
                user_text=user_text,
            )
        else:
            logger.warning(
                "USE_LLM=False 且未开启 DSH_FIXED_REPLY：会话 %s 不做任何回复",
                conversation_id,
            )
        return

    # 0. 图片消息：这个模型到底看不看得见图（第十三轮）。
    #
    image_url = user_text if vision.is_data_url(user_text) else ""
    prompt_user_text = vision.display_text(content_type, user_text)
    is_image_turn = bool(image_url) or vision.IMAGE_PLACEHOLDER in (user_text or "")
    if is_image_turn and not image_url:
        # user_text 里的图已经被换成 [图片] 占位了（连发合并、或客户端把图当
        # text 发过来时）。支持视觉的模型不该因为这点就看不见图 ——
        # 回库里把那条图的原图捞出来。
        image_url = _latest_image_data_url(conversation_id)
    active_cfg = api_pool.first_config(
        role="chat", user_id=user_id, ai_profile_id=ai_profile_id
    )
    # 只在**这一轮真的带图**时才去问服务端能力 —— 普通聊天不该多一次网络请求。
    can_see_image = bool(
        is_image_turn and await api_pool.config_supports_vision(active_cfg)
    )
    if is_image_turn:
        logger.info(
            "这一轮带图：model=%r 支持视觉=%s（不支持就如实说看不到，不许猜）",
            active_cfg.model_name if active_cfg else "(无配置)",
            can_see_image,
        )

    # 1. 组装 Prompt（人格层 / 状态层 / 记忆层 / 上下文层）
    persona_data = persona.load_personality(ai_profile_id)
    state = sm.get_state(ai_profile_id)
    bundle = mem.build_memory_bundle(
        ai_profile_id=ai_profile_id,
        conversation_id=conversation_id,
        query=prompt_user_text,
        user_display=USER_DISPLAY,
    )
    messages = custom_persona.build_chat_prompt(
        personality_data=persona_data,
        ai_name=ai_profile["name"],
        personality_prompt=persona.personality_prompt(persona_data, ai_profile["name"]),
        emotion=state.emotion,
        energy=state.energy,
        intimacy=state.intimacy,
        short_term_window=bundle.short_term_window,
        mid_term_summary=bundle.mid_term_summary,
        long_term_memories=bundle.long_term_memories,
        conversation_history=mem.conversation_history_text(conversation_id, USER_DISPLAY),
        user_message=prompt_user_text,
        proactive_topic=proactive_topic,
        is_urge=is_urge,
        force_reply=force_reply,
        # 带图但模型看不见时，明确告诉它「你看不到，别猜」。
        image_blind=is_image_turn and not can_see_image,
    )
    if image_url and can_see_image:
        # 真看得见的模型：把原图作为多模态内容附在最后一条用户消息上。
        messages = vision.attach_image(messages, image_url)

    if custom_persona.has_script(persona_data) and not custom_persona.uses_card(persona_data):
        messages = performance_layer.attach(messages, emoji=False)

    # 2. 调 API + 结构化解析（失败重试一次，再失败降级）
    ai_response = await _call_and_parse(
        messages, user_id=user_id, ai_profile_id=ai_profile_id
    )

    # 2a. 联网搜索（第十三轮）：模型在 JSON 里填了 search_query 就查一次，
    #     把结果喂回去让它重新答一遍。最多一轮，避免无限查下去烧钱。
    ai_response = await _search_and_reanswer(
        ai_response,
        messages=messages,
        user_id=user_id,
        ai_profile_id=ai_profile_id,
    )

    if force_reply and ai_response.action == "ignore":
        # 用户已经催到第 URGE_FORCE_REPLY_AFTER 次，模型还想装死。
        # 这时候必须有人兜底 —— 催了三次还不回，用户只会觉得这东西坏了。
        # 做法：退化成一句最短的回应，而不是硬编内容（内容仍来自模型，
        # 只是不接受它的 ignore 决定）。
        logger.warning(
            "强制回复：用户已连催 %s 次，模型仍返回 ignore，改为回一句最短回应",
            URGE_FORCE_REPLY_AFTER,
        )
        ai_response = ai_response.model_copy(
            update={
                "action": "reply",
                "content": ["……知道了"],
                "message_count": 1,
                "segment_delays": [0],
                "delay_seconds": 0,
            }
        )
    elif is_urge and ai_response.action == "reply":
        # 用户在催，AI 说要回但要等 3 分钟 —— 那等于没催。
        # 这里把首响压到 3 秒内（后续段间隔保留 AI 的判断）。
        delays = list(ai_response.segment_delays or [])
        if delays:
            delays[0] = min(delays[0], URGE_MAX_ADVANCE_SECONDS)
            ai_response = ai_response.model_copy(update={"segment_delays": delays})
        elif ai_response.delay_seconds > URGE_MAX_ADVANCE_SECONDS:
            ai_response = ai_response.model_copy(
                update={"delay_seconds": URGE_MAX_ADVANCE_SECONDS}
            )

    # 2b. PRD 3.3.3：「能量低、情绪低落、或亲密度未达阈值时，action 更可能为 ignore」。
    #
    # 模型本来就会看状态层里的三个值自己决定，但实测它经常一路回到底 ——
    # 亲密度 0.05 的陌生人深夜找它，它也热情回复。所以这里加一道**硬约束**：
    # 用 state_machine.ignore_bias 算出「这一刻该不该沉默」，只在倾向分
    # **很低**（状态好、关系近）却仍选 ignore 时才推翻模型 ——
    # 那种情况下装死不合理，用户会觉得「都这么熟了还装没看见」。
    # 反过来倾向分高时，模型选 ignore 一律尊重，不再想办法逼它回话。
    if ai_response.action == "ignore":
        now_state = sm.get_state(ai_profile_id)
        bias = sm.ignore_bias(now_state.emotion, now_state.energy, now_state.intimacy)
        if bias < IGNORE_BLOCK_BIAS:
            logger.info(
                "状态良好（ignore 倾向 %.2f < %.2f）却选了已读不回，改为最短回复：ai=%s",
                bias,
                IGNORE_BLOCK_BIAS,
                ai_profile_id,
            )
            ai_response = ai_response.model_copy(
                update={
                    "action": "reply",
                    "content": ["嗯"],
                    "message_count": 1,
                    "segment_delays": [0],
                    "delay_seconds": 0,
                }
            )
        else:
            logger.info(
                "已读不回（ignore 倾向 %.2f）：ai=%s energy=%.2f intimacy=%.2f",
                bias,
                ai_profile_id,
                now_state.energy,
                now_state.intimacy,
            )

    # 3. 应用状态更新
    new_state = sm.apply_update(ai_profile_id, ai_response.state_update)
    db.touch_ai_active(ai_profile_id)

    # 3b. PRD 3.3.3：用户持续发消息时，能量可能因“被打扰”降低，也可能因“被需要”回升。
    #     方向由人格（reply_rhythm.reply_rate）决定，见 state_machine.disturbance_effect。
    unanswered = _trailing_user_message_count(conversation_id)
    if unanswered >= sm.DISTURBANCE_MIN_UNANSWERED:
        moved = sm.apply_disturbance(
            ai_profile_id, reply_rate=persona_data["reply_rhythm"]["reply_rate"]
        )
        if moved is not None:
            new_state = sm.get_state(ai_profile_id)

    # 3c. PRD 3.2.2「亲密度随对话频率和深度缓慢积累」。
    #     原来完全靠模型每次自己给 intimacy_delta，模型给 0 就永远不涨，
    #     「关系越来越好」就是空话。这里由服务端保证一个与模型无关的基础累积。
    if user_text and ai_response.action != "ignore":
        sm.accumulate_intimacy(
            ai_profile_id, segments=max(1, len(ai_response.content))
        )
        new_state = sm.get_state(ai_profile_id)

    # 4. 按 action 执行
    await execute_action(
        conversation_id=conversation_id,
        ai_profile_id=ai_profile_id,
        ai_name=ai_profile["name"],
        ai_response=ai_response,
        state=new_state,
        is_proactive=is_proactive,
    )

    # 5. 后台记忆维护：摘要、事实提取与 Token 压缩
    await maintain_memory(
        ai_profile_id=ai_profile_id,
        conversation_id=conversation_id,
        user_id=user_id,
    )

    # 6. PRD 3.2.7 / 3.3.4：人格档案在后续对话中持续微调
    refine_personality(ai_profile_id=ai_profile_id, conversation_id=conversation_id)

def _latest_image_data_url(conversation_id: int, limit: int = 10) -> str:
    """会话里最近一条图片消息的 data URL；没有就返回空串（第十三轮）。

    只在「这一轮确实带图、但 user_text 已经被换成 [图片] 占位」时才用得上 ——
    典型场景是连发合并把「图片 + 一句话」拼成了一段文本。
    只往回找 limit 条：图片是**这一轮**发的，翻太远会把上一张旧图当成新的发过去。
    """
    try:
        rows = db.list_messages(conversation_id, limit=limit)
    except Exception:  # noqa: BLE001 - 取图失败就退回「看不见」
        return ""
    for row in reversed(rows):
        if row["sender_type"] != "user":
            continue
        content = row["content"] or ""
        if vision.is_image_message(row["content_type"], content):
            if vision.is_data_url(content):
                return content
            for part in db.content_to_array(content):
                if vision.is_data_url(part):
                    return part
    return ""

def _trailing_user_message_count(conversation_id: int, limit: int = 20) -> int:
    """统计最近连续多少条用户消息还没等到 AI 回复（PRD 3.3.3 的“持续发消息”）。"""
    rows = db.list_messages(conversation_id, limit=limit)
    count = 0
    for row in reversed(rows):
        if row["sender_type"] == "user":
            count += 1
        else:
            break
    return count

def refine_personality(*, ai_profile_id: int, conversation_id: int) -> None:
    """对话后持续微调人格档案（PRD 3.2.7 / 3.3.4）。失败不影响消息链路。"""
    try:
        persona.refine_from_conversation(ai_profile_id, conversation_id, user_display=USER_DISPLAY)
    except Exception:  # noqa: BLE001 - 微调失败不能影响消息链路
        logger.exception("人格持续微调失败 ai=%s conversation=%s", ai_profile_id, conversation_id)

async def _search_and_reanswer(
    ai_response: AIResponse,
    *,
    messages: list[dict],
    user_id: int,
    ai_profile_id: Optional[int] = None,
) -> AIResponse:
    """模型要求联网时就查一次，把结果喂回去让它重新回答（第十三轮）。

    为什么是这个形状，而不是「模型自己调工具」的循环： 规定
    模型一次只返回一个 JSON、由服务端解析执行。这里沿用同一模式 ——
    模型在 JSON 里填 `search_query`，服务端搜完把结果拼成一条新的用户消息，
    让它**再答一次**，然后**只认第二次的结果**（第二次的 search_query 会被
    忽略，所以最多查一轮，不会无限烧钱）。

    搜索本身失败（网络不通 / 没搜到）**不算错误**：照样让它再答一次，
    但提示里会说「没搜到」——它会如实告诉用户查不到，而不是继续编。
    """
    query = (ai_response.search_query or "").strip()
    if not query:
        return ai_response

    logger.info("模型请求联网：%r", query[:80])
    try:
        results = await web_search.search(query)
    except Exception as exc:  # noqa: BLE001 - 搜索坏了也必须能继续回话
        # web_search.search 自己已经兜了一层，这里再兜一次是因为**测试/替换实现**
        # 或将来换后端时可能抛出别的异常 —— 搜索只是加分项，
        # 绝不能因为它把整条回复链路打断（用户等的是回话，不是搜索结果）。
        logger.warning("搜索异常，按「没搜到」处理：%s", exc)
        results = []
    followup = web_search.followup_text(query, results)

    # 把搜索结果接在原来的 messages 后面当成一轮新的对话。
    # 不放进 system 里，是为了让它明确知道「这是刚查回来的」。
    follow_messages = list(messages) + [{"role": "user", "content": followup}]
    try:
        second = await _call_and_parse(
            follow_messages, user_id=user_id, ai_profile_id=ai_profile_id
        )
    except Exception as exc:  # noqa: BLE001 - 第二次失败就用第一次的结果，别把回复弄丢
        logger.warning("带搜索结果重新生成失败，沿用第一次的回答：%s", exc)
        return ai_response

    # 第二次还可能填 search_query（模型不听话），这里直接清掉：一轮只搜一次。
    if second.search_query:
        logger.info("忽略第二次返回的 search_query=%r（一轮只搜一次）", second.search_query)
        second = second.model_copy(update={"search_query": None})
    logger.info(
        "联网完成：搜到 %s 条，回复从 %s 段变为 %s 段",
        len(results),
        len(ai_response.content),
        len(second.content),
    )
    return second

async def _call_and_parse(
    messages: list[dict], *, user_id: int, ai_profile_id: Optional[int] = None
) -> AIResponse:
    """调用主模型并解析；非法 JSON 重试一次，仍失败降级。

    2026-09-27：新增 ai_profile_id，让会话级 API 优先于全局池；
    并且**“没配 API”不再降级成假回复** —— 以前会退化成 fallback_response（「……」），
    用户看到 AI 莫名其妙回一个省略号，却不知道是自己没填 Key。
    现在这种情况直接抛 LLMCallError，由上层明确报错。
    """
    last_raw = ""
    for attempt in (1, 2):
        try:
            raw = await api_pool.chat(
                messages,
                role="chat",
                user_id=user_id,
                ai_profile_id=ai_profile_id,
            )
            last_raw = raw
            return parse_ai_response(raw)
        except api_pool.LLMCallError as exc:
            # API 层失败（没配 Key / 401 / 超时 / 全部配置都挂了）：
            # 这是**配置或网络问题**，不是模型输出问题，降级成「……」只会骗用户。
            # 因此重试一次仍然失败就把错误原样抛给上层，让它明确告诉用户。
            logger.warning("AI 调用失败（第 %s 次）：%s", attempt, exc)
            if attempt == 2:
                raise
        except Exception as exc:  # noqa: BLE001 - 校验失败或 API 失败都走重试
            logger.warning("AI 输出解析失败（第 %s 次）：%s", attempt, exc)
            if attempt == 2:
                logger.error("两次解析均失败，降级为纯文本回复。原始返回：%s", last_raw[:500])
    return fallback_response(last_raw)

# ---------------------------------------------------------------- action 执行

async def execute_action(
    *,
    conversation_id: int,
    ai_profile_id: int,
    ai_name: str,
    ai_response: AIResponse,
    state: sm.State,
    is_proactive: bool = False,
) -> Optional[str]:
    """按 action 执行。

    - reply：延迟队列推送 content
    - ignore：不发送，状态已更新（已读不回）
    - double_text：先发 content，再排 follow_up
    返回主消息的 msg_id（ignore 返回 None）。
    """
    action = ai_response.action

    if action == "ignore":
        logger.info("已读不回（action=ignore）conversation=%s", conversation_id)
        return None

    segment_delays = _resolve_segment_delays(ai_response)
    main_msg_id = ""
    for index, text in enumerate(ai_response.content):
        seg_delay = segment_delays[index] if index < len(segment_delays) else 0
        msg_id = await _enqueue_ai_message(
            conversation_id=conversation_id,
            ai_profile_id=ai_profile_id,
            ai_name=ai_name,
            content=[text],
            delay_seconds=seg_delay,
            is_proactive=is_proactive and index == 0,
            is_double_text=False,
            task_type="proactive" if (is_proactive and index == 0) else "send_message",
            segment_index=index,
            segment_count=len(ai_response.content),
        )
        if index == 0:
            main_msg_id = msg_id
    if not ai_response.content:  # pragma: no cover - reply 必带 content，这里只是防御
        return None

    if action == "double_text" and ai_response.follow_up:
        follow_delay = random.randint(
            DOUBLE_TEXT_MIN_DELAY_SECONDS, DOUBLE_TEXT_MAX_DELAY_SECONDS
        )
        await _enqueue_ai_message(
            conversation_id=conversation_id,
            ai_profile_id=ai_profile_id,
            ai_name=ai_name,
            content=[ai_response.follow_up],
            delay_seconds=follow_delay,
            is_proactive=False,
            is_double_text=True,
            task_type="double_text",
        )
        logger.info(
            "double_text 已排后续消息：conversation=%s delay=%ss follow_up=%s",
            conversation_id,
            follow_delay,
            ai_response.follow_up[:40],
        )
    return main_msg_id

def _resolve_segment_delays(ai_response: AIResponse) -> list[int]:
    """算出每一段各自的延迟（秒）。第 0 段=首响，其余=相对上一段的间隔。

    模型给了 segment_delays 就用它；没给就退化成「第 0 段用 delay_seconds、
    其余段立即」（与旧行为一致，保证模型不配合时也能正常发消息）。
    """
    count = len(ai_response.content)
    if count == 0:
        return []
    delays = list(ai_response.segment_delays or [])
    if len(delays) == count:
        return [sm.resolve_delay(d) for d in delays]
    head = sm.resolve_delay(ai_response.delay_seconds)
    return [head] + [0] * (count - 1)

async def _enqueue_ai_message(
    *,
    conversation_id: int,
    ai_profile_id: int,
    ai_name: str,
    content: list[str],
    delay_seconds: int,
    is_proactive: bool,
    is_double_text: bool,
    task_type: str,
    segment_index: int = 0,
    segment_count: int = 1,
) -> str:
    """把一条 AI 消息落库并排入延迟队列。"""
    # AI 说等多久就等多久，只保留 1 天的格式边界（见 state_machine.resolve_delay）。
    delay = sm.resolve_delay(delay_seconds)
    stored = json.dumps(content, ensure_ascii=False)
    ai_row = db.insert_message(
        conversation_id=conversation_id,
        sender_type="ai",
        sender_id=ai_profile_id,
        content=stored,
        timestamp=db.iso_in_seconds(delay),
        content_type="text",
        state="pending",
    )
    if ai_row is None:  # pragma: no cover - msg_id 冲突概率极低
        raise RuntimeError("AI 消息写入失败（msg_id 冲突）")

    payload: dict[str, Any] = {
        "ai_profile_id": ai_profile_id,
        "sender_name": ai_name,
        "content": content,
        "content_type": "text",
        "is_proactive": is_proactive,
        "is_double_text": is_double_text,
        "delay_seconds": delay,
        "msg_id": ai_row["msg_id"],
        "message_id": ai_row["id"],
        # 这一段在多段回复里的位置（客户端拼气泡、排障时看日志都用得上）。
        "segment_index": segment_index,
        "segment_count": segment_count,
    }
    task_id = db.enqueue_task(
        conversation_id=conversation_id,
        task_type=task_type,
        payload=payload,
        execute_at=db.iso_in_seconds(delay),
        message_id=ai_row["id"],
    )
    logger.info(
        "已排入延迟队列：task_id=%s type=%s msg_id=%s delay=%ss content=%s",
        task_id,
        task_type,
        ai_row["msg_id"],
        delay,
        content,
    )
    return str(ai_row["msg_id"])

# ---------------------------------------------------------------- 记忆维护

async def maintain_memory(
    *, ai_profile_id: int, conversation_id: int, user_id: int = DEFAULT_USER_ID
) -> None:
    """对话后的记忆维护：摘要 -> 事实提取 -> Token 压缩。"""
    try:
        await mem.maybe_generate_summary(
            conversation_id, user_id=user_id, user_display=USER_DISPLAY
        )
        window = mem.short_term_window(conversation_id)
        if window:
            await mem.extract_and_store_facts(
                ai_profile_id=ai_profile_id,
                conversation_id=conversation_id,
                rows=window[-4:],
                user_id=user_id,
                user_display=USER_DISPLAY,
            )
        await mem.maybe_compress_by_tokens(
            conversation_id, user_id=user_id, user_display=USER_DISPLAY
        )
    except Exception:  # noqa: BLE001 - 记忆维护失败不能影响消息链路
        logger.exception("记忆维护失败 conversation=%s", conversation_id)

# ---------------------------------------------------------------- 固定回复（回归测试用）

async def _schedule_fixed_reply(
    *,
    conversation_id: int,
    ai_profile_id: int,
    ai_profile_name: str,
    user_text: str,
) -> None:
    """阶段1任务1.14：写死的多段回复，固定延迟后推送。

    保留该分支是为了满足阶段1验收“写死逻辑回复可测”，
    阶段2起 USE_LLM=True 走完整 Prompt + API 管线。
    """
    content = list(FIXED_REPLY_CONTENT)
    stored = json.dumps(content, ensure_ascii=False)
    ai_row = db.insert_message(
        conversation_id=conversation_id,
        sender_type="ai",
        sender_id=ai_profile_id,
        content=stored,
        timestamp=db.iso_in_seconds(FIXED_REPLY_DELAY_SECONDS),
        content_type="text",
        state="pending",
    )
    if ai_row is None:  # pragma: no cover
        logger.warning("AI 回复消息写入失败（msg_id 冲突）")
        return

    payload: dict[str, Any] = {
        "ai_profile_id": ai_profile_id,
        "sender_name": ai_profile_name,
        "content": content,
        "content_type": "text",
        "is_proactive": False,
        "is_double_text": False,
        "delay_seconds": FIXED_REPLY_DELAY_SECONDS,
        "msg_id": ai_row["msg_id"],
        "message_id": ai_row["id"],
        "user_text": user_text,
    }
    task_id = db.enqueue_task(
        conversation_id=conversation_id,
        task_type="send_message",
        payload=payload,
        execute_at=db.iso_in_seconds(FIXED_REPLY_DELAY_SECONDS),
        message_id=ai_row["id"],
    )
    logger.info(
        "已排入延迟队列（写死回复）：task_id=%s msg_id=%s delay=%ss content=%s",
        task_id,
        ai_row["msg_id"],
        FIXED_REPLY_DELAY_SECONDS,
        content,
    )