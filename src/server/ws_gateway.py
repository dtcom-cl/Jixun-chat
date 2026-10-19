"""WebSocket 网关。

职责：管理客户端连接、收发 7 种协议消息、心跳保活、ACK、断线补发。

依据文档：（只实现文档定义的 7 种消息类型，不新增）。

分工：
- ws_gateway.py：连接生命周期 + 协议收发 + 心跳 + resume 补发 + ack
- dispatcher.py：收到 send 之后的业务处理（写库、生成回复、放延迟队列）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

import api_pool
import database as db
from config import (
    DEFAULT_CONVERSATION_ID,
    DEFAULT_USER_ID,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS,
)

logger = logging.getLogger("ws_gateway")

# 错误码
ERR_CONVERSATION_NOT_FOUND = 4001
ERR_EMPTY_CONTENT = 4002
ERR_BAD_FORMAT = 4003
ERR_DUPLICATE_CLIENT_MSG_ID = 4004
ERR_INTERNAL = 5001
# 5002 = AI API 调用失败。文档原意是“服务端自动切换，
# 客户端无感知”，指的是**池子里还有下一条可切**的情况，那时确实不该打扰用户。
ERR_AI_NOT_CONFIGURED = 5002

class ConnectionManager:
    """维护当前在线的客户端连接（本项目仅供个人使用，实际只有一个连接）。

    连接对象同时保存“最后收到客户端心跳的时间”和“最后一条送达客户端的 msg_id”，
    用于心跳超时检测和断线补发。
    """

    def __init__(self) -> None:
        self._conns: dict[int, WebSocket] = {}
        self._last_heartbeat: dict[int, float] = {}
        self._lock = asyncio.Lock()

    async def add(self, user_id: int, ws: WebSocket) -> None:
        async with self._lock:
            old = self._conns.get(user_id)
            self._conns[user_id] = ws
            self._last_heartbeat[user_id] = asyncio.get_running_loop().time()
        if old is not None and old is not ws:
            # 同一用户重复连接：关闭旧连接，保留新连接
            try:
                await old.close(code=1000)
            except Exception:  # noqa: BLE001 - 关闭旧连接失败不影响新连接
                pass

    async def remove(self, user_id: int, ws: WebSocket) -> None:
        async with self._lock:
            if self._conns.get(user_id) is ws:
                self._conns.pop(user_id, None)
                self._last_heartbeat.pop(user_id, None)

    def get(self, user_id: int) -> Optional[WebSocket]:
        return self._conns.get(user_id)

    def is_online(self, user_id: int) -> bool:
        return user_id in self._conns

    def touch_heartbeat(self, user_id: int) -> None:
        self._last_heartbeat[user_id] = asyncio.get_running_loop().time()

    def seconds_since_heartbeat(self, user_id: int) -> float:
        t = self._last_heartbeat.get(user_id)
        if t is None:
            return 0.0
        return asyncio.get_running_loop().time() - t

    def all_online(self) -> list[int]:
        return list(self._conns.keys())

manager = ConnectionManager()

# ---------------------------------------------------------------- 发送工具

def _envelope(payload: dict[str, Any]) -> str:
    """补齐公共字段 timestamp（WS-001 三、公共字段）后序列化。"""
    if "timestamp" not in payload:
        payload["timestamp"] = db.now_iso()
    return json.dumps(payload, ensure_ascii=False)

async def send_json(ws: WebSocket, payload: dict[str, Any]) -> None:
    await ws.send_text(_envelope(payload))

async def send_error(
    ws: WebSocket,
    code: int,
    message: str,
    ref_msg_id: Optional[str] = None,
) -> None:
    """发送 error 消息（WS-001 3.8）。所有错误都记录日志。"""
    logger.warning("发送 error code=%s message=%s ref=%s", code, message, ref_msg_id)
    await send_json(
        ws,
        {
            "type": "error",
            "code": code,
            "message": message,
            "ref_msg_id": ref_msg_id,
        },
    )

async def push_to_user(user_id: int, payload: dict[str, Any]) -> bool:
    """向指定用户的在线连接推送消息。返回是否推送成功。

    延迟队列 worker 到期后调用这里把 message 推给客户端。
    不在线时返回 False，消息已持久化，等客户端 resume 时补发。
    """
    ws = manager.get(user_id)
    if ws is None:
        return False
    try:
        await send_json(ws, payload)
        return True
    except Exception as exc:  # noqa: BLE001 - 连接可能刚好断开
        logger.warning("推送给 user_id=%s 失败：%s", user_id, exc)
        return False

async def push_message_to_user(user_id: int, ws_message: dict[str, Any]) -> bool:
    """把 WS-001 的 message 结构推给客户端。"""
    return await push_to_user(user_id, ws_message)

# ---------------------------------------------------------------- 心跳

async def heartbeat_sender(user_id: int) -> None:
    """服务端每 30 秒发一次 heartbeat（WS-001 3.7）。"""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        ws = manager.get(user_id)
        if ws is None:
            return
        try:
            await send_json(ws, {"type": "heartbeat"})
        except Exception:  # noqa: BLE001 - 连接已断，交给接收循环清理
            return

async def heartbeat_watcher(user_id: int) -> None:
    """90 秒未收到客户端 heartbeat 视为断线（WS-001 3.7）。"""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        ws = manager.get(user_id)
        if ws is None:
            return
        if manager.seconds_since_heartbeat(user_id) > HEARTBEAT_TIMEOUT_SECONDS:
            logger.warning("user_id=%s 心跳超时，判定断线", user_id)
            try:
                await ws.close(code=1000)
            except Exception:  # noqa: BLE001
                pass
            return

# ---------------------------------------------------------------- 各类型处理

async def _handle_heartbeat(user_id: int) -> None:
    manager.touch_heartbeat(user_id)

async def _handle_ack(ws: WebSocket, msg_id: str) -> None:
    """ack：把对应消息标记为 delivered（WS-001 3.4）。"""
    ok = db.mark_delivered(msg_id)
    if not ok:
        logger.info("ack 未命中任何消息：msg_id=%s", msg_id)

async def _handle_resume(ws: WebSocket, user_id: int, last_msg_id: str) -> None:
    """resume：查询遗漏消息并通过 resume_missed 返回（WS-001 3.5 / 3.6）。"""
    missed = db.get_messages_after(user_id, last_msg_id or "")
    await send_json(
        ws,
        {
            "type": "resume_missed",
            "messages": missed,
        },
    )
    logger.info("resume 补发 %s 条消息（last_msg_id=%s）", len(missed), last_msg_id)

async def _handle_send(ws: WebSocket, user_id: int, payload: dict[str, Any]) -> None:
    """send：校验 -> 去重 -> 落库 -> 交给调度器（WS-001 3.2）。"""
    # 延迟导入，避免 ws_gateway <-> dispatcher 循环依赖
    from dispatcher import handle_user_send

    client_msg_id = payload.get("client_msg_id")
    conversation_id = payload.get("conversation_id")
    content = payload.get("content")
    content_type = payload.get("content_type") or "text"
    timestamp = payload.get("timestamp") or db.now_iso()

    # 1. 字段完整性
    if not client_msg_id or conversation_id is None or content is None:
        await send_error(ws, ERR_BAD_FORMAT, "消息格式错误：缺少必填字段", client_msg_id)
        return
    # 2. 内容非空
    if not isinstance(content, str) or content.strip() == "":
        await send_error(ws, ERR_EMPTY_CONTENT, "消息内容为空", client_msg_id)
        return
    # 3. 会话存在
    conversation = db.get_conversation(int(conversation_id))
    if conversation is None:
        await send_error(ws, ERR_CONVERSATION_NOT_FOUND, "会话不存在", client_msg_id)
        return

    # 4. client_msg_id 去重
    existing = db.get_message_by_client_msg_id(str(client_msg_id))
    if existing is not None:
        logger.info("client_msg_id 重复，忽略：%s", client_msg_id)
        await send_error(
            ws, ERR_DUPLICATE_CLIENT_MSG_ID, "client_msg_id 重复", str(client_msg_id)
        )
        return

    try:
        await handle_user_send(
            user_id=user_id,
            conversation_id=int(conversation_id),
            content=content,
            content_type=str(content_type),
            timestamp=str(timestamp),
            client_msg_id=str(client_msg_id),
        )
    except api_pool.LLMCallError as exc:
        logger.warning("AI 调用失败，明确回报客户端：%s", exc)
        await send_error(ws, ERR_AI_NOT_CONFIGURED, str(exc), str(client_msg_id))
    except Exception as exc:  # noqa: BLE001 - 兜底，避免单条消息把连接打断
        logger.exception("处理 send 失败")
        await send_error(ws, ERR_INTERNAL, f"服务端内部错误：{exc}", str(client_msg_id))

# ---------------------------------------------------------------- 连接入口

async def handle_connection(ws: WebSocket, user_id: int = DEFAULT_USER_ID) -> None:
    """单个 WebSocket 连接的主循环（由 main.py 的 /ws 路由调用）。"""
    await ws.accept()
    await manager.add(user_id, ws)
    logger.info("客户端已连接：user_id=%s", user_id)

    sender_task = asyncio.create_task(heartbeat_sender(user_id))
    watcher_task = asyncio.create_task(heartbeat_watcher(user_id))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                payload = json.loads(raw)
            except ValueError:
                await send_error(ws, ERR_BAD_FORMAT, "消息格式错误：不是合法 JSON")
                continue
            if not isinstance(payload, dict):
                await send_error(ws, ERR_BAD_FORMAT, "消息格式错误：不是 JSON 对象")
                continue

            msg_type = payload.get("type")
            if msg_type == "heartbeat":
                await _handle_heartbeat(user_id)
            elif msg_type == "ack":
                msg_id = payload.get("msg_id")
                if not msg_id:
                    await send_error(ws, ERR_BAD_FORMAT, "ack 缺少 msg_id")
                else:
                    await _handle_ack(ws, str(msg_id))
            elif msg_type == "resume":
                await _handle_resume(ws, user_id, str(payload.get("last_msg_id") or ""))
            elif msg_type == "send":
                await _handle_send(ws, user_id, payload)
            else:
                # 客户端不应发送 message / resume_missed / error
                await send_error(
                    ws, ERR_BAD_FORMAT, f"未知或不允许的消息类型：{msg_type}"
                )
    except WebSocketDisconnect:
        logger.info("客户端断开：user_id=%s", user_id)
    except Exception:  # noqa: BLE001
        logger.exception("WebSocket 连接异常")
    finally:
        sender_task.cancel()
        watcher_task.cancel()
        await manager.remove(user_id, ws)
        logger.info("连接已清理：user_id=%s", user_id)