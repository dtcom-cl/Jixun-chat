"""延迟队列 worker。

职责：按 的“数据库轮询 + asyncio worker”模式，
轮询 delayed_tasks 表中 status='pending' AND execute_at <= now() 的任务，
到期后把消息通过 WS 网关推送给客户端。

持久化与恢复：
- 任务全部落库，服务端重启不丢。
- 启动时把上次崩溃停在 'processing' 的任务放回 'pending'（db.recover_stuck_tasks）。
- 单条任务执行失败按 max_retries 重试，超过则置 'failed'（WS-001 错误码 5004 场景）。
"""
from __future__ import annotations

import asyncio
import json
import logging

import database as db
import ws_gateway as gw
from config import DELAY_QUEUE_POLL_INTERVAL_SECONDS, DEFAULT_USER_ID

logger = logging.getLogger("delay_queue")

_worker_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None

def _build_message_payload(task_row, payload: dict) -> dict:
    """把 delayed_tasks.payload 组装成 WS-001 的 message 结构。"""
    # 优先用 messages 表里的真实记录，保证与库中一致（含 timestamp）
    message_id = payload.get("message_id")
    msg_row = None
    if message_id:
        msg_row = db.get_message_by_msg_id(str(payload.get("msg_id"))) if payload.get("msg_id") else None
    if msg_row is not None:
        message = db.message_row_to_ws(msg_row)
        message["delay_seconds"] = int(payload.get("delay_seconds", 0) or 0)
        message["is_proactive"] = bool(payload.get("is_proactive", False))
        message["is_double_text"] = bool(payload.get("is_double_text", False))
        return message

    # 兜底：payload 里没有对应消息记录时，按 payload 现场组装
    return {
        "type": "message",
        "msg_id": payload.get("msg_id") or db.new_msg_id(),
        "conversation_id": task_row["conversation_id"],
        "sender_type": "ai",
        "sender_id": int(payload.get("ai_profile_id") or 0),
        "sender_name": payload.get("sender_name") or "AI",
        "sender_avatar": None,
        "content": payload.get("content") or [],
        "content_type": payload.get("content_type") or "text",
        "timestamp": db.now_iso(),
        "is_proactive": bool(payload.get("is_proactive", False)),
        "is_double_text": bool(payload.get("is_double_text", False)),
        "delay_seconds": int(payload.get("delay_seconds", 0) or 0),
    }

async def _execute_task(task_row) -> None:
    """执行一条到期任务。"""
    payload = json.loads(task_row["payload"] or "{}")
    task_type = task_row["task_type"]

    if task_type in ("send_message", "double_text", "proactive"):
        message = _build_message_payload(task_row, payload)
        # 2026-09-28 第八轮修正：收件人必须是**这条会话的主人**，不能写死 DEFAULT_USER_ID。
        #
        # 原来这里恒定 `push_message_to_user(DEFAULT_USER_ID, ...)`。
        # 多账号上线后这是真 bug：0001 阿伟的 AI 到点了，消息却被推给 user_id=1，
        # 而 1 号是账号 0000「乐」—— 也就是「串号」在推送侧的最后一环。
        # 库里消息没错（落在正确的 conversation 上），但推错了人。
        owner_id = _conversation_owner(int(task_row["conversation_id"]))
        pushed = await gw.push_message_to_user(owner_id, message)
        # 客户端不在线也算任务执行成功：消息已落库，重连 resume 时补发。
        # 未读 +1 由服务端统一记（客户端进聊天页时清零，PRD 3.1.6）。
        logger.info(
            "任务 %s 执行完成：type=%s msg_id=%s owner=%s 在线推送=%s",
            task_row["id"],
            task_type,
            message.get("msg_id"),
            owner_id,
            pushed,
        )
        db.touch_conversation(
            task_row["conversation_id"], message.get("timestamp") or db.now_iso(), unread_delta=1
        )
    elif task_type == "group_round":
        import group_chat

        await group_chat.continue_group_round(
            conversation_id=int(task_row["conversation_id"]),
            user_id=int(payload.get("user_id") or DEFAULT_USER_ID),
            round_index=int(payload.get("round_index") or 1),
        )
    else:
        raise ValueError(f"未知 task_type：{task_type}")

def _conversation_owner(conversation_id: int) -> int:
    """这条会话属于哪个用户。查不到就退回 DEFAULT_USER_ID（老库兜底）。"""
    row = db.get_conversation(conversation_id)
    if row is None:
        return DEFAULT_USER_ID
    try:
        return int(row["user_id"] or DEFAULT_USER_ID)
    except (KeyError, TypeError, ValueError):
        return DEFAULT_USER_ID

async def _run_once() -> bool:
    """取一条到期任务并执行。返回是否处理了任务。"""
    task_row = db.claim_due_task()
    if task_row is None:
        return False
    try:
        await _execute_task(task_row)
        db.finish_task(int(task_row["id"]))
    except Exception as exc:  # noqa: BLE001 - 失败要落库并继续跑，不能让 worker 挂掉
        logger.exception("任务 %s 执行失败", task_row["id"])
        db.fail_task(int(task_row["id"]), str(exc))
    return True

async def _worker_loop() -> None:
    logger.info("延迟队列 worker 已启动，轮询间隔 %.1fs", DELAY_QUEUE_POLL_INTERVAL_SECONDS)
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            processed = await _run_once()
        except Exception:  # noqa: BLE001 - 轮询本身出错也不能退出
            logger.exception("延迟队列轮询异常")
            processed = False
        if not processed:
            try:
                await asyncio.wait_for(
                    _stop_event.wait(), timeout=DELAY_QUEUE_POLL_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass
    logger.info("延迟队列 worker 已停止")

def start() -> None:
    """启动 worker（main.py 的 startup 事件调用）。"""
    global _worker_task, _stop_event
    if _worker_task is not None and not _worker_task.done():
        return
    recovered = db.recover_stuck_tasks()
    if recovered:
        logger.warning("恢复上次未完成的任务 %s 条（processing -> pending）", recovered)
    _stop_event = asyncio.Event()
    _worker_task = asyncio.create_task(_worker_loop())

async def stop() -> None:
    """停止 worker（main.py 的 shutdown 事件调用）。"""
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