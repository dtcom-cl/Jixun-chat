"""服务端入口（阶段1任务1.2；阶段2~4 扩展 HTTP 接口）。

职责：
- 启动 FastAPI，注册 WebSocket 路由 /ws。
- 初始化数据库与延迟队列 worker、后台状态衰减、后台反思循环。
- 提供客户端需要的 HTTP 接口：会话列表、历史消息、人格上传、AI 档案、API 配置。

- 接入层 / 4.2 调度层 / 4.3.1 状态机后台衰减
- 一、协议总则（地址 ws://{host}:{port}/ws）

启动命令（见 src/server/README.md）：
    uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel, Field

import accounts
import api_pool
import database as db
import delay_queue
import discovery
import group_chat
import memory as mem
import personality as persona
import reflection
import state_machine as sm
import ws_gateway as gw
from config import (
    ACCOUNTS_RESTORE_ON_START,
    AUTH_REQUIRED,
    DB_PATH,
    DEFAULT_AI_NAME,
    DEFAULT_AI_PROFILE_ID,
    DEFAULT_CONVERSATION_ID,
    DEFAULT_USER_ID,
    FIXED_REPLY_ENABLED,
    LOG_LEVEL,
    PROACTIVE_ENABLED,
    PURGE_LEGACY_DEFAULT_AI,
    USE_LLM,
    WS_PATH,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

# 后台状态衰减任务
_decay_task: Optional[asyncio.Task] = None

# ================================================================
# 身份认证（用户 2026-09-27 第六轮：多账号 + 禁止串数据）
# ================================================================
#
# 背景：在账号功能之前，所有接口都写死 `DEFAULT_USER_ID = 1`，而且**不校验归属** ——
# 知道 ai_profile_id 就能读/改别人的 API KEY。多账号上线前必须堵住这个洞。
#
# 做法（用户选定）：
# - 令牌走 `Authorization: Bearer <token>`，永久有效，只验签名 + 是否当前有效令牌；
# - **忽略客户端传来的 user_id**（传了也不认），身份只从令牌取；
# - 访问不属于自己的资源一律 404（不是 403 —— 不泄露“这个东西存在”）。

def _extract_bearer(authorization: Optional[str]) -> str:
    """从 `Authorization: Bearer xxx` 里取令牌；格式不对返回空串。"""
    text = (authorization or "").strip()
    if not text:
        return ""
    if text.lower().startswith("bearer "):
        return text[7:].strip()
    return text

async def current_user(
    authorization: Optional[str] = Header(default=None),
) -> Any:
    """FastAPI 依赖：把请求头里的令牌换成 `users` 行。

    这就是「同 ID 禁止多设备同时登录」的落点之一 —— 老设备的令牌不在库里，
    这里直接 401，客户端收到后会清掉本地凭证回到登录页。

    `AUTH_REQUIRED=0`（只在自动化测试里设）且没带令牌时，回落到默认用户，
    好让既有的 200 多项用例不用每条都造令牌；带了令牌仍然按令牌走，
    所以专测鉴权的用例照常有效。生产默认 AUTH_REQUIRED=1，没有这个后门。
    """
    token = _extract_bearer(authorization)
    if token:
        row = accounts.resolve_token(token)
        if row is None:
            raise HTTPException(
                status_code=401,
                detail="登录已失效，请重新登录",
                headers={"X-Error-Code": "unauthorized"},
            )
        return row
    if not AUTH_REQUIRED:
        return db.get_user(DEFAULT_USER_ID)
    raise HTTPException(
        status_code=401,
        detail="请先登录",
        headers={"X-Error-Code": "unauthorized"},
    )

def _uid(user: Any) -> int:
    """取当前用户的 `users.id`（业务表用的整数外键）。"""
    return int(user["id"])

def _owned_ai(user: Any, ai_profile_id: int):
    """取一个属于当前用户的 AI 档案；不属于或不存在都返回 None（调用方回 404）。"""
    row = db.get_ai_profile(ai_profile_id)
    if row is None or int(row["user_id"] or 0) != _uid(user):
        return None
    return row

def _owned_conversation(user: Any, conversation_id: int):
    """取一个属于当前用户的会话；不属于或不存在都返回 None。"""
    row = db.get_conversation(conversation_id)
    if row is None or int(row["user_id"] or 0) != _uid(user):
        return None
    return row

async def _decay_loop() -> None:
    """按 config.DECAY_INTERVAL_SECONDS 周期执行状态衰减。"""
    from config import DECAY_INTERVAL_SECONDS

    logger.info("后台状态衰减任务已启动，间隔 %ss", DECAY_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(DECAY_INTERVAL_SECONDS)
            await asyncio.to_thread(sm.decay_all)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 衰减失败不能拖垮服务端
            logger.exception("状态衰减任务异常")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：建表 -> 种子数据 -> 启动延迟队列/衰减/反思；关闭：全部停止。"""
    global _decay_task
    db.init_db()
    db.bootstrap_defaults(
        user_id=DEFAULT_USER_ID,
        username="owner",
        display_name="我",
        ai_profile_id=DEFAULT_AI_PROFILE_ID,
        ai_name=DEFAULT_AI_NAME,
        # 用户 2026-09-27 要求：像 QQ 一样首次进入没有任何会话，
        # 单聊要用户自己“加好友”才出现，所以生产启动不再自动建默认会话。
        with_default_conversation=False,
        # 用户 2026-09-27 第二次要求：“我登录进去直接就有个小真，把这个永久删掉
        # 再也不要重新出现”。因此生产启动连默认 AI 档案都不再预置。
        with_default_ai=False,
    )
    # 清理历史库里遗留的默认 AI「小真」及其消息/延迟任务（幂等）。
    # 不清这一步，老 data/app.db 一启动延迟队列就会继续替它推送消息。
    if PURGE_LEGACY_DEFAULT_AI:
        purged = db.purge_legacy_default_ai(DEFAULT_AI_NAME)
        if any(purged.values()):
            logger.info("已清理历史默认 AI「%s」：%s", DEFAULT_AI_NAME, purged)

    # 账号体系（用户 2026-09-27 第六轮）：
    # 1. 开发者账号 0000（昵称「乐」）自动建好，幂等；
    # 2. 如果 data/accounts/<ID>/ 里有 DB 里没有的账号，从明文文件夹回灌。
    #    这是用户要「换手机 / 删库重装也能拿回配置」的第二道保险。
    accounts.ensure_developer_account()
    if ACCOUNTS_RESTORE_ON_START:
        restored = accounts.restore_from_folders()
        if restored:
            logger.info("已从明文文件夹恢复账号：%s", ", ".join(restored))
    logger.info("数据库就绪：%s，表：%s", DB_PATH, ", ".join(db.list_tables()))
    delay_queue.start()
    # 2026-10-01 第二十八轮：局域网自动发现（手机在同一网络下可自动找到这台电脑）。
    # 纯本地 UDP 广播，不经过任何第三方、不需要账号；端口跟服务端一致
    # （run_server.py 通过 ZUICHENG_PORT 告诉它真实端口，默认 8000）。
    discovery.start(int(os.environ.get("ZUICHENG_PORT") or 8000))
    # 默认配置（USE_LLM=False）只启动延迟队列。
    # 状态衰减与反思循环仅在显式开启 LLM 管线时启动，
    # 避免默认运行时出现无关的后台任务与日志。
    if USE_LLM:
        _decay_task = asyncio.create_task(_decay_loop())
        reflection.start()
    else:
        logger.info("USE_LLM=False（阶段1模式）：跳过后台状态衰减与反思循环")
    try:
        yield
    finally:
        await reflection.stop()
        if _decay_task is not None:
            _decay_task.cancel()
            _decay_task = None
        await delay_queue.stop()
        await discovery.stop()
        logger.info("服务端已关闭")

app = FastAPI(title="极迅 服务端", version="1.0.0", lifespan=lifespan)

# ================================================================
# 健康检查
# ================================================================

@app.get("/health")
async def health() -> dict:
    """健康检查：确认服务端与数据库可用。阶段1辅助接口，不属于 WS 协议。"""
    return {
        "status": "ok",
        # 2026-09-27 第五轮起默认就是 "phase2+"：config.USE_LLM 默认 1，
        # 走真实大模型管线（显式设 DSH_USE_LLM=0 才退回简化模式）。
        "phase": "phase1" if not USE_LLM else "phase2+",
        # 给客户端与排障用：当前是否真的在走大模型、写死回复有没有被打开。
        # 用户明确要求“不要再用固定脚本”，所以这两个值要能一眼看到。
        "use_llm": USE_LLM,
        "fixed_reply_enabled": FIXED_REPLY_ENABLED,
        "proactive_enabled": PROACTIVE_ENABLED,
        "db_path": str(DB_PATH),
        "tables": db.list_tables(),
        "messages": db.count_messages(),
        "pending_tasks": db.count_tasks("pending"),
        "client_online": gw.manager.is_online(DEFAULT_USER_ID),
    }

# ================================================================
# 会话 / 消息（客户端 UI 用）
# ================================================================

def _conversation_to_json(row) -> dict[str, Any]:
    """会话行 -> 客户端 JSON。

    2026-09-27 第七轮新增两类内容：

    1. **人类好友会话**（type='human'）：对方是 users 行，标题/头像都从那里取，
    2. **头像与亲密度**：客户端原来气泡里的 AI 头像读的是全局内置头像，
       跟会话设置里换的那张毫无关系（用户实测「改了头像没反应」的根因之一）。
       现在把 avatar_url 一路带出去，客户端按会话渲染。
    """
    participants = db.conversation_participants(int(row["id"]))
    conv_type = str(row["type"] or "single")
    ai_name = ""
    ai_avatar = None
    ai_state: dict[str, Any] = {}
    if participants:
        ai_row = db.get_ai_profile(participants[0])
        if ai_row is not None:
            ai_name = ai_row["name"]
            ai_avatar = ai_row["avatar_url"]
            ai_state = sm.get_state(int(ai_row["id"])).as_dict()
    # PRD 3.4.1 / 3.4.2：群聊要显示每个 AI 各自的昵称与头像（客户端气泡用）
    members = []
    for ai_id in participants:
        member = db.get_ai_profile(ai_id)
        if member is None:
            continue
        members.append(
            {
                "ai_profile_id": int(member["id"]),
                "name": member["name"],
                "avatar_url": member["avatar_url"],
            }
        )

    peer_user_id = row["peer_user_id"] if "peer_user_id" in row.keys() else None
    peer_name = ""
    peer_avatar = None
    peer_online = False
    if conv_type == "human" and peer_user_id is not None:
        peer_row = db.get_user(int(peer_user_id))
        if peer_row is not None:
            peer_name = peer_row["display_name"] or ""
            peer_avatar = peer_row["avatar_url"] if "avatar_url" in peer_row.keys() else None
        peer_online = gw.manager.is_online(int(peer_user_id))

    last = db.last_message_in_conversation(int(row["id"]))
    preview = ""
    last_at = row["last_message_at"] or row["created_at"]
    if last is not None:
        preview = " ".join(db.content_to_array(last["content"]))[:40]
        last_at = last["timestamp"]

    intimacy = float(ai_state.get("intimacy", 0.0))
    return {
        "id": int(row["id"]),
        "type": conv_type,
        "title": row["title"] or ai_name or peer_name or "会话",
        "participants": participants,
        "members": members,
        "ai_name": ai_name,
        "ai_avatar": ai_avatar,
        # 人类好友会话（第七轮）
        "peer_user_id": peer_user_id,
        "peer_name": peer_name,
        "peer_avatar": peer_avatar,
        "peer_online": peer_online,
        "intimacy": round(intimacy, 4),
        "intimacy_percent": sm.intimacy_percent(intimacy),
        "intimacy_label": sm.intimacy_label(intimacy),
        "has_background": bool(db.conversation_background(int(row["id"]))),
        "last_message_at": last_at,
        "last_message_preview": preview,
        "unread_count": int(row["unread_count"] or 0),
    }

@app.get("/api/conversations")
async def api_conversations(user: Any = Depends(current_user)) -> dict:
    """会话列表（按最后一条消息时间倒序，PRD 3.1.6）。

    第六轮起身份只从令牌取；客户端再传 `user_id` 也会被忽略（堵串数据）。
    """
    rows = db.list_conversations(_uid(user))
    return {"conversations": [_conversation_to_json(r) for r in rows]}

@app.post("/api/conversations")
async def api_create_conversation(
    payload: dict, user: Any = Depends(current_user)
) -> dict:
    """创建会话（阶段4任务4.1 群聊 + PRD 3.4.1 群聊背景 + 用户 2026-09-27 的“加好友”单聊）。

    支持两种 type：
    - group：多人（多个 AI）群聊，title 必填，可带 background_records；
    - single：给某个 AI 建单聊（客户端“加好友”），title 用 AI 昵称，已存在则直接返回旧的。
    """
    conv_type = str(payload.get("type") or "group")
    user_id = _uid(user)
    participants = payload.get("participants") or []

    if conv_type == "single":
        if len(participants) != 1:
            raise HTTPException(status_code=400, detail="单聊会话只能有 1 个 AI")
        ai_profile_id = int(participants[0])
        # 只能给「自己的」AI 建单聊：别人的档案一律 404。
        ai_row = _owned_ai(user, ai_profile_id)
        if ai_row is None:
            raise HTTPException(status_code=404, detail="AI 不存在")
        # 幂等：同一个 AI 不重复建会话，直接返回已有的那条
        existing = db.find_single_conversation(user_id, ai_profile_id)
        conv_id = existing if existing is not None else db.create_conversation(
            user_id=user_id,
            conv_type="single",
            title=str(payload.get("title") or ai_row["name"]),
            participants=[ai_profile_id],
        )
        row = db.get_conversation(conv_id)
        return {
            "conversation": _conversation_to_json(row) if row else {"id": conv_id},
            "created": existing is None,
        }

    if conv_type == "human":
        # 人类好友会话（用户 2026-09-27 第七轮）。客户端一般不直接调它 ——
        # 加好友接口会顺手把会话建好；这里保留入口是为了「会话被误删后重建」。
        peer_user_id = payload.get("peer_user_id")
        if peer_user_id is None:
            raise HTTPException(status_code=400, detail="人类会话需要 peer_user_id")
        peer_row = db.get_user(int(peer_user_id))
        if peer_row is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        if int(peer_user_id) == user_id:
            raise HTTPException(status_code=400, detail="不能加自己为好友")
        import human_chat

        conv_id = human_chat.ensure_conversation(user_id, int(peer_user_id))
        row = db.get_conversation(conv_id)
        return {"conversation": _conversation_to_json(row) if row else {"id": conv_id}}

    if conv_type != "group":
        raise HTTPException(status_code=400, detail="type 只能是 group 或 single")

    title = str(payload.get("title") or "群聊")
    try:
        conv_id = group_chat.create_group(
            user_id=user_id,
            title=title,
            ai_profile_ids=[int(x) for x in participants],
            # PRD 3.4.1：可上传特定群聊的聊天记录文档作为上下文背景
            background_records=payload.get("background_records"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = db.get_conversation(conv_id)
    return {"conversation": _conversation_to_json(row) if row else {"id": conv_id}}

@app.get("/api/conversations/{conversation_id}/messages")
async def api_messages(
    conversation_id: int,
    limit: int = 50,
    before_id: Optional[int] = None,
    user: Any = Depends(current_user),
) -> dict:
    """历史消息（客户端进入聊天页时拉取）。别人的会话一律 404。

    2026-09-29 第十轮：**必须排除还没到点发出去的 AI 消息**。
    AI 的多段回复是先全部落库（timestamp 在将来）再由延迟队列逐段推送的，
    不过滤的话历史接口会一次把整段回复（3~7 条）全返回给客户端，
    用户一进聊天页就看到「很长一串」，延迟队列形同虚设。
    """
    conv = _owned_conversation(user, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    rows = db.list_messages(
        conversation_id,
        limit=max(1, min(200, limit)),
        before_id=before_id,
        exclude_unsent=True,
    )
    out = []
    for row in rows:
        item = db.message_row_to_ws(row)
        item["client_msg_id"] = row["client_msg_id"]
        item["state"] = row["state"]
        item["is_read"] = int(row["is_read"] or 0)
        out.append(item)
    return {"messages": out}

@app.post("/api/conversations/{conversation_id}/read")
async def api_mark_read(
    conversation_id: int, user: Any = Depends(current_user)
) -> dict:
    """进入聊天框后未读清零（PRD 3.1.6）。"""
    if _owned_conversation(user, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    count = db.mark_conversation_read(conversation_id)
    return {"conversation_id": conversation_id, "read_count": count}

@app.post("/api/conversations/{conversation_id}/clear")
async def api_clear_conversation(
    conversation_id: int, user: Any = Depends(current_user)
) -> dict:
    """清空聊天记录（用户 2026-10-04 要求：「人类用户和所有 AI 用户均有效」。

    对 AI 好友而言，这条请求之后它**不再记得**之前聊过什么 ——
    因为它的"记忆"就是从这几张表来的：短期窗口读 `messages`、
    中期摘要读 `summaries`、长期事实读 `memories`；
    另外还没发出去的 `delayed_tasks` 也要清，否则清空之后它还会"补发"
    几条几秒前排好的消息，看起来像没清干净。

    会话本身保留（标题 / AI 档案 / 亲密度 / 密码都不动）——
    要连人一起删，走「删好友」那条路。
    """
    row = _owned_conversation(user, conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    removed = db.clear_conversation_history(conversation_id)
    # 向量库里的长期记忆一并删除（不然只是"检索不到"，孤儿向量会一直留着）
    try:
        mem.forget_conversation(conversation_id)
    except Exception as exc:  # noqa: BLE001 - 向量库挂了也不能让清空失败
        logger.warning("清空会话 %s 的向量失败：%r", conversation_id, exc)
    # 明文镜像跟着刷新，否则 data/accounts/0000/friends/<名>/chat.jsonl 里
    # 还留着刚被"清空"的内容 —— 用户会以为没清干净（这是他亲自查过的地方）。
    try:
        accounts.mirror_account(str(user["username"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("清空后刷新账号镜像失败：%r", exc)

    logger.info(
        "清空聊天记录：conv=%s user=%s 删除 %s",
        conversation_id,
        user["username"],
        removed,
    )
    return {"conversation_id": conversation_id, "removed": removed}

# ================================================================
# AI 档案与人格
# ================================================================

class ChatLogUpload(BaseModel):
    """人格对话数据上传体（docs 未定义格式，见 persona_text.parse_persona_text）。

    2026-09-27 第七轮：客户端改传**文本**（`chat_log`），格式是

        用户：兄弟你在干啥？
        朋友：我在外面骑车呢，你要来不（3分钟后）

    每行「说话人：内容」，行尾括号是相对上一条的间隔，空行分隔每一次发言。
    `records` 结构化数组保留给内部调用与老客户端，客户端不再使用。
    """

    ai_profile_id: Optional[int] = None
    friend_name: str = ""
    chat_log: str = ""
    # PRD 5.2：人格导入功能要求用户确认已获得相关方授权。
    # 客户端必须先弹确认框，用户确认后带 authorized=True 再上传；服务端二次校验。
    authorized: bool = False

class AiProfileCreate(BaseModel):
    """新建 AI 档案（客户端“加好友”用）。docs 未定义该接口，属自行决定的补充：

    用户 2026-09-27 要求“像 QQ 一样，进去什么都没有，需要自己新建群聊和加好友”。
    加好友 = 先有一个联系人（AI 档案），再给它建单聊会话。只有默认的“小真”一个
    AI 时群聊抢麦没有意义，所以必须允许用户自己添加 AI。

    第六轮起**不再接受 user_id** —— AI 一定属于当前登录的账号（AI 不占号段）。
    """

    name: str

@app.post("/api/ai-profiles")
async def api_create_ai_profile(
    payload: AiProfileCreate, user: Any = Depends(current_user)
) -> dict:
    """新建一个 AI 档案（“加好友”第一步）。名称必填且不能重复。"""
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="AI 昵称不能为空")
    user_id = _uid(user)
    for row in db.list_ai_profiles(user_id):
        if str(row["name"]) == name:
            raise HTTPException(status_code=400, detail=f"已经有一个叫“{name}”的 AI 了")
    ai_id = db.create_ai_profile(user_id=user_id, name=name)
    row = db.get_ai_profile(ai_id)
    # AI 的档案/API/文件夹都跟着账号走，新建完立刻刷新明文镜像。
    accounts.mirror_account(str(user["username"]))
    return {
        "ai_profile": {
            "id": ai_id,
            "name": row["name"] if row else name,
            "avatar_url": row["avatar_url"] if row else None,
            "state": sm.get_state(ai_id).as_dict(),
            "personality_data": persona.load_personality(ai_id),
        }
    }

@app.get("/api/ai-profiles")
async def api_ai_profiles(user: Any = Depends(current_user)) -> dict:
    """当前账号下的全部 AI 档案（联系人）。"""
    out = []
    for row in db.list_ai_profiles(_uid(user)):
        out.append(
            {
                "id": int(row["id"]),
                "name": row["name"],
                "avatar_url": row["avatar_url"],
                "state": sm.get_state(int(row["id"])).as_dict(),
                "personality_data": persona.load_personality(int(row["id"])),
            }
        )
    return {"ai_profiles": out}

class AiProfileUpdate(BaseModel):
    """会话设置里能改的东西（用户 2026-09-27 明确限定为五项）。

    对方的头像、名称、API接口、APIKEY、人格对话数据——只有这五项，
    其他（软件名、状态机数值等）不允许从这个页面改。
    """

    name: Optional[str] = None
    # 旧字段：服务端路径（`/uploads/...`）或完整 http 地址。
    # 客户端**不应该**再传手机本地路径进来 —— 见 _is_usable_avatar_url。
    avatar_url: Optional[str] = None
    # 新字段（2026-09-28 第九轮）：头像图片本体（data URL 或纯 Base64）。
    # 为什么要加：客户端原来把「手机私有目录里的文件路径」当 avatar_url 传上来，
    # 服务端原样存下，结果 (1) 服务端根本没有这张图，(2) 客户端 AvatarCache
    # 拿这个路径拼上服务器地址去请求 -> 日志里那条
    # `GET /data/user/0/.../ai_avatar.jpg 404 Not Found`。
    # 现在改成把字节传上来，服务端落盘后回一个 /uploads/ 路径。
    avatar: Optional[str] = None
    avatar_ext: Optional[str] = None
    api_provider: Optional[str] = None
    api_base_url: Optional[str] = None
    api_model: Optional[str] = None
    # api_key 传字符串=改，传 null/不传=不改；传空串=清空。
    api_key: Optional[str] = None

def _is_usable_avatar_url(value: str) -> bool:
    """这个 avatar_url 服务端能不能用？

    只有两种情况是能用的：
    - `/uploads/...`：服务端自己存下来的相对路径；
    - `http(s)://...`：用户手填的外链。

    其余一律拒绝。踩过的坑：客户端把 Android 私有目录路径
    （`/data/user/0/com.jixun.jixun_client/code_cache/.../ai_avatar.jpg`）
    当 avatar_url 传上来，服务端照存，于是：服务端没有这张图；客户端
    AvatarCache 又给这个路径拼上服务器地址去请求，日志里刷 404。
    """
    text = (value or "").strip()
    if not text:
        return False
    return text.startswith("/uploads/") or text.startswith("http://") or text.startswith(
        "https://"
    )

def _ai_settings_public(ai_profile_id: int) -> dict:
    """会话设置的返回体：API Key 只回后四位，绝不回明文。"""
    row = db.get_ai_profile(ai_profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="AI 档案不存在")
    raw_key = (row["api_key"] or "") if "api_key" in row.keys() else ""
    key = api_pool.normalize_api_key(api_pool.decode_api_key(raw_key)) if raw_key else ""
    return {
        "id": ai_profile_id,
        "name": row["name"],
        "avatar_url": row["avatar_url"],
        "api_provider": (row["api_provider"] or "") if "api_provider" in row.keys() else "",
        "api_base_url": (row["api_base_url"] or "") if "api_base_url" in row.keys() else "",
        "api_model": (row["api_model"] or "") if "api_model" in row.keys() else "",
        "has_key": bool(key),
        "key_tail": key[-4:] if key else "",
        # 前端提示用：按 Key 格式能识别出的厂家（识别不出返回空串）。
        "detected_provider": api_pool.detect_provider(key) if key else "",
        "personality_data": persona.load_personality(ai_profile_id),
    }

class PersonalityDataUpdate(BaseModel):
    """会话设置页里「人格对话数据」编辑框提交的内容。

    两种都收：JSON 对象（前端 jsonDecode 后回传）或 JSON 字符串（原样粘贴）。
    收进来后统一交给 personality.normalize_personality 补齐缺失字段，
    避免用户手删掉某个键之后整份人格变成空壳。
    """

    personality_data: Any = None

@app.put("/api/ai-profiles/{ai_profile_id}/personality-data")
async def api_put_personality_data(
    ai_profile_id: int,
    payload: PersonalityDataUpdate,
    user: Any = Depends(current_user),
) -> dict:
    """直接覆盖某个 AI 的人格档案（PRD 3.3.4 的编辑入口）。

    与 POST .../personality 的分工：
    - POST 是「导入聊天记录 -> 蒸馏出人格」，用户给原料；
    - PUT  是「人格已经在这儿了，用户想自己改」，用户给成品。
    用户 2026-09-27 要求会话设置里能改「人格对话数据」，指的就是后者。
    """
    if _owned_ai(user, ai_profile_id) is None:
        raise HTTPException(status_code=404, detail="AI 档案不存在")

    raw = payload.personality_data
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"人格数据不是合法 JSON：{exc}"
            ) from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="人格数据必须是一个 JSON 对象")

    normalized = persona.normalize_personality(data)
    persona.save_personality(ai_profile_id, normalized)
    accounts.mirror_account(str(user["username"]))
    return {"personality_data": persona.load_personality(ai_profile_id)}

@app.get("/api/ai-profiles/{ai_profile_id}/settings")
async def api_get_ai_settings(
    ai_profile_id: int, user: Any = Depends(current_user)
) -> dict:
    """读取某个会话的设置（对方头像/名称/API/Key 尾部/人格数据）。

    第六轮起校验归属：别人的档案返回 404。以前只要知道 id 就能读到别人的
    Key 后四位，多账号之后那属于串数据。
    """
    if _owned_ai(user, ai_profile_id) is None:
        raise HTTPException(status_code=404, detail="AI 档案不存在")
    return {"settings": _ai_settings_public(ai_profile_id)}

@app.patch("/api/ai-profiles/{ai_profile_id}")
async def api_update_ai_profile(
    ai_profile_id: int,
    payload: AiProfileUpdate,
    user: Any = Depends(current_user),
) -> dict:
    """修改 AI 昵称 / 头像 / 这个 AI 自己的 API 配置。

    昵称与头像依据 PRD 3.1.2 自定义伪装；API 四件套依据用户 2026-09-27
    对「每个会话自己的设置」的要求。api_key 入库前先 Base64 编码（DB-001 3.8），
    并清掉用户粘贴时带进来的引号 / Bearer 前缀。
    """
    if _owned_ai(user, ai_profile_id) is None:
        raise HTTPException(status_code=404, detail="AI 档案不存在")
    encoded_key: Optional[str] = None
    if payload.api_key is not None:
        cleaned = api_pool.normalize_api_key(payload.api_key)
        # 清空 Key 时写入空串；否则 Base64 编码后入库。
        encoded_key = api_pool.encode_api_key(cleaned) if cleaned else ""

    # 头像：优先用上传的字节（新路径），其次用传进来的服务端/外链 URL。
    # 手机本地路径一律丢掉并记一条日志 —— 存下来只会变成一条永远 404 的记录。
    avatar_url: Optional[str] = None
    if payload.avatar is not None:
        ext = payload.avatar_ext or "jpg"
        avatar_url = (
            # ai 前缀把 AI 头像与账号头像分开放：账号 ID 是纯数字，
            # `ai7.jpg` 不可能和 `0007.jpg` 撞名。
            _save_avatar_base64(f"ai{ai_profile_id}", payload.avatar, ext)
            if payload.avatar.strip()
            else ""
        )
    elif payload.avatar_url is not None:
        if _is_usable_avatar_url(payload.avatar_url):
            avatar_url = payload.avatar_url.strip()
        else:
            logger.warning(
                "忽略不可用的头像地址（既不是 /uploads/ 也不是 http）：%s",
                payload.avatar_url[:120],
            )

    db.update_ai_profile_fields(
        ai_profile_id,
        name=payload.name,
        avatar_url=avatar_url,
        api_provider=payload.api_provider,
        api_base_url=payload.api_base_url,
        api_model=payload.api_model,
        api_key=encoded_key,
    )
    # 改完刷新明文镜像：data/accounts/<ID>/friends/<AI的ID>_<名字>/api.json 等
    accounts.mirror_account(str(user["username"]))
    return {"settings": _ai_settings_public(ai_profile_id)}

@app.delete("/api/ai-profiles/{ai_profile_id}")
async def api_delete_ai_profile(
    ai_profile_id: int, user: Any = Depends(current_user)
) -> dict:
    """删除一个 AI 好友（用户 2026-09-27 第六轮要求）。

    入口在「聊天页右上角 -> 聊天设置 -> 删除这位好友」，随时可删。

    删掉的东西（用户确认的口径）：
    - AI 档案、人格数据、它的头像文件；
    - 跟它的单聊会话，以及会话里的消息 / 延迟任务 / 摘要 / 记忆；
    - 它在别的会话里留下的记忆。

    瞬间消失了」）：**只把人从 participants 里移走，群和历史消息都留着**，
    否则会误伤群里其他 AI 的对话上下文；群成员被移空时才连带删群。

    幂等：再删一次返回 deleted=false，不报 500 —— 用户可能是手抖点了两次。
    """
    removed = db.delete_ai_profile_cascade(_uid(user), ai_profile_id)
    if removed is None:
        # 档案不存在（或不属于当前账号）-> 视为已经删过了，幂等返回。
        return {"deleted": False, "ai_profile_id": ai_profile_id}
    # 删完刷新明文镜像：那个好友文件夹会被一并清掉，不留残骸。
    accounts.mirror_account(str(user["username"]))
    logger.info(
        "已删除好友「%s」(id=%s)：会话 %s，退群 %s，消息 %s 条，任务 %s 条，记忆 %s 条",
        removed["name"],
        ai_profile_id,
        removed["conversations_deleted"],
        removed["groups_left"],
        removed["messages"],
        removed["tasks"],
        removed["memories"],
    )
    return {"deleted": True, **removed}

@app.post("/api/ai-profiles/{ai_profile_id}/personality")
async def api_upload_personality(
    ai_profile_id: int,
    payload: ChatLogUpload,
    user: Any = Depends(current_user),
) -> dict:
    """上传聊天记录并蒸馏人格（阶段2任务2.2 / 2.3 / 2.4；PRD 5.2 授权确认）。"""
    if _owned_ai(user, ai_profile_id) is None:
        raise HTTPException(status_code=404, detail="AI 档案不存在")
    # PRD 5.2：未确认已获得相关方授权，拒绝导入人格
    if not payload.authorized:
        raise HTTPException(
            status_code=403, detail="人格导入需要先确认已获得相关方授权（PRD 5.2）"
        )
    # 2026-09-27 第七轮：只认文本（`chat_log`），旧的结构化数组入口已彻底删除。
    raw: str = payload.chat_log or ""
    if not raw.strip():
        raise HTTPException(
            status_code=400,
            detail="聊天记录为空。请粘贴「用户：… / 朋友：…（3分钟后）」格式的对话",
        )
    try:
        result = await persona.upload_and_distill(
            ai_profile_id=ai_profile_id,
            raw_chat_log=raw,
            friend_name=payload.friend_name,
            user_id=_uid(user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 2026-09-29 第十二轮补：蒸馏完必须同步 data/accounts/ 明文镜像。
    #
    # 原来这里没有这一句，而 PUT /personality-data 有 —— 于是「导入并生成人格」
    # 之后账号文件夹里的 profile.json 还是旧人格，只有重启或下一次保存才对得上。
    # 排查用户那次「客户端报超时、服务端说成功」时正是靠这个时间差才看清真相：
    # 20:00:38 蒸馏成功（只写了库，没写镜像），20:00:45 镜像才被写 ——
    # 那一下不是蒸馏写的，是用户超时后点了「保存」，把编辑框里**旧的**人格
    # 覆盖了回去（见客户端 _longTimeout 的注释）。两边都补上，才不会再出现
    # 「库里的人格和账号文件夹里的人格不一样」。
    accounts.mirror_account(str(user["username"]))
    return result

# ================================================================
# API 池配置
# ================================================================

class ApiConfigIn(BaseModel):
    provider: str
    api_key: str = ""
    model_name: str = ""
    base_url: str = ""
    role: str = "chat"
    priority: int = 0
    is_primary: int = 0
    api_id: Optional[int] = None

@app.get("/api/api-pool")
async def api_list_api_pool(user: Any = Depends(current_user)) -> dict:
    """当前账号的全局 API 池。第六轮起 user_id 只从令牌取，前端传的忽略。"""
    return {"configs": api_pool.list_configs_public(_uid(user))}

@app.post("/api/api-pool")
async def api_save_api_pool(
    payload: ApiConfigIn, user: Any = Depends(current_user)
) -> dict:
    """保存一条 API 配置。用户填一次即保存，下次不用重填。"""
    user_id = _uid(user)
    api_id = api_pool.save_api_config(
        user_id=user_id,
        provider=payload.provider,
        api_key=payload.api_key,
        model_name=payload.model_name,
        base_url=payload.base_url,
        role=payload.role,
        priority=payload.priority,
        is_primary=payload.is_primary,
        api_id=payload.api_id,
    )
    accounts.mirror_account(str(user["username"]))
    return {"id": api_id, "configs": api_pool.list_configs_public(user_id)}

@app.delete("/api/api-pool/{api_id}")
async def api_delete_api_pool(
    api_id: int, user: Any = Depends(current_user)
) -> dict:
    """删掉一条 API 配置。只能删自己账号名下的（别人的一律当不存在）。"""
    user_id = _uid(user)
    for row in db.list_api_configs(user_id=user_id):
        if int(row["id"]) == int(api_id):
            db.delete_api_config(api_id)
            accounts.mirror_account(str(user["username"]))
            return {"deleted": api_id}
    raise HTTPException(status_code=404, detail="API 配置不存在")

# ================================================================
# 人类好友（用户 2026-09-27 第七轮）
# ================================================================
#

class HumanFriendIn(BaseModel):
    """按 4 位账号加好友。"""

    account_id: str

@app.get("/api/friends/human")
async def api_list_human_friends(user: Any = Depends(current_user)) -> dict:
    """我的人类好友列表（含账号 ID、昵称、头像、在线状态）。"""
    out = []
    for row in db.list_friend_links(_uid(user)):
        out.append(
            {
                "user_id": int(row["id"]),
                "account_id": row["username"],
                "nickname": row["display_name"],
                "avatar_url": row["avatar_url"] if "avatar_url" in row.keys() else None,
                "online": gw.manager.is_online(int(row["id"])),
            }
        )
    return {"friends": out}

@app.post("/api/friends/human")
async def api_add_human_friend(
    payload: HumanFriendIn, user: Any = Depends(current_user)
) -> dict:
    """按账号加人类好友。

    用户明确「不需要对方同意，输入账号即双向生效」——
    所以这里直接建立双向关系并建好两边的会话，不等对方确认。

    """
    account_id = (payload.account_id or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="请输入对方的账号")
    peer_row = db.get_user_by_account(account_id)
    if peer_row is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    peer_user_id = int(peer_row["id"])
    if peer_user_id == _uid(user):
        raise HTTPException(status_code=400, detail="不能加自己为好友")

    created = db.add_friend_link(_uid(user), peer_user_id)
    import human_chat

    human_chat.ensure_conversation(_uid(user), peer_user_id)
    accounts.mirror_account(str(user["username"]))
    accounts.mirror_account(account_id)
    logger.info("人类好友已建立：%s <-> %s（新建=%s）", user["username"], account_id, created)
    return {
        "added": True,
        "created": created,
        "friend": {
            "user_id": peer_user_id,
            "account_id": peer_row["username"],
            "nickname": peer_row["display_name"],
            "avatar_url": peer_row["avatar_url"] if "avatar_url" in peer_row.keys() else None,
        },
    }

@app.delete("/api/friends/human/{peer_user_id}")
async def api_delete_human_friend(
    peer_user_id: int, user: Any = Depends(current_user)
) -> dict:
    """删人类好友（用户要求：只要有一方删除了双向都没了）。

    三步一起做，缺一不可：
    1. 删双向 friend_links（两边列表同时消失）；
    2. 删两边的人类会话及其消息（否则列表里会挂一条点不进去的对话）；
    3. 刷新两个账号的明文镜像文件夹。
    """
    peer_row = db.get_user(peer_user_id)
    removed = db.remove_friend_link(_uid(user), peer_user_id)
    convs = db.delete_human_conversations(_uid(user), peer_user_id)
    accounts.mirror_account(str(user["username"]))
    if peer_row is not None:
        accounts.mirror_account(str(peer_row["username"]))
    logger.info(
        "人类好友已删除：%s -/-> %s（关系行 %s，会话 %s）",
        user["username"],
        peer_row["username"] if peer_row is not None else peer_user_id,
        removed,
        convs,
    )
    return {"deleted": True, "links_removed": removed, "conversations_deleted": convs}

# ================================================================
# 账号：注册 / 登录 / 资料 / 同步（用户 2026-09-27 第六轮）
# ================================================================
#

class RegisterIn(BaseModel):
    """注册：头像 + 昵称 + 密码，三个都要（用户明确要求）。

    头像用 Base64 传（data URL 也接受）。为什么不走 multipart：
    项目一贯「能用标准库/少依赖就用」，而 FastAPI 收 multipart 要额外装 python-multipart。
    一张压缩后的头像只有几十 KB，Base64 之后仍然很小。
    """

    nickname: str
    password: str
    avatar: str = ""          # data URL 或纯 Base64
    avatar_ext: str = "jpg"

class LoginIn(BaseModel):
    account_id: str
    password: str

class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str

class AccountUpdateIn(BaseModel):
    """账号资料与各种自定义（用户要求全部存服务端）。

    None = 这一项不动；空串 = 清空。与 ai_profiles 的 PATCH 语义保持一致。
    """

    nickname: Optional[str] = None
    avatar: Optional[str] = None      # 新头像（Base64）；不传表示不换
    avatar_ext: Optional[str] = None
    app_name: Optional[str] = None
    icon_id: Optional[str] = None
    launcher_index: Optional[int] = None

_AVATAR_ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif"}

def _save_avatar_base64(account_id: str, raw: str, ext: str) -> str:
    """把 Base64 头像落成服务端文件，返回可访问的 URL 路径。

    存到 `<DATA_DIR>/uploads/avatars/<账号ID>.<后缀>`。
    同一账号再换头像就覆盖同一个文件名 —— 头像不需要历史版本，
    覆盖能让 data 目录一直保持干净（用户要的是「随手能看」）。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("data:"):
        # data:image/jpeg;base64,xxxx
        head, _, payload = text.partition(",")
        if ";base64" not in head:
            raise HTTPException(status_code=400, detail="头像格式不对（只支持 Base64）")
        text = payload
        if "/" in head and ";" in head:
            mime = head[5:].split(";", 1)[0]
            guess = mime.rsplit("/", 1)[-1].lower()
            if guess in _AVATAR_ALLOWED_EXT:
                ext = guess
    suffix = (ext or "jpg").lower().lstrip(".")
    if suffix == "jpeg":
        suffix = "jpg"
    if suffix not in _AVATAR_ALLOWED_EXT:
        suffix = "jpg"
    try:
        blob = base64.b64decode(text, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"头像不是合法 Base64：{exc}") from exc
    if not blob:
        raise HTTPException(status_code=400, detail="头像内容为空")
    if len(blob) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="头像太大了（最多 5MB）")

    from config import UPLOAD_DIR

    folder = Path(UPLOAD_DIR) / "avatars"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{account_id}.{suffix}"
    # 换过后缀就删掉旧文件，避免同一账号留下两张头像。
    for old in folder.glob(f"{account_id}.*"):
        if old.name != target.name:
            try:
                old.unlink()
            except OSError:
                pass
    target.write_bytes(blob)
    return f"/uploads/avatars/{target.name}"

@app.get("/uploads/{file_path:path}")
async def serve_upload(file_path: str) -> Response:
    """把服务端上的头像等文件发给客户端（不鉴权：文件名含账号 ID，只读）。

    为什么不做鉴权：图片要在 Flutter 的 Image.network 里直接加载，
    而它不方便带 Authorization 头。头像本身不是敏感信息（用户自己的脸），
    路径里的账号 ID 也不可猜（这里用账号 ID 而非用户名）。
    真正敏感的 API KEY 走的是 /api/ai-profiles/{id}/settings，那条是鉴权的。
    """
    from config import UPLOAD_DIR

    root = Path(UPLOAD_DIR).resolve()
    target = (root / file_path).resolve()
    # 防目录穿越：解析后的路径必须仍在 uploads 里。
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    media = "image/jpeg"
    suffix = target.suffix.lower().lstrip(".")
    media = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(suffix, "application/octet-stream")
    return Response(content=target.read_bytes(), media_type=media)

@app.post("/api/auth/register")
async def api_register(payload: RegisterIn) -> dict:
    """注册：头像 + 昵称 + 密码 -> 分配 4 位账号 ID + 永久令牌。

    满员（0001~9999 都发完）返回 **409**，detail 就是「服务器已满」，
    客户端原样弹给用户。
    """
    try:
        accounts.validate_nickname(payload.nickname)
        accounts.validate_password(payload.password)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    # 先发号再存头像：这样头像文件名直接用账号 ID，不用事后改名。
    try:
        account_id = accounts._allocate_account_id()
    except accounts.ServerFullError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc

    avatar_url = ""
    if payload.avatar:
        avatar_url = _save_avatar_base64(account_id, payload.avatar, payload.avatar_ext)

    user_id = db.create_user_row(
        account_id=account_id,
        nickname=accounts.validate_nickname(payload.nickname),
        password=payload.password,
        avatar_url=avatar_url,
    )
    token = accounts.issue_token(account_id)
    db.set_user_token(account_id, token)
    accounts.mirror_account(account_id)
    logger.info("新账号注册：%s（%s）", account_id, payload.nickname)
    return {
        "account_id": account_id,
        "user_id": user_id,
        "nickname": payload.nickname.strip(),
        "token": token,
        "account": accounts.account_snapshot(account_id),
    }

@app.post("/api/auth/login")
async def api_login(payload: LoginIn) -> dict:
    """登录：ID + 密码 -> 令牌。

    客户端按钮转圈倒计时。账号/密码不对返回 401。
    """
    try:
        result = accounts.login(
            account_id=payload.account_id, password=payload.password
        )
    except accounts.LoginCooldownError as exc:
        raise HTTPException(
            status_code=429,
            detail=exc.message,
            headers={"Retry-After": str(int(exc.retry_after) + 1)},
        ) from exc
    except accounts.AccountError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    return {**result, "account": accounts.account_snapshot(result["account_id"])}

@app.post("/api/auth/logout")
async def api_logout(user: Any = Depends(current_user)) -> dict:
    """退出登录：服务端忘掉这个令牌，旧令牌立刻失效。"""
    accounts.logout(str(user["username"]))
    return {"logged_out": True}

@app.get("/api/auth/me")
async def api_me(user: Any = Depends(current_user)) -> dict:
    """当前账号资料 + 全部自定义项（软件名称、图标、桌面身份等）。"""
    return {"account": accounts.account_snapshot(str(user["username"]))}

@app.patch("/api/auth/me")
async def api_update_me(
    payload: AccountUpdateIn, user: Any = Depends(current_user)
) -> dict:
    """改昵称 / 头像 / 软件名称 / 软件图标 / 桌面图标身份（全部存服务端）。"""
    account_id = str(user["username"])
    avatar_url: Optional[str] = None
    if payload.avatar is not None:
        ext = payload.avatar_ext or "jpg"
        avatar_url = (
            _save_avatar_base64(account_id, payload.avatar, ext)
            if payload.avatar.strip()
            else ""
        )
    nickname: Optional[str] = None
    if payload.nickname is not None:
        try:
            nickname = accounts.validate_nickname(payload.nickname)
        except accounts.AccountError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
    db.update_user_account(
        account_id,
        display_name=nickname,
        avatar_url=avatar_url,
        app_name=payload.app_name,
        icon_id=payload.icon_id,
        launcher_index=payload.launcher_index,
    )
    accounts.mirror_account(account_id)
    return {"account": accounts.account_snapshot(account_id)}

@app.post("/api/auth/password")
async def api_change_password(
    payload: ChangePasswordIn, user: Any = Depends(current_user)
) -> dict:
    """改密码：必须先验旧密码，否则拿到令牌就能把别人密码改掉。"""
    try:
        accounts.change_password(
            account_id=str(user["username"]),
            old_password=payload.old_password,
            new_password=payload.new_password,
        )
    except accounts.LoginCooldownError as exc:
        raise HTTPException(
            status_code=429,
            detail=exc.message,
            headers={"Retry-After": str(int(exc.retry_after) + 1)},
        ) from exc
    except accounts.AccountError as exc:
        status_code = 401 if exc.code == "bad_credentials" else 400
        raise HTTPException(status_code=status_code, detail=exc.message) from exc
    return {"changed": True}

@app.get("/api/auth/sync")
async def api_sync(user: Any = Depends(current_user)) -> dict:
    """登录后一次性拉全量：账号资料 + 会话 + 好友 + 每个好友的设置 + 消息。

    用户要的是「换手机删软件重装，登录上还是之前的配置」，所以这里
    把客户端首屏需要的一切一次给全，避免开机连打十几个请求。

    注意：**不含 API KEY 明文**（只有 has_key / key_tail），
    与 /api/ai-profiles/{id}/settings 的口径一致。
    """
    account_id = str(user["username"])
    user_id = _uid(user)
    friends = []
    for row in db.list_ai_profiles(user_id):
        ai_id = int(row["id"])
        friends.append(
            {
                "id": ai_id,
                "name": row["name"],
                "avatar_url": row["avatar_url"],
                "state": sm.get_state(ai_id).as_dict(),
                "personality_data": persona.load_personality(ai_id),
                "settings": _ai_settings_public(ai_id),
            }
        )
    conversations = [_conversation_to_json(r) for r in db.list_conversations(user_id)]
    # 人类好友（第七轮）：一起给全，客户端首屏就能把两类联系人都画出来。
    humans = []
    for row in db.list_friend_links(user_id):
        humans.append(
            {
                "user_id": int(row["id"]),
                "account_id": row["username"],
                "nickname": row["display_name"],
                "avatar_url": row["avatar_url"] if "avatar_url" in row.keys() else None,
                "online": gw.manager.is_online(int(row["id"])),
            }
        )
    return {
        "account": accounts.account_snapshot(account_id),
        "ai_profiles": friends,
        "human_friends": humans,
        "conversations": conversations,
    }

# ================================================================
# WebSocket
# ================================================================

@app.websocket(WS_PATH)
async def websocket_endpoint(websocket: WebSocket, token: str = "") -> None:
    """WebSocket 入口。

    第六轮起要鉴权：令牌走**连接地址上的查询参数**（`?token=xxx`），
    因为 WS-001 把消息类型写死成 7 种，不能为了登录再加第 8 种。
    验不过直接以 1008 关闭，客户端会提示去登录。
    """
    row = accounts.resolve_token(token)
    if row is None:
        await websocket.close(code=1008)
        logger.warning("WebSocket 拒绝连接：令牌无效")
        return
    await gw.handle_connection(websocket, user_id=int(row["id"]))