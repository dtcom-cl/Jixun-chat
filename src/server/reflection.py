"""后台反思循环。

职责：在用户静默期间，AI 进行“内心独白”，
判断是否主动发起聊天。

- 后台反思循环 / 3.3.1 主动发起聊天
- 反思循环、4.3.6 费用控制器
- 主动发起逻辑

关键规则（文档写死的部分）：
- 意愿分 willingness 0-1，超过 0.6 才可能开口。
- 每天主动发起不超过 5 次，超出静默。
- 两次主动发起之间有冷却时间。
- 反思循环用轻量模型（role=judge），只有确认要开口才调用主模型（费用控制）。

文档未写死的细节，自行决定并记录：
1. 反思循环间隔 REFLECTION_INTERVAL_SECONDS = 60 秒（config）。
2. 用户静默 REFLECTION_IDLE_SECONDS = 120 秒后才进入反思。
3. 冷却时间 PROACTIVE_COOLDOWN_SECONDS = 1800 秒（30 分钟）。
4. 意愿分阈值 REFLECTION_WILLINGNESS_THRESHOLD = 0.6（照抄文档 5.4）。
5. 空闲时间表
   在文档里没有给出具体时段，这里用 ai_profiles.personality_data.schedule.active_hours：
   当前小时在 active_hours 内 -> 阈值 -0.1；否则 +0.1。属于自行决定的实现细节。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

import api_pool
import database as db
import memory as mem
import personality as persona
import prompt_builder
import state_machine as sm
from config import (
    DEFAULT_USER_ID,
    PROACTIVE_COOLDOWN_SECONDS,
    PROACTIVE_DAILY_LIMIT,
    PROACTIVE_ENABLED,
    REFLECTION_IDLE_SECONDS,
    REFLECTION_INTERVAL_SECONDS,
    REFLECTION_WILLINGNESS_THRESHOLD,
)
from schemas import ReflectionResult

logger = logging.getLogger("reflection")

# 主动开口阈值的上下限。见 _threshold_for 的说明。
#
# 判定是 `willingness > threshold` 才开口，所以**上限越小越容易开口**。
#
# 为什么必须有上限：时段 +0.1、亲密度最低 +0.1 叠起来能算到 0.8，而实测模型
# 能给出的 willingness 上限只有 0.7~0.75 —— 阈值高过分数上限就等于「永远不开口」，
THRESHOLD_FLOOR = 0.2
THRESHOLD_CEILING = 0.5

_worker_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None

USER_DISPLAY = "用户"

# ---------------------------------------------------------------- 工具

def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None

def _seconds_since(value: str) -> Optional[float]:
    dt = _parse_iso(value)
    if dt is None:
        return None
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return (now - dt).total_seconds()

def _today() -> str:
    return db.now_iso()[:10]

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
        raise ValueError("反思结果不是 JSON 对象")
    return data

def idle_seconds(conversation_id: int) -> Optional[float]:
    """距离最后一条消息过去了多久（没有任何消息时返回 None）。"""
    last = db.last_message_in_conversation(conversation_id)
    if last is None:
        return None
    return _seconds_since(last["timestamp"])

def _threshold_for(ai_profile_row) -> float:
    """主动开口的意愿分阈值。

    两项调整：

    1. **空闲时间表**（PRD 3.3.1）：用户通常活跃的时段阈值降低，其他时段升高。
    2. **亲密度**（PRD 3.2.2 原文：「亲密度……影响角色主动开口的阈值和语气亲密程度」）：
       按 state_machine.threshold_shift_from_intimacy 算偏移
       （亲密度 0 → +0.1，1 → -0.1，见那里的系数说明）。

    最后**夹到 [THRESHOLD_FLOOR, THRESHOLD_CEILING] = [0.2, 0.4]**。
    为什么要夹上限：两个调整各自都合理，叠起来却会到 0.8 —— 而实测模型能给出的
    willingness 上限只有 0.7~0.75，阈值比它高就等于「永远不开口」，功能形同没做。
    用户的原话是「他真的能主动找我聊天吗？为什么一直不找？我都等烦了」。
    当前 0.2 / 0.4 是用户自己定的值，其副作用（两档调整被夹成同一个值）
    见模块顶部 THRESHOLD_CEILING 的注释；
    防打扰由下面 can_proactive 的冷却（30 分钟）与日上限（5 次）负责，
    不靠这个阈值。

    **不要**再把「亲密度低」加进 _threshold 之外的扣分项：亲密度在这里
    加门槛，Prompt 那边就不能再让模型按亲密度压分，否则同一因素算两次，
    新朋友（亲密度 0）会永远跨不过门槛 —— 而这正是本轮修的 bug。
    """
    threshold = REFLECTION_WILLINGNESS_THRESHOLD
    try:
        p = persona.load_personality(int(ai_profile_row["id"]))
        active_hours = p["schedule"]["active_hours"]
    except Exception:  # noqa: BLE001 - 人格读取失败不影响阈值默认值
        active_hours = []
    hour = datetime.now().hour
    if active_hours and hour in active_hours:
        threshold -= 0.1
    elif active_hours:
        threshold += 0.1

    # 亲密度参与决策（PRD 3.2.2）。
    try:
        intimacy = sm.get_state(int(ai_profile_row["id"])).intimacy
    except Exception:  # noqa: BLE001 - 状态读不到就按中性处理
        intimacy = 0.5
    threshold += sm.threshold_shift_from_intimacy(intimacy)
    return max(THRESHOLD_FLOOR, min(THRESHOLD_CEILING, threshold))

def can_proactive(ai_profile_row) -> tuple[bool, str]:
    """费用控制 + 冷却 + 「他平时几点会主动找人」检查。

        「主动发消息的时间参照人格聊天记录中的朋友是什么时候主动发消息的，
          AI 学习到之后就按照这个来主动发消息，每天最多发主动 5 次」

    所以这里多了一道**时段闸门**：人格里学到了朋友主动开口的钟点
    （`personality_data.proactive_style.proactive_hours`），当前小时不在其中就
    不开口。没学到（人格里是空数组）时**不拦** —— 不能因为用户没导人格就
    让 AI 永远不主动，那就回到用户抱怨的「被关了」状态了。

    日上限 5 次（PRD 3.2.9）与冷却时间照旧，一并用上。
    """
    today = _today()
    if ai_profile_row["proactive_count_date"] == today and int(
        ai_profile_row["proactive_count_today"]
    ) >= PROACTIVE_DAILY_LIMIT:
        return False, f"今日主动发起已达上限 {PROACTIVE_DAILY_LIMIT} 次"

    last = ai_profile_row["last_proactive_at"]
    since = _seconds_since(last) if last else None
    if since is not None and since < PROACTIVE_COOLDOWN_SECONDS:
        return False, f"距上次主动发起仅 {int(since)} 秒，未过冷却"

    # 时段闸门：只在他平时会主动找人的钟点开口。
    hours = _proactive_hours(ai_profile_row)
    if hours:
        hour = datetime.now().hour
        # 允许前后各 1 小时的余量：记录里的样本总是稀疏的，
        # 卡死在整点会让「他晚上 11 点会找我」变成「只有 23:00 那一分钟能开口」。
        near = {(h + delta) % 24 for h in hours for delta in (-1, 0, 1)}
        if hour not in near:
            return False, f"当前 {hour} 点不在他平时主动开口的时段 {hours}"
    return True, ""

def _proactive_hours(ai_profile_row) -> list[int]:
    """人格里学到的「朋友主动开口钟点」。读不到就返回空列表（不拦）。"""
    try:
        p = persona.load_personality(int(ai_profile_row["id"]))
        style = p.get("proactive_style") or {}
        return [int(h) for h in (style.get("proactive_hours") or []) if 0 <= int(h) <= 23]
    except Exception:  # noqa: BLE001 - 人格读不到就不拦
        return []

# ---------------------------------------------------------------- 单次反思

async def evaluate(
    ai_profile_row,
    conversation_id: int,
    *,
    user_id: int = DEFAULT_USER_ID,
    idle_seconds_override: Optional[float] = None,
) -> ReflectionResult:
    """调用轻量模型评估发起意愿分。

    :param idle_seconds_override: 这次评估当作「已经静默了多久」。测试和
        调参时用；不传就按真实静默时间算。
    """
    ai_profile_id = int(ai_profile_row["id"])
    state = sm.get_state(ai_profile_id)
    persona_data = persona.load_personality(ai_profile_id)
    summary_line = persona.personality_prompt(persona_data, ai_profile_row["name"]).split("\n")
    personality_summary = "\n".join(summary_line[:12])

    idle = idle_seconds_override
    if idle is None:
        idle = idle_seconds(conversation_id)
    messages = prompt_builder.build_reflection_prompt(
        personality_summary=personality_summary,
        emotion=state.emotion,
        energy=state.energy,
        intimacy=state.intimacy,
        recent_conversation=mem.conversation_history_text(conversation_id, USER_DISPLAY),
        current_time=db.now_iso(),
        # 把「静默了多久」喂给模型（2026-09-29 第十轮）：
        # 不给这个数时它没有时间尺度，一律当成「刚聊过」，实测固定给 0.25，
        # 于是永远不会主动开口。见 build_reflection_prompt 的说明。
        idle_minutes=None if idle is None else int(idle // 60),
    )

    raw = await api_pool.chat(
        messages,
        role="judge",
        user_id=user_id,
        temperature=0.4,
        # 2026-09-28 第九轮：带上档案 id，用会话设置里那份 Key 做意愿评估。
        # 不传就只能查全局池（空表）→ 主动开口每次都被判「没配 API」。
        ai_profile_id=ai_profile_id,
    )
    try:
        return ReflectionResult.model_validate(_extract_json(raw))
    except Exception as exc:  # noqa: BLE001 - 解析失败按“不想说话”处理
        logger.warning("反思结果解析失败：%s（原始：%s）", exc, (raw or "")[:200])
        return ReflectionResult(willingness=0.0, reason="解析失败", topic="")

async def reflect_once(
    *,
    conversation_id: int,
    ai_profile_row,
    user_id: int = DEFAULT_USER_ID,
    idle_seconds_override: Optional[float] = None,
    willingness_override: Optional[float] = None,
    topic_override: Optional[str] = None,
) -> dict:
    """对单个会话做一次反思；满足条件则生成主动发起消息。

    :param idle_seconds_override: 测试用，绕过真实静默时间计算。
    :param willingness_override: 测试用，绕过模型评估。
    :return: 结果字典，便于测试与日志。
    """
    idle = idle_seconds_override
    if idle is None:
        idle = idle_seconds(conversation_id)
    if idle is None or idle < REFLECTION_IDLE_SECONDS:
        return {"opened": False, "reason": "用户还在活跃，静默时间不足"}

    allowed, why = can_proactive(ai_profile_row)
    if not allowed:
        return {"opened": False, "reason": why}

    if willingness_override is None:
        result = await evaluate(ai_profile_row, conversation_id, user_id=user_id)
    else:
        result = ReflectionResult(
            willingness=max(0.0, min(1.0, willingness_override)),
            reason="override",
            topic=topic_override or "",
        )

    threshold = _threshold_for(ai_profile_row)
    if result.willingness <= threshold:
        logger.info(
            "反思结束：意愿分 %.2f 未超过阈值 %.2f（%s）", result.willingness, threshold, result.reason
        )
        return {
            "opened": False,
            "reason": f"意愿分 {result.willingness:.2f} <= 阈值 {threshold:.2f}",
            "willingness": result.willingness,
        }

    # 意愿分超过阈值：调主模型生成实际内容（费用控制：只有这一步用主模型）
    import dispatcher  # 延迟导入，避免循环依赖

    await dispatcher.schedule_single_reply(
        user_id=user_id,
        conversation_id=conversation_id,
        user_text="",
        proactive_topic=result.topic,
        is_proactive=True,
    )
    db.record_proactive(int(ai_profile_row["id"]), _today())
    logger.info(
        "主动发起已生成：conversation=%s willingness=%.2f topic=%s",
        conversation_id,
        result.willingness,
        result.topic,
    )
    return {
        "opened": True,
        "willingness": result.willingness,
        "topic": result.topic,
        "reason": result.reason,
    }

async def reflect_all_once(*, user_id: Optional[int] = None) -> list[dict]:
    """对**所有账号**的启用中 AI 单聊会话各做一次反思。

    我都等烦了」）：

    改之前这里写的是 `user_id: int = DEFAULT_USER_ID`，也就是**只扫 user_id=1**。
    `_worker_loop` 调用时不传参，所以后台循环永远只看 1 号用户的会话。
    而多账号上线后真实用户在 0000（user_id=2）等，他们的会话从来没被扫过 ——
    AI 自然一次都不会主动开口。这是「一直不找」的直接原因。

    现在默认扫全部有会话的用户（`db.list_conversation_owner_ids()`），
    显式传 user_id 时只扫那一个（测试与单账号场景仍然可用）。

    :param user_id: 只反思这一个用户；None（默认）= 所有账号。
    """
    if not PROACTIVE_ENABLED:
        return []

    owner_ids = (
        [int(user_id)] if user_id is not None else db.list_conversation_owner_ids()
    )
    out: list[dict] = []
    for owner_id in owner_ids:
        for conv in db.list_conversations(owner_id):
            if conv["type"] != "single":
                continue
            participants = db.conversation_participants(int(conv["id"]))
            if not participants:
                continue
            ai_row = db.get_ai_profile(participants[0])
            if ai_row is None or int(ai_row["is_active"] or 0) != 1:
                continue
            try:
                out.append(
                    await reflect_once(
                        conversation_id=int(conv["id"]),
                        ai_profile_row=ai_row,
                        user_id=owner_id,
                    )
                )
            except Exception:  # noqa: BLE001 - 单个会话出错不能拖垮循环
                logger.exception("反思失败 conversation=%s", conv["id"])
    return out

# ---------------------------------------------------------------- 后台循环

async def _worker_loop() -> None:
    logger.info("后台反思循环已启动，间隔 %ss", REFLECTION_INTERVAL_SECONDS)
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            await reflect_all_once()
        except Exception:  # noqa: BLE001 - 轮询本身出错也不能退出
            logger.exception("反思循环异常")
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=REFLECTION_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("后台反思循环已停止")

def start() -> None:
    global _worker_task, _stop_event
    if _worker_task is not None and not _worker_task.done():
        return
    _stop_event = asyncio.Event()
    _worker_task = asyncio.create_task(_worker_loop())

async def stop() -> None:
    global _worker_task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _worker_task is not None:
        try:
            await asyncio.wait_for(_worker_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _worker_task.cancel()
        _worker_task = None
    _stop_event = None