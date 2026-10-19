"""「催」机制（用户 2026-09-27 第七轮要求）。

    「延迟上线不保留，因为AI决定的延迟是它学习的朋友延迟方式。但是用户可以催，
      比如：你他妈的赶紧回我呀、说话、哑巴了吗、又不理我了之类的话，AI可以临时
      加一个补丁把之前决定过很久才回复的话提前回复。」

设计（三条按顺序判断）：

1. **有排队中的延迟任务** —— 全部提前到 now + 1~3 秒，立刻发。
   这是最常见的场景：AI 说「3 分钟后回你」，用户等不及了。
2. **没有排队任务**（AI 当时选了 ignore 已读不回）—— 带「用户在催」的提示
   重跑一次管线，让 AI 看着这句催促重新决定。
3. **连着催到 URGE_FORCE_REPLY_AFTER 次还不回** —— 强制回复，不再允许 ignore。

为什么判定用关键词而不是再调一次模型：
调模型判断「这算不算催」要多等 1~3 秒、多花一次 token，而误判的代价只是
提前回复一次（用户本来就在等回复）。所以宁可宽松：命中就触发。

误判风险也有兜底：只有当用户发的是**短消息**（<= 30 字）时才认关键词，
避免用户长篇讨论「说话的艺术」被当成催促。
"""
from __future__ import annotations

import logging
import random
from typing import Optional

import database as db
from config import (
    URGE_FORCE_REPLY_AFTER,
    URGE_KEYWORDS,
    URGE_MAX_ADVANCE_SECONDS,
    URGE_MIN_ADVANCE_SECONDS,
)

logger = logging.getLogger("urge")

# 关键词只在短消息里生效：长句多半在正常聊天，不是在催。
URGE_MAX_LENGTH = 30

# 连续催到这个次数还没回，就强制回复（不再允许 ignore）。
# 与 config.URGE_FORCE_REPLY_AFTER 一致，这里再取一次便于阅读。
FORCE_AFTER = URGE_FORCE_REPLY_AFTER


def is_urge(text: str) -> bool:
    """这句话是不是在催？（关键词命中 + 短消息）

    只做关键词匹配，不调模型 —— 理由见模块 docstring。
    """
    raw = (text or "").strip()
    if not raw or len(raw) > URGE_MAX_LENGTH:
        return False
    lowered = raw.lower()
    for word in URGE_KEYWORDS:
        if word.lower() in lowered:
            return True
    # 「在吗？」「喂？」这类：去掉标点后只剩两三个字且带疑问/呼唤语气。
    stripped = raw.rstrip("？?！!。.~～ ")
    if 0 < len(stripped) <= 4 and raw.endswith(("？", "?", "！", "!")):
        return True
    return False


def trailing_urge_count(conversation_id: int, limit: int = 10) -> int:
    """最近连续多少条用户消息是在催（遇到 AI 消息就停）。

    用户催一次不行会催第二次，这个计数决定要不要强制回复。
    """
    rows = db.list_messages(conversation_id, limit=limit)
    count = 0
    for row in reversed(rows):
        if row["sender_type"] != "user":
            break
        text = " ".join(db.content_to_array(row["content"]))
        if is_urge(text):
            count += 1
        else:
            break
    return count


async def handle_urge(
    *,
    user_id: int,
    conversation_id: int,
    user_text: str,
) -> Optional[str]:
    """处理一次催促。返回做了什么（给日志与测试看），没催则返回 None。

    返回值为以下之一：
    - "advanced"：把排队中的消息提前了
    - "rerun"   ：重跑了一次管线（AI 之前选了已读不回）
    - "forced"  ：连续催太多次，强制回复
    """
    if not is_urge(user_text):
        return None

    urge_count = trailing_urge_count(conversation_id)

    # 1. 有排队中的消息：提前发出来。
    pending = db.count_pending_tasks(conversation_id)
    if pending > 0:
        seconds = random.randint(URGE_MIN_ADVANCE_SECONDS, URGE_MAX_ADVANCE_SECONDS)
        moved = db.advance_pending_tasks(conversation_id, seconds)
        logger.info(
            "用户催促：把会话 %s 的 %s 条待发消息提前到 %s 秒后（第 %s 次催）",
            conversation_id,
            moved,
            seconds,
            urge_count,
        )
        if urge_count < FORCE_AFTER:
            return "advanced"
        # 持续催促时：提前之后仍需重跑一次，让模型针对这句催促作出反应，
        # 而不是把三分钟前准备好的旧话原样吐出来。
        await _rerun(user_id=user_id, conversation_id=conversation_id, urge_text=user_text)
        return "forced"

    # 2. 没有排队消息：只有在「用户已经连着发了几条、AI 一条都没回」时才当成
    #    已读不回重跑。否则返回 None，走正常管线。
    #
    #    这道闸必须有。「在吗」「喂」「说话」这些词单独出现时**是很普通的开场白**，
    #    不是催促；如果一见这几个字就重跑管线，用户每句「在吗」都会绕过正常流程，
    #    而且会因为 is_urge 把首响压到 3 秒内 —— 那就变成另一种固定行为了。
    #    真正的已读不回有个客观特征：用户的消息已经积了 2 条以上还没等到回复。
    if _trailing_user_messages(conversation_id) < 2:
        logger.info(
            "用户消息命中催促词，但会话 %s 只有一条未回消息，按正常消息处理", conversation_id
        )
        return None

    forced = urge_count >= FORCE_AFTER
    await _rerun(
        user_id=user_id,
        conversation_id=conversation_id,
        urge_text=user_text,
        force_reply=forced,
    )
    logger.info(
        "用户催促：会话 %s 无待发消息，重跑管线（第 %s 次催，强制回复=%s）",
        conversation_id,
        urge_count,
        forced,
    )
    return "forced" if forced else "rerun"


def _trailing_user_messages(conversation_id: int, limit: int = 10) -> int:
    """最近连续多少条用户消息还没等到 AI 回复（AI 一说话就归零）。"""
    rows = db.list_messages(conversation_id, limit=limit)
    count = 0
    for row in reversed(rows):
        if row["sender_type"] != "user":
            break
        count += 1
    return count


async def _rerun(
    *,
    user_id: int,
    conversation_id: int,
    urge_text: str,
    force_reply: bool = False,
) -> None:
    """带「用户在催」的提示重跑一次回复管线（延迟导入避免循环依赖）。"""
    import dispatcher

    await dispatcher.schedule_single_reply(
        user_id=user_id,
        conversation_id=conversation_id,
        user_text=urge_text,
        is_urge=True,
        force_reply=force_reply,
    )