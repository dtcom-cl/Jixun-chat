"""群聊管理器（阶段4任务4.1 ~ 4.5、4.8）。

职责：管理群聊会话，执行抢麦机制。

- 群聊模式（抢麦、冷却惩罚、阈值静默）
- 群聊管理器 / 五、核心数据流 5.4
- 六、群聊抢麦 Prompt
- ~ 4.5、4.8

关键规则（文档写死）：
- 每轮对话中每个 AI 私下评估自己“是否想发言”，给出 0-1 的意愿分。
- 只有意愿分最高且超过阈值的 AI 获得发言权，其余候选发言丢弃。
- 最近发言过的 AI 有冷却惩罚，从意愿分中扣减，防止同一个 AI 连续独白。
- 没有任何 AI 超过阈值时，对话自然静默（群聊不会没话找话）。
- 群聊中同样遵循延迟回复、多段回复、已读不回（ignore 表现为不发言但状态更新）。

文档未写死的细节，自行决定并记录：
1. 意愿分阈值 GROUP_WILLINGNESS_THRESHOLD = 0.6（与反思循环一致）。
2. 冷却惩罚 GROUP_COOLDOWN_PENALTY = 0.35，作用于“本会话内最近一条 AI 消息的发送者”。
3. 抢麦评估用 role="judge" 的轻量模型；获胜者才调 role="chat" 主模型生成内容。
4. 冷却惩罚在评估之后、比较之前扣减（docs 顺序：收集意愿分 -> 应用冷却惩罚 -> 选择最高）。
5. 群聊上下文背景（PRD 3.4.1）：上传的聊天记录文档只做“上下文背景”，注入到抢麦评估
   与内容生成的 Prompt 里，格式与 personality.parse_chat_log 一致（me=用户，ta=目标朋友）。
   为控制费用，最多注入 GROUP_BACKGROUND_MAX_LINES = 60 行（取最近的记录）。
"""
from __future__ import annotations

import logging
import random
from typing import Any, Optional

import api_pool
import database as db
import custom_persona
import dispatcher
import memory as mem
import persona_text
import performance_layer
import personality as persona
import prompt_builder
import state_machine as sm
from config import (
    DEFAULT_USER_ID,
    GROUP_COOLDOWN_PENALTY,
    GROUP_MAX_SPEAKERS_PER_TURN,
    GROUP_ROUND_MAX_DELAY_SECONDS,
    GROUP_ROUND_MIN_DELAY_SECONDS,
    GROUP_WILLINGNESS_THRESHOLD,
)
from schemas import GroupBid

logger = logging.getLogger("group_chat")

USER_DISPLAY = "用户"

# 群聊背景注入 Prompt 的最大行数（自行决定：控费 + 防止 Prompt 过长）
GROUP_BACKGROUND_MAX_LINES = 60

def _extract_json(raw: str) -> dict:
    import json

    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("抢麦结果不是 JSON 对象")
    return data

# ---------------------------------------------------------------- 群聊会话

def create_group(
    *,
    user_id: int = DEFAULT_USER_ID,
    title: str,
    ai_profile_ids: list[int],
    background_records: Any = None,
) -> int:
    """创建群聊会话。participants 是 JSON 数组，存多个 AI profile ID。

    :param background_records: PRD 3.4.1 的群聊上下文背景（JSON 聊天记录），
        可为 None / 字符串 / 已解析的列表。解析失败直接报错，不静默吞掉。
    """
    if not ai_profile_ids:
        raise ValueError("群聊至少需要一个 AI")
    for ai_id in ai_profile_ids:
        # 用户 2026-09-27 第七轮：「人类好友不能拉群」。
        # 群聊的抢麦机制靠意愿分投标，真人不会投标 —— 把人类 ID 放进来
        # 只会让这个群永远不发言。
        #
        # 判定顺序很重要：**必须先查 ai_profiles**。users 与 ai_profiles 是两套
        # 各自从 1 开始的自增 ID，一定撞号（users.id=1 是账号主人，
        # ai_profiles.id=1 是某个 AI，两者同时存在）。先查 users 的话，
        # 每个 id=1 的 AI 都会被误判成「人类好友」。
        if db.get_ai_profile(ai_id) is not None:
            continue
        if db.get_user(ai_id) is not None:
            raise ValueError("人类好友不能拉进群聊")
        raise ValueError(f"AI 档案不存在：{ai_id}")
    conv_id = db.create_conversation(
        user_id=user_id,
        conv_type="group",
        title=title,
        participants=ai_profile_ids,
    )
    background_text = normalize_group_background(background_records)
    if background_text:
        db.set_conversation_background(conv_id, background_text)
    logger.info(
        "创建群聊 conversation=%s title=%s members=%s background_lines=%s",
        conv_id,
        title,
        ai_profile_ids,
        len(background_text.splitlines()) if background_text else 0,
    )
    return conv_id

# ---------------------------------------------------------------- 群聊背景（PRD 3.4.1）

def normalize_group_background(records: Any) -> str:
    """把上传的群聊聊天记录规整成可注入 Prompt 的文本（PRD 3.4.1）。

    三种输入都要能收（群聊背景和单聊人格不是同一件事，不能共用解析器）：

    - None / 空 -> 空字符串（没有背景）；
    - 字符串 -> 先当「说话人：内容」的对话文本解析，认不出来就**按纯文本原样保留**
      （用户可能直接贴一份文档或群公告）；
    - 结构化列表（`[{"sender": ..., "content": ...}]`）-> 逐条转成 `me|时间|内容`，
      这些记录在群聊里还要显示发言人，不能走 persona_text（它只认两方对话）。
    """
    if records is None:
        return ""
    if isinstance(records, str):
        # 字符串原样保留：用户贴进来的多半已经是「老王：今天去哪」这种可读形式，
        # 再解析成 `friend||今天去哪` 反而会把发言人名字弄丢。
        # 只有结构化列表才需要转成 `me|时间|内容`（见下面的分支）。
        return records.strip()

    rows = _as_record_list(records)
    if not rows:
        return ""
    return persona._records_to_lines(rows, max_lines=GROUP_BACKGROUND_MAX_LINES)

def _as_record_list(records: Any) -> list[dict[str, str]]:
    """把结构化聊天记录规整成 `_records_to_lines` 认识的形式。

    宽松识别 sender：me/user/我 -> user，其余（ta/assistant/具体昵称）-> friend。
    两条都是空的记录直接丢掉，免得在 Prompt 里留一堆空行。
    """
    if isinstance(records, dict):
        for key in ("messages", "records", "chat_log", "data"):
            if isinstance(records.get(key), list):
                records = records[key]
                break
    if not isinstance(records, list):
        return []
    out: list[dict[str, str]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if content is None:
            content = item.get("text") or item.get("message") or ""
        content = str(content).strip()
        if not content:
            continue
        who = str(item.get("sender") or item.get("role") or item.get("from") or "").strip()
        sender = "user" if who.lower() in persona_text.USER_ALIASES else "friend"
        out.append(
            {
                "sender": sender,
                "content": content,
                "timestamp": str(item.get("timestamp") or item.get("time") or ""),
            }
        )
    return out

def group_background_prompt(conversation_id: int) -> str:
    """读取群聊背景并包成 Prompt 片段（没有背景时返回空字符串）。"""
    text = db.conversation_background(conversation_id)
    if not text:
        return ""
    return (
        "【这个群之前的聊天记录（背景资料，仅作参考）】\n"
        f"{text}\n"
        "以上是群里以前的聊天记录。你要延续这些人的说话风格和关系，"
        "但不要生硬复述里面的内容。"
    )

# ---------------------------------------------------------------- 抢麦

async def _bid(
    *, ai_profile_row, conversation_id: int, user_id: int
) -> GroupBid:
    """单个 AI 的意愿分评估。"""
    ai_profile_id = int(ai_profile_row["id"])
    state = sm.get_state(ai_profile_id)
    persona_data = persona.load_personality(ai_profile_id)
    personality_summary = "\n".join(
        persona.personality_prompt(persona_data, ai_profile_row["name"]).split("\n")[:12]
    )
    messages = prompt_builder.build_group_bid_prompt(
        personality_summary=personality_summary,
        emotion=state.emotion,
        energy=state.energy,
        intimacy=state.intimacy,
        group_conversation=mem.conversation_history_text(conversation_id, USER_DISPLAY),
        group_background=group_background_prompt(conversation_id),
    )
    raw = await api_pool.chat(
        messages,
        role="judge",
        user_id=user_id,
        temperature=0.5,
        # 2026-09-28 第九轮：群聊抢麦也要用会话设置里的 Key，否则永远判「没配 API」。
        ai_profile_id=ai_profile_id,
    )
    try:
        return GroupBid.model_validate(_extract_json(raw))
    except Exception as exc:  # noqa: BLE001 - 解析失败视为不发言
        logger.warning("抢麦意愿分解析失败：%s", exc)
        return GroupBid(willingness=0.0, reason="解析失败")

def _last_speaker_id(conversation_id: int) -> Optional[int]:
    """本会话最近一条 AI 消息的 sender_id，用于冷却惩罚。"""
    rows = db.list_messages(conversation_id, limit=20)
    for row in reversed(rows):
        if row["sender_type"] == "ai" and row["sender_id"] is not None:
            return int(row["sender_id"])
    return None

async def pick_speaker(
    *,
    conversation_id: int,
    user_id: int = DEFAULT_USER_ID,
    bids_override: Optional[dict[int, float]] = None,
) -> dict[str, Any]:
    """执行一轮抢麦，返回 {speaker_id, willingness, reason, silent, scores}。

    :param bids_override: 测试用，{ai_profile_id: willingness} 直接给定意愿分，
        绕过轻量模型调用。
    """
    participants = db.conversation_participants(conversation_id)
    if not participants:
        return {"silent": True, "reason": "群聊没有成员", "scores": {}, "speaker_id": None}

    scores: dict[int, float] = {}
    reasons: dict[int, str] = {}
    for ai_id in participants:
        ai_row = db.get_ai_profile(ai_id)
        if ai_row is None or int(ai_row["is_active"] or 0) != 1:
            continue
        if bids_override is not None and ai_id in bids_override:
            bid = GroupBid(
                willingness=max(0.0, min(1.0, bids_override[ai_id])), reason="override"
            )
        else:
            bid = await _bid(
                ai_profile_row=ai_row, conversation_id=conversation_id, user_id=user_id
            )
        scores[ai_id] = bid.willingness
        reasons[ai_id] = bid.reason

    # 冷却惩罚：最近发言过的 AI 扣减意愿分
    last_speaker = _last_speaker_id(conversation_id)
    if last_speaker is not None and last_speaker in scores:
        scores[last_speaker] = max(0.0, scores[last_speaker] - GROUP_COOLDOWN_PENALTY)
        logger.info("冷却惩罚：ai=%s 意愿分降至 %.2f", last_speaker, scores[last_speaker])

    if not scores:
        return {"silent": True, "reason": "没有可发言的 AI", "scores": {}, "speaker_id": None}

    speaker_id, best = max(scores.items(), key=lambda item: item[1])
    if best <= GROUP_WILLINGNESS_THRESHOLD:
        logger.info("群聊静默：最高意愿分 %.2f 未超过阈值 %.2f", best, GROUP_WILLINGNESS_THRESHOLD)
        return {
            "silent": True,
            "reason": f"最高意愿分 {best:.2f} <= 阈值 {GROUP_WILLINGNESS_THRESHOLD:.2f}",
            "scores": scores,
            "speaker_id": None,
        }

    return {
        "silent": False,
        "speaker_id": speaker_id,
        "willingness": best,
        "reason": reasons.get(speaker_id, ""),
        "scores": scores,
    }

# ---------------------------------------------------------------- 群聊消息处理

async def _run_group_turn(
    *,
    user_id: int,
    conversation_id: int,
    user_text: str,
    content_type: str = "text",
    bids_override: Optional[dict[int, float]] = None,
    ai_response_override: Optional[Any] = None,
    interject: bool = False,
) -> dict[str, Any]:
    """跑一轮群聊：所有 AI 抢麦 -> 获胜者生成内容 -> 落库排队。

    2026-09-30 第十四轮从 handle_group_message 里抽出来，让「用户发消息」
    与「接别人的话」两轮共用同一套抢麦 / 状态更新 / 落库逻辑。

    :param interject: 这一轮是接群里别人的话，不是回答用户（决定上下文层）。
    """
    result = await pick_speaker(
        conversation_id=conversation_id, user_id=user_id, bids_override=bids_override
    )
    if result["silent"]:
        return result

    speaker_id = int(result["speaker_id"])
    ai_row = db.get_ai_profile(speaker_id)
    if ai_row is None:
        return {**result, "silent": True, "reason": "获胜 AI 档案不存在"}

    if ai_response_override is not None:
        ai_response = ai_response_override
    else:
        # 获胜者才调主模型生成实际内容
        persona_data = persona.load_personality(speaker_id)
        state = sm.get_state(speaker_id)
        bundle = mem.build_memory_bundle(
            ai_profile_id=speaker_id,
            conversation_id=conversation_id,
            query=user_text,
            user_display=USER_DISPLAY,
        )
        messages = custom_persona.build_chat_prompt(
            personality_data=persona_data,
            ai_name=ai_row["name"],
            personality_prompt=persona.personality_prompt(persona_data, ai_row["name"]),
            emotion=state.emotion,
            energy=state.energy,
            intimacy=state.intimacy,
            short_term_window=bundle.short_term_window,
            mid_term_summary=bundle.mid_term_summary,
            long_term_memories=bundle.long_term_memories,
            conversation_history=mem.conversation_history_text(conversation_id, USER_DISPLAY),
            user_message=user_text,
            group_background=group_background_prompt(conversation_id),
            # 第十四轮：接话轮不能再用「用户刚刚说」那套上下文。
            group_interject=interject,
        )
        # 2026-09-28 第九轮：必须带 speaker_id。不带的话 role="chat" 的候选里
        # 没有会话级配置，用户在会话设置里填的 Key 会被忽略（详见 api_pool.chat）。
        if custom_persona.has_script(persona_data) and not custom_persona.uses_card(persona_data):
            messages = performance_layer.attach(messages, emoji=False)
        ai_response = await dispatcher._call_and_parse(
            messages, user_id=user_id, ai_profile_id=speaker_id
        )

    # 状态更新对所有 AI 都发生（未发言的 AI 也"看到了消息"）——
    new_state = sm.apply_update(speaker_id, ai_response.state_update)
    db.touch_ai_active(speaker_id)

    # 群聊中 ignore 表现为"不发言但状态已更新"
    if ai_response.action == "ignore":
        logger.info("群聊获胜 AI 选择已读不回（不发言）：ai=%s", speaker_id)
        return {**result, "ignored": True, "msg_id": None}

    msg_id = await dispatcher.execute_action(
        conversation_id=conversation_id,
        ai_profile_id=speaker_id,
        ai_name=ai_row["name"],
        ai_response=ai_response,
        state=new_state,
    )

    await dispatcher.maintain_memory(
        ai_profile_id=speaker_id, conversation_id=conversation_id, user_id=user_id
    )
    # PRD 3.2.7 / 3.3.4：群聊里的对话同样参与人格持续微调
    dispatcher.refine_personality(ai_profile_id=speaker_id, conversation_id=conversation_id)
    return {**result, "ignored": False, "msg_id": msg_id}

def _schedule_next_round(
    *, conversation_id: int, user_id: int, round_index: int
) -> Optional[int]:
    """排下一轮「接话」抢麦，让 AI 之间能相互插话。

              而是有一搭没一搭的」。

    为什么走延迟队列、而不是当场再 await 一轮：
    - `handle_group_message` 是在 WS 接收循环里被 await 的，连着跑几轮模型调用
      会把整条连接卡住（心跳、新消息全都收不到）；
    - 任务落库，服务端重启也不丢（delay_queue 已有恢复机制）。

    时间点 = max(现在, 这个会话里还没发出去的 AI 消息最晚那条) + 2~7 秒：
    必须等上一个人的话**真的发出去**之后再插话，否则屏幕上顺序会乱。
    达到 GROUP_MAX_SPEAKERS_PER_TURN 就不再排（防刷屏）。
    """
    if round_index >= GROUP_MAX_SPEAKERS_PER_TURN:
        logger.info(
            "群聊接话已达上限 %s 个 AI，不再排下一轮：conversation=%s",
            GROUP_MAX_SPEAKERS_PER_TURN,
            conversation_id,
        )
        return None

    gap = random.uniform(GROUP_ROUND_MIN_DELAY_SECONDS, GROUP_ROUND_MAX_DELAY_SECONDS)
    latest = db.pending_tasks_latest_execute_at(conversation_id)
    execute_at = (
        db.iso_after_seconds(latest, gap) if latest else db.iso_in_seconds(gap)
    )
    task_id = db.enqueue_task(
        conversation_id=conversation_id,
        task_type="group_round",
        payload={"user_id": user_id, "round_index": round_index},
        execute_at=execute_at,
    )
    logger.info(
        "已排下一轮群聊抢麦：task_id=%s conversation=%s round=%s 等到 %s",
        task_id,
        conversation_id,
        round_index,
        execute_at,
    )
    return task_id

def _latest_group_text(conversation_id: int) -> str:
    """群里最后一条有内容的消息文本（接话轮用）。

    接话轮没有新的用户消息，但生成 Prompt 需要一个抓手：记忆检索的 query。
    （上下文层用的是完整群聊历史，这里只影响检索与兜底文案。）
    """
    for row in reversed(db.list_messages(conversation_id, limit=5)):
        text = (mem.content_text(row) or "").strip()
        if text:
            return text
    return ""

async def handle_group_message(
    *,
    user_id: int,
    conversation_id: int,
    user_text: str,
    content_type: str = "text",
    bids_override: Optional[dict[int, float]] = None,
    ai_response_override: Optional[Any] = None,
    round_index: int = 0,
    schedule_next: bool = True,
) -> dict[str, Any]:
    """用户在群里发了一条消息：所有 AI 收到上下文 -> 抢麦 -> 获胜者生成内容。

    第十四轮起：这一轮说完了会**再排一轮**抢麦（_schedule_next_round），
    让别的 AI 有机会接梗/插话；哪一轮没人超过阈值，链子自然断掉（PRD 3.4.2）。

    :param bids_override: 测试用，直接指定每个 AI 的意愿分。
    :param ai_response_override: 测试用，直接指定获胜 AI 的 AIResponse。
    :param round_index: 这是本条用户消息引发的第几个 AI 发言（0 起）。
    :param schedule_next: 测试用，False 表示不排下一轮。
    """
    result = await _run_group_turn(
        user_id=user_id,
        conversation_id=conversation_id,
        user_text=user_text,
        content_type=content_type,
        bids_override=bids_override,
        ai_response_override=ai_response_override,
    )
    # 没人超过阈值（silent）或获胜者已读不回，都表示这一句到此为止，
    # 不该硬排下一轮 —— 群聊不能没话找话（PRD 3.4.2）。
    if schedule_next and not result.get("silent") and not result.get("ignored"):
        result["next_task_id"] = _schedule_next_round(
            conversation_id=conversation_id,
            user_id=user_id,
            round_index=round_index + 1,
        )
    return result

async def continue_group_round(
    *,
    conversation_id: int,
    user_id: int = DEFAULT_USER_ID,
    round_index: int = 1,
    bids_override: Optional[dict[int, float]] = None,
    ai_response_override: Optional[Any] = None,
    schedule_next: bool = True,
) -> dict[str, Any]:
    """延迟队列到点后跑的一轮「接话」抢麦（第十四轮）。

    与用户发消息那一轮的唯一区别：没有新的用户消息，上下文层换成
    「群里刚才有人说话了，你要接话」，所以模型是顺着**别人的话**说，
    而不是回答用户 —— 这才有「一群人在聊」的感觉。
    """
    result = await _run_group_turn(
        user_id=user_id,
        conversation_id=conversation_id,
        user_text=_latest_group_text(conversation_id),
        bids_override=bids_override,
        ai_response_override=ai_response_override,
        interject=True,
    )
    if schedule_next and not result.get("silent") and not result.get("ignored"):
        result["next_task_id"] = _schedule_next_round(
            conversation_id=conversation_id,
            user_id=user_id,
            round_index=round_index + 1,
        )
    return result