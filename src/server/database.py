"""数据库模块。

职责：建立并管理 SQLite 数据库 data/app.db，提供 8 张表的建表与全部数据读写函数。

依据文档：（表、字段、类型、默认值、索引严格照抄，不增不减）。

实现选择：
- 使用 Python 标准库 sqlite3，不引入 SQLAlchemy。理由：阶段1只有 8 张表、读写简单，
  标准库依赖更少、更易排查。
- 每次操作打开一个连接并关闭，避免 SQLite 跨线程问题；开启 WAL 提升并发读写稳定性。
- 时间字段：建表 DEFAULT 按 原样写 CURRENT_TIMESTAMP；
  但写入时由 Python 显式传入 ISO8601 字符串，保证落库时间格式统一为 ISO8601。
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import string
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from config import DB_PATH, DELAY_TASK_MAX_RETRIES

logger = logging.getLogger("database")

TZ = timezone(timedelta(hours=8))  # 东八区，与 WS-001 timestamp 示例一致

# ---------------------------------------------------------------- 基础工具

def now_iso() -> str:
    """当前时间，ISO8601 字符串，如 2026-09-25T14:30:00+08:00。"""
    return datetime.now(TZ).isoformat(timespec="seconds")

def iso_in_seconds(seconds: float) -> str:
    """当前时间 + seconds 秒，ISO8601 字符串。"""
    return (datetime.now(TZ) + timedelta(seconds=seconds)).isoformat(timespec="seconds")

def _rand6() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))

def new_msg_id() -> str:
    """服务端消息ID，格式 msg_{毫秒时间戳}_{6位随机}。"""
    return f"msg_{int(time.time() * 1000)}_{_rand6()}"

def content_to_array(content: str) -> list:
    """把 messages.content 还原成 WS-001 的 content 数组。

    AI 的多段消息在 messages.content 里以 JSON 数组字符串存储（多段结构不丢失，
    断线补发时能原样还原）；用户消息是纯文本，包成单元素数组。
    该存储约定 未明确，属于自行决定的细节，已记录在任务记录中。
    """
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return [content]
    if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
        return parsed
    return [content]

# ---------------------------------------------------------------- 连接

def get_conn() -> sqlite3.Connection:
    """打开数据库连接。调用方负责 close()。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

# ---------------------------------------------------------------- 建表

SCHEMA_SQL: tuple[str, ...] = (
    # 3.1 users
    """
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT    UNIQUE NOT NULL,
        display_name  TEXT    NOT NULL,
        password_hash TEXT    NULL,
        created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
    # 3.2 conversations
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL REFERENCES users(id),
        type            TEXT    NOT NULL DEFAULT 'single',
        title           TEXT    NULL,
        participants    TEXT    NOT NULL DEFAULT '[]',
        last_message_at TEXT    NULL,
        unread_count    INTEGER NOT NULL DEFAULT 0,
        background_text TEXT    NOT NULL DEFAULT '',
        created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_conversations_last_message_at "
    "ON conversations(last_message_at DESC)",
    # 3.3 messages
    """
    CREATE TABLE IF NOT EXISTS messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        sender_type     TEXT    NOT NULL,
        sender_id       INTEGER NULL,
        msg_id          TEXT    UNIQUE NOT NULL,
        client_msg_id   TEXT    NULL,
        content         TEXT    NOT NULL,
        content_type    TEXT    NOT NULL DEFAULT 'text',
        timestamp       TEXT    NOT NULL,
        is_read         INTEGER NOT NULL DEFAULT 0,
        state           TEXT    NOT NULL DEFAULT 'sent',
        created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_conversation_id_timestamp "
    "ON messages(conversation_id, timestamp)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_msg_id ON messages(msg_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_state ON messages(state)",
    # 3.4 delayed_tasks
    """
    CREATE TABLE IF NOT EXISTS delayed_tasks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        message_id      INTEGER NULL REFERENCES messages(id),
        task_type       TEXT    NOT NULL,
        payload         TEXT    NOT NULL DEFAULT '{}',
        execute_at      TEXT    NOT NULL,
        status          TEXT    NOT NULL DEFAULT 'pending',
        retry_count     INTEGER NOT NULL DEFAULT 0,
        max_retries     INTEGER NOT NULL DEFAULT 3,
        last_error      TEXT    NULL,
        created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_delayed_tasks_execute_at_status "
    "ON delayed_tasks(execute_at, status)",
    "CREATE INDEX IF NOT EXISTS idx_delayed_tasks_conversation_id "
    "ON delayed_tasks(conversation_id)",
    # 3.5 ai_profiles
    """
    CREATE TABLE IF NOT EXISTS ai_profiles (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id               INTEGER NOT NULL REFERENCES users(id),
        name                  TEXT    NOT NULL,
        avatar_url            TEXT    NULL,
        personality_data      TEXT    NOT NULL DEFAULT '{}',
        state_emotion         REAL    NOT NULL DEFAULT 0.5,
        state_energy          REAL    NOT NULL DEFAULT 0.5,
        state_intimacy        REAL    NOT NULL DEFAULT 0.0,
        last_active_at        TEXT    NULL,
        last_proactive_at     TEXT    NULL,
        proactive_count_today INTEGER NOT NULL DEFAULT 0,
        proactive_count_date  TEXT    NULL,
        is_active             INTEGER NOT NULL DEFAULT 1,
        created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ai_profiles_user_id ON ai_profiles(user_id)",
    # 3.6 memories
    """
    CREATE TABLE IF NOT EXISTS memories (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ai_profile_id    INTEGER NOT NULL REFERENCES ai_profiles(id),
        conversation_id  INTEGER NOT NULL REFERENCES conversations(id),
        memory_type      TEXT    NOT NULL DEFAULT 'fact',
        content          TEXT    NOT NULL,
        vector_id        TEXT    NULL,
        importance       REAL    NOT NULL DEFAULT 0.5,
        last_accessed_at TEXT    NULL,
        created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memories_ai_profile_id ON memories(ai_profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_memories_conversation_id ON memories(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_memories_memory_type ON memories(memory_type)",
    # 3.7 summaries
    """
    CREATE TABLE IF NOT EXISTS summaries (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id  INTEGER NOT NULL REFERENCES conversations(id),
        summary_text     TEXT    NOT NULL,
        start_message_id INTEGER NOT NULL,
        end_message_id   INTEGER NOT NULL,
        token_count      INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_summaries_conversation_id ON summaries(conversation_id)",
    # 3.8 api_pool
    """
    CREATE TABLE IF NOT EXISTS api_pool (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL REFERENCES users(id),
        provider      TEXT    NOT NULL,
        model_name    TEXT    NOT NULL,
        api_key       TEXT    NOT NULL,
        base_url      TEXT    NULL,
        priority      INTEGER NOT NULL DEFAULT 0,
        is_active     INTEGER NOT NULL DEFAULT 1,
        is_primary    INTEGER NOT NULL DEFAULT 0,
        role          TEXT    NOT NULL DEFAULT 'chat',
        last_used_at  TEXT    NULL,
        failure_count INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_api_pool_user_id ON api_pool(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_api_pool_role ON api_pool(role)",
    "CREATE INDEX IF NOT EXISTS idx_api_pool_is_active ON api_pool(is_active)",
    # 3.9 friend_links（人类好友关系，用户 2026-09-27 第七轮）
    #
    # 没有这张表 —— 那时只考虑了「用户 vs AI」，没有真人互加。
    # 按“用户当面指令 > 文档”新增，并记录在验收清单。
    # 双向各存一行：你加了他，你一行、他一行，任一方删除时两行一起删，
    # 所以「只要有一方删除了双向都没了」是天然成立的，不需要额外同步逻辑。
    """
    CREATE TABLE IF NOT EXISTS friend_links (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL REFERENCES users(id),
        peer_user_id INTEGER NOT NULL REFERENCES users(id),
        created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, peer_user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_friend_links_user_id ON friend_links(user_id)",
)

EXPECTED_TABLES = (
    "users",
    "conversations",
    "messages",
    "delayed_tasks",
    "ai_profiles",
    "memories",
    "summaries",
    "api_pool",
    # 用户 2026-09-27 第七轮新增（人类好友）。 未定义，
    # 按“用户当面指令 > 文档”补上，见 SCHEMA_SQL 里的说明。
    "friend_links",
)

# 已交付库的结构增量（PRD 3.4.1 群聊背景）。
# CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，所以老库需要走一次 ALTER TABLE。
# 依据 冲突处理规则：PRD 3.4.1 要求群聊可带聊天记录背景，
# 未定义该字段，这里按“PRD 优先”新增并记录在验收清单。
SCHEMA_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("conversations", "background_text", "TEXT NOT NULL DEFAULT ''"),
    # 每个会话自己的 API 配置（用户 2026-09-27 要求：
    # 「每一个不同的聊天点进去后可以在右上角设置，这个设置里面只能设置
    #   对方的头像、名称、API接口、APIKEY、人格对话数据」）。
    # 的 ai_profiles 没有这几个字段，按“用户当面指令 > 文档”新增，
    # 走 ALTER TABLE 幂等迁移，不动老数据。
    ("ai_profiles", "api_provider", "TEXT NOT NULL DEFAULT ''"),
    ("ai_profiles", "api_base_url", "TEXT NOT NULL DEFAULT ''"),
    ("ai_profiles", "api_model", "TEXT NOT NULL DEFAULT ''"),
    ("ai_profiles", "api_key", "TEXT NOT NULL DEFAULT ''"),
    # 人类好友会话（用户 2026-09-27 第七轮：可以加真人好友）。
    #
    # 为什么不把人类 ID 塞进 participants：
    # participants 现在装的是 ai_profiles.id，而人类是 users.id，两套自增 ID
    # 一定会撞号（users.id=1 与 ai_profiles.id=1 都真实存在），混在一个数组里
    # 根本分不清是「AI 1 号」还是「用户 1 号」。所以人类对手单独一列。
    ("conversations", "peer_user_id", "INTEGER NULL"),
    # 账号体系（用户 2026-09-27 第六轮）。
    # 的 users 表只有 username/display_name/password_hash，
    # 这里**复用现有列**承担账号语义，只补真正缺的：
    #   username      -> 4 位账号 ID（本来就是 UNIQUE，天然满足「ID 不能重复」）
    #   display_name  -> 昵称（本来就没有 UNIQUE，天然满足「昵称可以重复」）
    #   password_hash -> 明文密码（用户明确要求明文存储，见验收文档风险说明）
    # 下面这些是文档里没有、但账号功能必须的：
    ("users", "avatar_url", "TEXT NOT NULL DEFAULT ''"),
    ("users", "token", "TEXT NOT NULL DEFAULT ''"),
    ("users", "app_name", "TEXT NOT NULL DEFAULT ''"),
    ("users", "icon_id", "TEXT NOT NULL DEFAULT ''"),
    ("users", "launcher_index", "INTEGER NOT NULL DEFAULT 0"),
)

def _migrate_schema(conn: sqlite3.Connection) -> None:
    """给已存在的老库补齐 SCHEMA_MIGRATIONS 里新增的列（幂等）。"""
    for table, column, definition in SCHEMA_MIGRATIONS:
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            continue  # 表还不存在，SCHEMA_SQL 会一次性建好
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def init_db() -> None:
    """建库建表。服务端每次启动调用，幂等。"""
    conn = get_conn()
    try:
        for sql in SCHEMA_SQL:
            conn.execute(sql)
        _migrate_schema(conn)
        conn.commit()
    finally:
        conn.close()

def list_tables() -> list[str]:
    """返回库中所有业务表名，供自检使用。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]
    finally:
        conn.close()

def bootstrap_defaults(
    user_id: int,
    username: str,
    display_name: str,
    ai_profile_id: int,
    ai_name: str,
    with_default_conversation: bool = True,
    with_default_ai: bool = True,
) -> None:
    """种子数据：默认用户（+ 可选的默认 AI 档案、默认单聊会话）。幂等。

    阶段1不做注册登录（PRD 5.3）。默认 AI 档案原本用于给写死回复提供
    sender_id / sender_name（WS-001 message 必填）。

    2026-09-27 第一次改动（用户：“像 QQ 一样进去什么都没有，自己加好友/建群”）：
    新增 with_default_conversation 开关，生产环境传 False。

    2026-09-27 第二次改动（用户：“我登录进去直接就有个小真，把这个永久删掉
    再也不要重新出现，并且我们还会自动发消息，太恶心了”）：
    再新增 with_default_ai 开关。生产环境传 False，**连 AI 档案都不再预置**，
    首次启动库里只有 users 一行；联系人与会话全部由用户自己加。
    测试与阶段1验收脚本仍用默认 True，继续依赖 ai_profile_id=1 / conversation_id=1，
    保证既有用例语义不变。
    """
    ts = now_iso()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO users "
            "(id, username, display_name, password_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (user_id, username, display_name, ts, ts),
        )
        if with_default_ai:
            conn.execute(
                "INSERT OR IGNORE INTO ai_profiles "
                "(id, user_id, name, personality_data, state_emotion, state_energy, "
                " state_intimacy, is_active, created_at, updated_at) "
                "VALUES (?, ?, ?, '{}', 0.5, 0.5, 0.0, 1, ?, ?)",
                (ai_profile_id, user_id, ai_name, ts, ts),
            )
        if with_default_conversation:
            conn.execute(
                "INSERT OR IGNORE INTO conversations "
                "(id, user_id, type, title, participants, unread_count, created_at, updated_at) "
                "VALUES (1, ?, 'single', NULL, ?, 0, ?, ?)",
                (user_id, json.dumps([ai_profile_id]), ts, ts),
            )
        conn.commit()
    finally:
        conn.close()

def purge_legacy_default_ai(ai_name: str = "小真") -> dict:
    """删除历史遗留的默认 AI（“小真”）及其全部痕迹。幂等，可反复调用。

    为什么需要这个函数：用户明确要求“把这个永久删掉再也不要重新出现”。
    只改 bootstrap_defaults 只能保证**新库**干净，已经跑过的 data/app.db 里
    仍然躺着 id=1 的小真、158 条消息和几十条 delayed_tasks —— 服务端一启动，
    延迟队列就会把到期的任务捞出来推送，用户看到的正是“它还在自动发消息”。
    所以必须做一次真正的数据清理。

    清理范围（只删与该 AI 相关的，不碰用户后来自己建的联系人）：
    1. 名字等于 ai_name 的 ai_profiles 行；
    2. 参与者**只有**这些 AI 的会话（single 与 group 都算），
       以及这些会话的消息 / 延迟任务 / 摘要 / 记忆；
    3. 指向已删除 AI 的延迟任务（防“幽灵消息”）。

    注意：按名字匹配是有意的——用户要的就是“叫小真这个东西永远消失”。
    该行为已登记在验收记录中。
    """
    conn = get_conn()
    removed = {"profiles": 0, "conversations": 0, "messages": 0, "tasks": 0}
    try:
        rows = conn.execute(
            "SELECT id FROM ai_profiles WHERE name = ?", (ai_name,)
        ).fetchall()
        dead_ai = [int(r["id"]) for r in rows]
        if not dead_ai:
            return removed

        placeholders = ",".join("?" for _ in dead_ai)
        dead_convs: list[int] = []
        for conv in conn.execute("SELECT id, participants FROM conversations").fetchall():
            try:
                parts = [int(x) for x in json.loads(conv["participants"] or "[]")]
            except (ValueError, TypeError):
                continue
            if parts and all(p in dead_ai for p in parts):
                dead_convs.append(int(conv["id"]))

        for cid in dead_convs:
            # 删除顺序必须服从外键：delayed_tasks.message_id -> messages.id，
            # messages.conversation_id -> conversations.id。
            # 先删消息会撞上 FOREIGN KEY constraint failed（实测踩过），
            # 所以先清延迟任务，再清消息，最后清会话。
            removed["tasks"] += conn.execute(
                "DELETE FROM delayed_tasks WHERE conversation_id = ?", (cid,)
            ).rowcount
            removed["messages"] += conn.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (cid,)
            ).rowcount
            conn.execute("DELETE FROM summaries WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM memories WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
            removed["conversations"] += 1

        # 兜底：这些 AI 名下的记忆一并清掉，避免残留事实被别的会话检索到。
        # 注意 delayed_tasks 没有 ai_profile_id 列，
        # 它的任务只能靠 conversation_id 定位，上面已按会话删过了。
        conn.execute(
            f"DELETE FROM memories WHERE ai_profile_id IN ({placeholders})", dead_ai
        )
        removed["profiles"] = conn.execute(
            f"DELETE FROM ai_profiles WHERE id IN ({placeholders})", dead_ai
        ).rowcount
        conn.commit()
        return removed
    finally:
        conn.close()

# ---------------------------------------------------------------- 会话

def get_conversation(conversation_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    finally:
        conn.close()

def get_ai_profile(ai_profile_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM ai_profiles WHERE id = ?", (ai_profile_id,)
        ).fetchone()
    finally:
        conn.close()

def touch_conversation(conversation_id: int, timestamp: str, unread_delta: int = 0) -> None:
    """更新会话最后消息时间与未读数（WS-001 3.2 服务端处理第5步）。"""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE conversations SET last_message_at = ?, "
            "unread_count = MAX(0, unread_count + ?), updated_at = ? WHERE id = ?",
            (timestamp, unread_delta, now_iso(), conversation_id),
        )
        conn.commit()
    finally:
        conn.close()

# ---------------------------------------------------------------- 消息

def insert_message(
    *,
    conversation_id: int,
    sender_type: str,
    sender_id: Optional[int],
    content: str,
    timestamp: str,
    msg_id: Optional[str] = None,
    client_msg_id: Optional[str] = None,
    content_type: str = "text",
    state: str = "sent",
) -> Optional[sqlite3.Row]:
    """写入一条消息。msg_id 冲突（重复推送）时返回 None，实现服务端去重。"""
    ts = now_iso()
    mid = msg_id or new_msg_id()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(conversation_id, sender_type, sender_id, msg_id, client_msg_id, content, "
            " content_type, timestamp, is_read, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (
                conversation_id,
                sender_type,
                sender_id,
                mid,
                client_msg_id,
                content,
                content_type,
                timestamp,
                state,
                ts,
                ts,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()

def get_message_by_msg_id(msg_id: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
    finally:
        conn.close()

def get_message_by_client_msg_id(client_msg_id: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM messages WHERE client_msg_id = ? ORDER BY id LIMIT 1",
            (client_msg_id,),
        ).fetchone()
    finally:
        conn.close()

def mark_delivered(msg_id: str) -> bool:
    """收到客户端 ack：state -> delivered（WS-001 3.4）。返回是否命中。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE messages SET state = 'delivered', updated_at = ? WHERE msg_id = ?",
            (now_iso(), msg_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def _sender_name(row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    if row["sender_type"] == "ai" and row["sender_id"]:
        r = conn.execute(
            "SELECT name FROM ai_profiles WHERE id = ?", (row["sender_id"],)
        ).fetchone()
        if r:
            return r["name"]
    if row["sender_type"] == "user" and row["sender_id"]:
        r = conn.execute(
            "SELECT display_name FROM users WHERE id = ?", (row["sender_id"],)
        ).fetchone()
        if r:
            return r["display_name"]
    return "系统"

def _is_from_peer(row: sqlite3.Row, conn: sqlite3.Connection) -> bool:
    """这条消息是不是「真人好友会话里对方发来的」。

    2026-09-30 第十四轮（用户实证）：「真人好友互相发消息的时候对面发的消息
    发过来居然变成了自己发的」。

    原因：真人好友会话里双方消息的 sender_type 都是 `user`
    （发送者那一侧由 dispatcher.handle_user_send 落库，对方那一侧由
    human_chat.deliver_human_message 复制一份），客户端原来只看
    `sender_type == "user"` 就当成「我发的」，于是把对面的消息画到了右边。

    这里给出一个与「收件人是谁」无关的权威标记：会话是 human、且发送者就是
    这条会话的 peer_user_id，那它一定是对方发来的。推送、补发（resume）、
    历史接口（/api/conversations/{id}/messages）共用这一处判定。
    """
    if row["sender_type"] != "user" or not row["sender_id"]:
        return False
    r = conn.execute(
        "SELECT type, peer_user_id FROM conversations WHERE id = ?",
        (row["conversation_id"],),
    ).fetchone()
    if r is None or r["type"] != "human" or r["peer_user_id"] is None:
        return False
    return int(r["peer_user_id"]) == int(row["sender_id"])

def _row_to_ws_message(row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
    """把 messages 行转换成 WS-001 的 message 结构（补发时逐条返回）。

    2026-09-27 第七轮：`sender_avatar` 以前恒为 None —— 协议里本来就有这个字段，
    只是从来没填过，导致客户端只能读全局内置头像，用户给某个 AI 换了头像
    气泡也不变。现在把 ai_profiles.avatar_url / users.avatar_url 带出去。
    """
    return {
        "type": "message",
        "msg_id": row["msg_id"],
        "conversation_id": row["conversation_id"],
        "sender_type": row["sender_type"],
        "sender_id": row["sender_id"] if row["sender_id"] is not None else 0,
        "sender_name": _sender_name(row, conn),
        "sender_avatar": _sender_avatar(row, conn),
        "content": content_to_array(row["content"]),
        "content_type": row["content_type"],
        "timestamp": row["timestamp"],
        # 第十四轮：真人好友对方发来的消息标一下，客户端据此画到左边。
        "from_peer": _is_from_peer(row, conn),
    }

def _sender_avatar(row: sqlite3.Row, conn: sqlite3.Connection) -> Optional[str]:
    """取发送者的头像 URL：AI 读 ai_profiles，人类读 users。"""
    if row["sender_id"] is None:
        return None
    if row["sender_type"] == "ai":
        r = conn.execute(
            "SELECT avatar_url FROM ai_profiles WHERE id = ?", (row["sender_id"],)
        ).fetchone()
        return r["avatar_url"] if r else None
    if row["sender_type"] == "user":
        r = conn.execute(
            "SELECT avatar_url FROM users WHERE id = ?", (row["sender_id"],)
        ).fetchone()
        return r["avatar_url"] if r else None
    return None

def message_row_to_ws(row: sqlite3.Row) -> dict[str, Any]:
    """行 -> WS message 转换（延迟队列 worker 推送时使用）。"""
    conn = get_conn()
    try:
        return _row_to_ws_message(row, conn)
    finally:
        conn.close()

def get_messages_after(user_id: int, last_msg_id: str) -> list[dict[str, Any]]:
    """断线补发查询（WS-001 3.5）。

    - last_msg_id 能在库中找到：返回该消息之后、属于该用户所有会话的消息。
    - last_msg_id 为空或找不到：返回该用户所有会话的全部消息（客户端首次连接/本地无记录）。
    """
    conn = get_conn()
    try:
        anchor = None
        if last_msg_id:
            anchor = conn.execute(
                "SELECT id FROM messages WHERE msg_id = ?", (last_msg_id,)
            ).fetchone()
        # 2026-09-29 第十轮：补发同样要排除「还没到点发出去」的 AI 消息。
        # 不排除的话，客户端一连上就会收到一堆「将来才该出现」的消息，
        # 延迟队列白做（用户实测的「回复很长一串」有一半来自这里）。
        unsent = (
            " AND NOT (m.state = 'pending' AND m.id IN ("
            "SELECT message_id FROM delayed_tasks "
            "WHERE status IN ('pending', 'processing') AND message_id IS NOT NULL))"
        )
        if anchor is None:
            rows = conn.execute(
                "SELECT m.* FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                f"WHERE c.user_id = ?{unsent} ORDER BY m.id ASC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT m.* FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                f"WHERE c.user_id = ? AND m.id > ?{unsent} ORDER BY m.id ASC",
                (user_id, anchor["id"]),
            ).fetchall()
        return [_row_to_ws_message(r, conn) for r in rows]
    finally:
        conn.close()

# ---------------------------------------------------------------- 延迟任务

def enqueue_task(
    *,
    conversation_id: int,
    task_type: str,
    payload: dict[str, Any],
    execute_at: str,
    message_id: Optional[int] = None,
    max_retries: int = DELAY_TASK_MAX_RETRIES,
) -> int:
    ts = now_iso()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO delayed_tasks "
            "(conversation_id, message_id, task_type, payload, execute_at, status, "
            " retry_count, max_retries, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
            (
                conversation_id,
                message_id,
                task_type,
                json.dumps(payload, ensure_ascii=False),
                execute_at,
                max_retries,
                ts,
                ts,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()

def claim_due_task() -> Optional[sqlite3.Row]:
    """取出一个到期任务并原子置为 processing。没有到期任务返回 None。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM delayed_tasks WHERE status = 'pending' AND execute_at <= ? "
            "ORDER BY execute_at ASC, id ASC LIMIT 1",
            (now_iso(),),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        cur = conn.execute(
            "UPDATE delayed_tasks SET status = 'processing', updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now_iso(), row["id"]),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return row
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()

def finish_task(task_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE delayed_tasks SET status = 'done', updated_at = ? WHERE id = ?",
            (now_iso(), task_id),
        )
        conn.commit()
    finally:
        conn.close()

def fail_task(task_id: int, error: str) -> None:
    """任务失败：未超过 max_retries 则回到 pending 重试，否则置 failed。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT retry_count, max_retries FROM delayed_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return
        retry = int(row["retry_count"]) + 1
        status = "pending" if retry < int(row["max_retries"]) else "failed"
        conn.execute(
            "UPDATE delayed_tasks SET status = ?, retry_count = ?, last_error = ?, "
            "updated_at = ? WHERE id = ?",
            (status, retry, error[:500], now_iso(), task_id),
        )
        conn.commit()
    finally:
        conn.close()

def recover_stuck_tasks() -> int:
    """服务端重启恢复：把上次崩溃时停在 processing 的任务放回 pending。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE delayed_tasks SET status = 'pending', updated_at = ? "
            "WHERE status = 'processing'",
            (now_iso(),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()

def count_pending_tasks(conversation_id: int) -> int:
    """这个会话还有多少条 AI 消息排在延迟队列里没发出去（催机制用）。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM delayed_tasks "
            "WHERE conversation_id = ? AND status IN ('pending', 'processing')",
            (conversation_id,),
        ).fetchone()
        return int(row["c"])
    finally:
        conn.close()

def pending_tasks_latest_execute_at(conversation_id: int) -> Optional[str]:
    """这个会话「还没发出去的」任务里最晚的 execute_at（没有则 None）。

    2026-09-30 第十四轮群聊多轮抢麦用：下一个人的插话必须排在**上一个人的话
    真正发出去之后**，否则屏幕上的顺序会乱（先看到插嘴，再看到被接的那句）。
    ISO8601 字符串带同一个 +08:00 偏移，字典序即时间序，可以直接 MAX()。
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(execute_at) AS t FROM delayed_tasks "
            "WHERE conversation_id = ? AND status IN ('pending', 'processing')",
            (conversation_id,),
        ).fetchone()
        return str(row["t"]) if row and row["t"] else None
    finally:
        conn.close()

def iso_after_seconds(base_iso: str, seconds: float) -> str:
    """在给定的 ISO8601 时间上加 seconds 秒，格式与 now_iso 一致。

    解析不了（老数据 / 空串）就退回「现在 + seconds」，不让调用方炸掉。
    """
    try:
        base = datetime.fromisoformat(base_iso)
    except (TypeError, ValueError):
        return iso_in_seconds(seconds)
    if base.tzinfo is None:
        base = base.replace(tzinfo=TZ)
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds")

def advance_pending_tasks(conversation_id: int, seconds: int = 1) -> int:
    """把该会话所有排队中的 AI 消息提前到 now + seconds 秒（用户「催」时用）。

    只改 execute_at，不动 payload 里的 delay_seconds —— 那个值是「AI 原本打算等多久」，
    排障时要能看出它本来想等 3 分钟、是被用户催出来的。

    同时把消息 timestamp 也一并改到新的执行时刻：客户端展示的是
    messages.timestamp，若只改任务不改消息，用户会看到一条「时间戳在未来」的
    消息（比如 22:00 收到标注 22:03 的气泡）。
    """
    if seconds < 0:
        seconds = 0
    new_at = iso_in_seconds(seconds)
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, message_id FROM delayed_tasks "
            "WHERE conversation_id = ? AND status IN ('pending', 'processing')",
            (conversation_id,),
        ).fetchall()
        if not rows:
            return 0
        conn.execute(
            "UPDATE delayed_tasks SET execute_at = ?, updated_at = ? "
            "WHERE conversation_id = ? AND status IN ('pending', 'processing')",
            (new_at, now_iso(), conversation_id),
        )
        for row in rows:
            if row["message_id"] is not None:
                conn.execute(
                    "UPDATE messages SET timestamp = ?, updated_at = ? WHERE id = ?",
                    (new_at, now_iso(), row["message_id"]),
                )
        conn.commit()
        return len(rows)
    finally:
        conn.close()

def cancel_pending_replies(conversation_id: int) -> int:
    """撤销这个会话里**还排着队、还没发出去**的 AI 回复（用户连发消息时用）。

        「AI会把我连续发的两条内容看成两条不同的内容，导致会回复很长一串，
          连续的定义就是在他还没有回复之前用户连续发送的两条内容」

    为什么会回复两串：用户发第一条时管线已经跑完、回复排进了延迟队列；
    发第二条时又跑一遍管线、又排一串。两条都到点就都推出去，
    用户看到的就是两串互不相干的回复。

    正确处理是：第二条到达时把**前一条触发的、还没发出去的**回复撤掉，
    用两条合并后的文本重新生成一次。所以这里既删任务、也删那条消息行 ——
    只删任务不删消息的话，那些行会立刻满足「已发出」的条件出现在历史里
    （历史接口靠 delayed_tasks 判断一条消息发没发出去，见
    UNSENT_MESSAGE_CONDITION），等于把取消掉的内容又漏给用户看。

    只撤销 status='pending'：status='processing' 说明 worker 已经取走正在推，
    这时候删行会造成「任务没了但消息已推送」的不一致，交给它发完更安全。

    2026-09-29 第十轮补修（单元测试抓到）：**必须先删 delayed_tasks、再删 messages**。
    `delayed_tasks.message_id` 外键指向 messages，反过来的顺序会撞
    FOREIGN KEY constraint failed（这正是本函数第一次实现时踩的坑，
    连发合并因此在生产里直接抛异常、用户发了第二条就报错）。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, message_id FROM delayed_tasks "
            "WHERE conversation_id = ? AND status = 'pending'",
            (conversation_id,),
        ).fetchall()
        if not rows:
            return 0
        msg_ids = [int(r["message_id"]) for r in rows if r["message_id"] is not None]
        # 先撤任务，解除 delayed_tasks.message_id 对 messages 的外键引用。
        conn.execute(
            "DELETE FROM delayed_tasks WHERE conversation_id = ? AND status = 'pending'",
            (conversation_id,),
        )
        if msg_ids:
            placeholders = ",".join("?" * len(msg_ids))
            conn.execute(
                f"DELETE FROM messages WHERE id IN ({placeholders}) AND state = 'pending'",
                tuple(msg_ids),
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()

def list_conversation_owner_ids() -> list[int]:
    """所有「有会话」的用户 id（后台反思循环要覆盖每一个账号）。

    2026-09-29 第十轮新增：主动发起的后台循环原来只扫 `DEFAULT_USER_ID`（=1），
    而多账号上线后真实用户是 0000（user_id=2）等，于是**他们的会话从来没被扫过**，
    AI 自然永远不会主动开口。用户实测「他为什么一直不找我」就是这个。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM conversations ORDER BY user_id"
        ).fetchall()
        return [int(r["user_id"]) for r in rows]
    finally:
        conn.close()

def count_tasks(status: Optional[str] = None) -> int:
    conn = get_conn()
    try:
        if status is None:
            row = conn.execute("SELECT COUNT(*) AS c FROM delayed_tasks").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM delayed_tasks WHERE status = ?", (status,)
            ).fetchone()
        return int(row["c"])
    finally:
        conn.close()

def count_messages() -> int:
    conn = get_conn()
    try:
        return int(conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"])
    finally:
        conn.close()

def iter_tables_and_columns() -> Iterable[tuple[str, list[str]]]:
    """自检用：返回每张表的列名列表。"""
    conn = get_conn()
    try:
        for table in EXPECTED_TABLES:
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            yield table, [c["name"] for c in cols]
    finally:
        conn.close()

# ================================================================
# 后续新增的数据访问函数。
# 只读写 已定义的字段，不新增字段、不改字段类型。
# ================================================================

# ---------------------------------------------------------------- 用户 / 会话

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()

# ---------------------------------------------------------------- 账号（第六轮）
#
# 账号 ID 存在 `users.username`（原本就是 TEXT UNIQUE），昵称存在 `users.display_name`
# （没有 UNIQUE 约束，正好满足用户「昵称可以重复」的要求）。
# 这样不用新建表，也不会影响既有外键（所有业务表都指向 users.id）。

def get_user_by_account(account_id: str) -> Optional[sqlite3.Row]:
    """按 4 位账号 ID 取用户行。找不到返回 None。"""
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE username = ?", (str(account_id),)
        ).fetchone()
    finally:
        conn.close()

def create_user_row(
    *,
    account_id: str,
    nickname: str,
    password: str,
    avatar_url: str = "",
) -> int:
    """建一个账号行，返回 `users.id`（业务表用的都是这个整数外键）。

    账号 ID 走 `username`。重复时 sqlite 会抛 UNIQUE 约束错误 ——
    但正常路径不会撞，因为号是 `allocate_next_account_id()` 在事务里发的。
    """
    ts = now_iso()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO users "
            "(username, display_name, password_hash, avatar_url, token, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '', ?, ?)",
            (str(account_id), nickname, password, avatar_url or "", ts, ts),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()

def allocate_next_account_id(min_number: int, max_number: int) -> Optional[int]:
    """发号：返回下一个可用的账号数字，发完了返回 None。

    为什么要在事务里做（BEGIN IMMEDIATE）：两个人几乎同时点注册，
    如果先查后写，会双双读到同一个「当前最大号」，然后一个插入失败、
    另一个拿到和别人一样的号。BEGIN IMMEDIATE 直接拿写锁，串行发号。

    只扫描纯数字的 username，所以默认的 `owner` 之类不会被误算进来；
    开发者账号 0000 解析成 0，小于 min_number（1），自然排除。
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT username FROM users").fetchall()
        used: set[int] = set()
        for row in rows:
            text = str(row["username"] or "")
            if text.isdigit():
                used.add(int(text))
        for number in range(int(min_number), int(max_number) + 1):
            if number not in used:
                conn.commit()
                return number
        conn.commit()
        return None
    finally:
        conn.close()

def update_user_account(
    account_id: str,
    *,
    display_name: Optional[str] = None,
    password: Optional[str] = None,
    avatar_url: Optional[str] = None,
    app_name: Optional[str] = None,
    icon_id: Optional[str] = None,
    launcher_index: Optional[int] = None,
) -> None:
    """改账号资料。传 None 表示这一项不动（与 ai_profiles 的 PATCH 语义一致）。"""
    sets, args = [], []
    for column, value in (
        ("display_name", display_name),
        ("password_hash", password),
        ("avatar_url", avatar_url),
        ("app_name", app_name),
        ("icon_id", icon_id),
        ("launcher_index", launcher_index),
    ):
        if value is not None:
            sets.append(f"{column} = ?")
            args.append(value)
    if not sets:
        return
    sets.append("updated_at = ?")
    args.append(now_iso())
    args.append(str(account_id))
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE username = ?", tuple(args)
        )
        conn.commit()
    finally:
        conn.close()

def set_user_token(account_id: str, token: str) -> None:
    """记住当前有效令牌。登录写新的、退出写空串。

    这就是「同 ID 禁止多设备同时登录」的实现：老设备的令牌和新值对不上，
    下一次请求就被判无效（见 accounts.resolve_token）。
    """
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET token = ?, updated_at = ? WHERE username = ?",
            (token or "", now_iso(), str(account_id)),
        )
        conn.commit()
    finally:
        conn.close()

def list_conversations(user_id: int) -> list[sqlite3.Row]:
    """会话列表，按最后一条消息时间倒序（PRD 3.1.6）。"""
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM conversations WHERE user_id = ? "
            "ORDER BY COALESCE(last_message_at, created_at) DESC, id ASC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

def create_conversation(
    *,
    user_id: int,
    conv_type: str,
    title: Optional[str],
    participants: list[int],
    peer_user_id: Optional[int] = None,
) -> int:
    """新建会话（阶段4任务4.1 群聊会话创建）。

    :param peer_user_id: 人类好友会话（type='human'）里对方的 users.id。
        单聊/群聊传 None。为什么人类对手要单独一列而不是放进 participants，
        见 SCHEMA_MIGRATIONS 里的说明（两套自增 ID 会撞号）。
    """
    ts = now_iso()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO conversations "
            "(user_id, type, title, participants, peer_user_id, unread_count, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (
                user_id,
                conv_type,
                title,
                json.dumps(participants),
                peer_user_id,
                ts,
                ts,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()

# ---------------------------------------------------------------- 人类好友（第七轮）
#

def add_friend_link(user_id: int, peer_user_id: int) -> bool:
    """建立双向好友关系（幂等）。返回是否新建。"""
    if int(user_id) == int(peer_user_id):
        return False
    ts = now_iso()
    conn = get_conn()
    try:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM friend_links "
            "WHERE user_id = ? AND peer_user_id = ?",
            (user_id, peer_user_id),
        ).fetchone()["c"]
        # 两行都插，任一方看自己的 friend_links 就能列出好友。
        for a, b in ((user_id, peer_user_id), (peer_user_id, user_id)):
            conn.execute(
                "INSERT OR IGNORE INTO friend_links "
                "(user_id, peer_user_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (a, b, ts, ts),
            )
        conn.commit()
        return int(before) == 0
    finally:
        conn.close()

def remove_friend_link(user_id: int, peer_user_id: int) -> int:
    """删除双向好友关系（用户原话：「只要有一方删除了双向都没了」）。

    返回删掉的关系行数（正常是 2）。
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM friend_links WHERE (user_id = ? AND peer_user_id = ?) "
            "OR (user_id = ? AND peer_user_id = ?)",
            (user_id, peer_user_id, peer_user_id, user_id),
        )
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()

def list_friend_links(user_id: int) -> list[sqlite3.Row]:
    """我的人类好友（users 行，含账号 ID、昵称、头像）。"""
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT u.* FROM friend_links f "
            "JOIN users u ON u.id = f.peer_user_id "
            "WHERE f.user_id = ? ORDER BY f.created_at ASC, u.id ASC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

def find_human_conversation(user_id: int, peer_user_id: int) -> Optional[int]:
    """我和某个人类好友的会话 id（没有返回 None）。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM conversations "
            "WHERE type = 'human' AND user_id = ? AND peer_user_id = ? "
            "ORDER BY id ASC LIMIT 1",
            (user_id, peer_user_id),
        ).fetchone()
        return int(row["id"]) if row else None
    finally:
        conn.close()

def delete_human_conversations(user_id: int, peer_user_id: int) -> int:
    """删掉两个人之间**两边的**人类会话及其消息（删好友时用）。

    两边都要删：好友关系没了，任何一方再看到那个会话都点不进去，
    留着只会让列表里挂一条打不开的对话。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM conversations WHERE type = 'human' AND "
            "((user_id = ? AND peer_user_id = ?) OR (user_id = ? AND peer_user_id = ?))",
            (user_id, peer_user_id, peer_user_id, user_id),
        ).fetchall()
        for row in rows:
            cid = int(row["id"])
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM delayed_tasks WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        conn.commit()
        return len(rows)
    finally:
        conn.close()

def find_single_conversation(user_id: int, ai_profile_id: int) -> Optional[int]:
    """查找已有的单聊会话（用户“加好友”时避免给同一个 AI 重复建会话）。

    2026-09-27 新增：首次启动不再自动建单聊会话，用户点“加好友”时才建，
    因此需要这个查重函数。participants 是 JSON 数组，单聊时恰好等于 [ai_profile_id]。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, participants FROM conversations "
            "WHERE user_id = ? AND type = 'single' ORDER BY id ASC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            parsed = json.loads(row["participants"] or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, list):
            continue
        ids = [int(x) for x in parsed if str(x).lstrip("-").isdigit()]
        if ids == [ai_profile_id]:
            return int(row["id"])
    return None

def update_conversation_participants(conversation_id: int, participants: list[int]) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE conversations SET participants = ?, updated_at = ? WHERE id = ?",
            (json.dumps(participants), now_iso(), conversation_id),
        )
        conn.commit()
    finally:
        conn.close()

def conversation_participants(conversation_id: int) -> list[int]:
    conv = get_conversation(conversation_id)
    if conv is None:
        return []
    try:
        parsed = json.loads(conv["participants"] or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [int(x) for x in parsed if isinstance(x, (int, float, str)) and str(x).isdigit()]

def set_conversation_background(conversation_id: int, background_text: str) -> None:
    """写入群聊上下文背景（PRD 3.4.1：可上传特定群聊的聊天记录文档作为上下文背景）。"""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE conversations SET background_text = ?, updated_at = ? WHERE id = ?",
            (background_text or "", now_iso(), conversation_id),
        )
        conn.commit()
    finally:
        conn.close()

def conversation_background(conversation_id: int) -> str:
    """读取群聊上下文背景；单聊或未上传时返回空字符串（PRD 3.4.1）。"""
    conv = get_conversation(conversation_id)
    if conv is None:
        return ""
    try:
        return str(conv["background_text"] or "")
    except (IndexError, KeyError):  # pragma: no cover - 老库未迁移时的兜底
        return ""

def clear_unread(conversation_id: int) -> None:
    """进入聊天框后未读数清零（PRD 3.1.6）。"""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE conversations SET unread_count = 0, updated_at = ? WHERE id = ?",
            (now_iso(), conversation_id),
        )
        conn.commit()
    finally:
        conn.close()

def mark_conversation_read(conversation_id: int) -> int:
    """把会话内所有消息标记已读（messages.is_read=1），返回影响条数。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE messages SET is_read = 1, updated_at = ? "
            "WHERE conversation_id = ? AND is_read = 0",
            (now_iso(), conversation_id),
        )
        conn.execute(
            "UPDATE conversations SET unread_count = 0, updated_at = ? WHERE id = ?",
            (now_iso(), conversation_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()

# ---------------------------------------------------------------- AI 档案 / 状态

def list_ai_profiles(user_id: Optional[int] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        if user_id is None:
            return conn.execute("SELECT * FROM ai_profiles ORDER BY id ASC").fetchall()
        return conn.execute(
            "SELECT * FROM ai_profiles WHERE user_id = ? ORDER BY id ASC", (user_id,)
        ).fetchall()
    finally:
        conn.close()

def create_ai_profile(
    *,
    user_id: int,
    name: str,
    avatar_url: Optional[str] = None,
    personality_data: Optional[str] = None,
    emotion: float = 0.5,
    energy: float = 0.5,
    intimacy: float = 0.0,
) -> int:
    ts = now_iso()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO ai_profiles "
            "(user_id, name, avatar_url, personality_data, state_emotion, state_energy, "
            " state_intimacy, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                user_id,
                name,
                avatar_url,
                personality_data or "{}",
                emotion,
                energy,
                intimacy,
                ts,
                ts,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()

def update_ai_state(
    ai_profile_id: int,
    *,
    emotion: float,
    energy: float,
    intimacy: float,
    last_active_at: Optional[str] = None,
) -> None:
    """写入状态机三值。"""
    conn = get_conn()
    try:
        if last_active_at is None:
            conn.execute(
                "UPDATE ai_profiles SET state_emotion = ?, state_energy = ?, "
                "state_intimacy = ?, updated_at = ? WHERE id = ?",
                (emotion, energy, intimacy, now_iso(), ai_profile_id),
            )
        else:
            conn.execute(
                "UPDATE ai_profiles SET state_emotion = ?, state_energy = ?, "
                "state_intimacy = ?, last_active_at = ?, updated_at = ? WHERE id = ?",
                (emotion, energy, intimacy, last_active_at, now_iso(), ai_profile_id),
            )
        conn.commit()
    finally:
        conn.close()

def update_personality_data(ai_profile_id: int, personality_data: str) -> None:
    """写入人格档案。"""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE ai_profiles SET personality_data = ?, updated_at = ? WHERE id = ?",
            (personality_data, now_iso(), ai_profile_id),
        )
        conn.commit()
    finally:
        conn.close()

def update_ai_profile_fields(
    ai_profile_id: int,
    *,
    name: Optional[str] = None,
    avatar_url: Optional[str] = None,
    api_provider: Optional[str] = None,
    api_base_url: Optional[str] = None,
    api_model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> None:
    """客户端可改 AI 昵称、头像、以及这个 AI 自己的 API 配置。

    昵称与头像依据 PRD 3.1.2；API 四件套依据用户 2026-09-27 的要求
    「每一个不同的聊天点进去后可以在右上角设置，这个设置里面只能设置
     对方的头像、名称、API接口、APIKEY、人格对话数据」。
    注意 api_key 传 None 表示“不改”，传空串表示“清空”。
    """
    sets, args = [], []
    if name is not None:
        sets.append("name = ?")
        args.append(name)
    if avatar_url is not None:
        sets.append("avatar_url = ?")
        args.append(avatar_url)
    if api_provider is not None:
        sets.append("api_provider = ?")
        args.append(api_provider)
    if api_base_url is not None:
        sets.append("api_base_url = ?")
        args.append(api_base_url)
    if api_model is not None:
        sets.append("api_model = ?")
        args.append(api_model)
    if api_key is not None:
        sets.append("api_key = ?")
        args.append(api_key)
    if not sets:
        return
    sets.append("updated_at = ?")
    args.append(now_iso())
    args.append(ai_profile_id)
    conn = get_conn()
    try:
        conn.execute(f"UPDATE ai_profiles SET {', '.join(sets)} WHERE id = ?", tuple(args))
        conn.commit()
    finally:
        conn.close()

def touch_ai_active(ai_profile_id: int, timestamp: Optional[str] = None) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE ai_profiles SET last_active_at = ?, updated_at = ? WHERE id = ?",
            (timestamp or now_iso(), now_iso(), ai_profile_id),
        )
        conn.commit()
    finally:
        conn.close()

def delete_ai_profile_cascade(user_id: int, ai_profile_id: int) -> Optional[dict]:
    """删除一个 AI 好友，以及它名下的一切。

    用户 2026-09-27 第六轮要求：「随时可以删除这个AI聊天好友，删除模块在每个好友
    聊天框点进去后的右上角设置」。

    删掉的范围（用户确认的口径）：
    1. AI 档案本身（`ai_profiles` 一行）；
    2. 跟它的**单聊会话**，以及该会话里的消息 / 延迟任务 / 摘要 / 记忆；
    3. 它名下的全部记忆（可能跨会话）；
    4. 它的头像文件（如果落在服务端自己的数据目录里）。

    群聊的处理（用户确认：「在群聊内则会被踢出群聊，直接从群聊中移走，瞬间消失了」）：
    - **只把它从 participants 里移出去，群和历史消息都留着**；
    - 群成员被移空时才连带删掉这个群。
    这样不会误伤群里其他人的对话上下文。

    返回 None 表示“这个 AI 不存在或不属于这个用户”，调用方按 404 处理
    （不返回 403，避免泄露“这个东西存在但不归你”）。
    返回的字典里带删除计数，供日志与测试断言。
    """
    profile = get_ai_profile(ai_profile_id)
    if profile is None or int(profile["user_id"] or 0) != int(user_id):
        return None

    removed = {
        "ai_profile_id": int(ai_profile_id),
        "name": profile["name"],
        "conversations_deleted": [],
        "groups_left": [],
        "messages": 0,
        "tasks": 0,
        "summaries": 0,
        "memories": 0,
    }
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, type, participants FROM conversations WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        single_convs: list[int] = []
        group_convs: list[tuple[int, list[int]]] = []
        for row in rows:
            try:
                parts = [int(x) for x in json.loads(row["participants"] or "[]")]
            except (ValueError, TypeError):
                continue
            if ai_profile_id not in parts:
                continue
            if len(parts) == 1:
                single_convs.append(int(row["id"]))
            else:
                group_convs.append((int(row["id"]), parts))

        # ---- 单聊：整个会话连同内容一起删 ----
        for cid in single_convs:
            # 顺序必须服从外键：delayed_tasks.message_id -> messages.id，
            # messages.conversation_id -> conversations.id。
            # 先删消息会撞 FOREIGN KEY constraint failed（实测踩过）。
            removed["tasks"] += conn.execute(
                "DELETE FROM delayed_tasks WHERE conversation_id = ?", (cid,)
            ).rowcount
            removed["messages"] += conn.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (cid,)
            ).rowcount
            removed["summaries"] += conn.execute(
                "DELETE FROM summaries WHERE conversation_id = ?", (cid,)
            ).rowcount
            removed["memories"] += conn.execute(
                "DELETE FROM memories WHERE conversation_id = ?", (cid,)
            ).rowcount
            conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
            removed["conversations_deleted"].append(cid)

        # ---- 群聊：只把人移出去，消息一条不动 ----
        for cid, parts in group_convs:
            left = [p for p in parts if p != ai_profile_id]
            if not left:
                # 群里只剩它自己了：这个群已经没有意义，整体删掉。
                removed["tasks"] += conn.execute(
                    "DELETE FROM delayed_tasks WHERE conversation_id = ?", (cid,)
                ).rowcount
                removed["messages"] += conn.execute(
                    "DELETE FROM messages WHERE conversation_id = ?", (cid,)
                ).rowcount
                removed["summaries"] += conn.execute(
                    "DELETE FROM summaries WHERE conversation_id = ?", (cid,)
                ).rowcount
                removed["memories"] += conn.execute(
                    "DELETE FROM memories WHERE conversation_id = ?", (cid,)
                ).rowcount
                conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
                removed["conversations_deleted"].append(cid)
            else:
                conn.execute(
                    "UPDATE conversations SET participants = ?, updated_at = ? "
                    "WHERE id = ?",
                    (json.dumps(left), now_iso(), cid),
                )
                removed["groups_left"].append(cid)

        # ---- 它名下剩下的记忆（可能在别的会话里） ----
        removed["memories"] += conn.execute(
            "DELETE FROM memories WHERE ai_profile_id = ?", (ai_profile_id,)
        ).rowcount
        # ---- 档案本身 ----
        conn.execute("DELETE FROM ai_profiles WHERE id = ?", (ai_profile_id,))
        conn.commit()
    finally:
        conn.close()

    # ---- 向量库：删掉那些已经被删掉的会话的向量 ----
    # memory 模块 import 了 database，这里必须延迟 import 才不会循环。
    try:
        import memory as mem

        for cid in removed["conversations_deleted"]:
            mem.vector_store().delete_by_conversation(int(cid))
    except Exception:  # noqa: BLE001 - 向量库清理失败不影响好友已删除的事实
        logging.getLogger("database").exception("清理向量库失败")

    # ---- 头像文件：只删落在服务端自己数据目录里的，绝不碰客户端路径 ----
    avatar = profile["avatar_url"]
    if avatar:
        try:
            from config import DATA_DIR, UPLOAD_DIR

            path = Path(str(avatar)).resolve()
            for base in (UPLOAD_DIR, DATA_DIR):
                base_r = Path(base).resolve()
                if path.is_relative_to(base_r) and path.is_file():
                    path.unlink()
                    break
        except Exception:  # noqa: BLE001 - 文件删不掉不影响数据已删除
            logging.getLogger("database").exception("删除好友头像文件失败")

    return removed

def record_proactive(ai_profile_id: int, date_str: str) -> None:
    """主动发起计数。

    同一天累加；跨天重置为 1。proactive_count_date 存 YYYY-MM-DD。
    """
    ts = now_iso()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT proactive_count_today, proactive_count_date FROM ai_profiles WHERE id = ?",
            (ai_profile_id,),
        ).fetchone()
        if row is None:
            return
        if row["proactive_count_date"] == date_str:
            count = int(row["proactive_count_today"]) + 1
        else:
            count = 1
        conn.execute(
            "UPDATE ai_profiles SET proactive_count_today = ?, proactive_count_date = ?, "
            "last_proactive_at = ?, updated_at = ? WHERE id = ?",
            (count, date_str, ts, ts, ai_profile_id),
        )
        conn.commit()
    finally:
        conn.close()

# ---------------------------------------------------------------- 消息查询

# 「还没发出去」的消息过滤片段（2026-09-29 第十轮修）。
#
# 背景：AI 的多段回复是**先全部落库（state='pending'、timestamp 在将来）、
# 再由延迟队列逐段推送**的。也就是说库里躺着一堆「将来才该出现」的消息。
# 而历史接口和补发接口原来把这些行一起返回了 —— 用户一进聊天页，
# 整段回复（3~7 条）瞬间全部出现，看着就是「回复很长一串」，
# 延迟也完全失效。用户实测反馈的就是这个。
#
# 判定「还没发出去」不能只看 state='pending'：消息推出去之后、客户端 ack
# 之前，state 也还是 pending（ack 才会改成 delivered）。所以必须同时满足
# 「state='pending'」**且**「还有一条活的延迟任务挂着它」——
# 任务一旦 done/failed，这条消息就算已经发过了，历史里必须能看到。
UNSENT_MESSAGE_CONDITION = (
    "state = 'pending' AND id IN ("
    "SELECT message_id FROM delayed_tasks "
    "WHERE status IN ('pending', 'processing') AND message_id IS NOT NULL)"
)

def list_messages(
    conversation_id: int,
    *,
    limit: int = 50,
    before_id: Optional[int] = None,
    exclude_unsent: bool = False,
) -> list[sqlite3.Row]:
    """按 id 升序返回消息（最多 limit 条）。before_id 用于取更早的历史。

    :param exclude_unsent: 是否排除「还没到点发出去」的 AI 消息。
        默认 **False**（返回库里的全部行）—— 这是一个底层读接口，
        内部统计、测试、镜像落盘都要看到数据库的真实内容。
        只有**给客户端看的界面**才传 True（历史接口），
        否则延迟就是摆设：AI 的整段回复会一进聊天页就全部冒出来
        （见 UNSENT_MESSAGE_CONDITION 与用户实测的「回复很长一串」）。
    """
    conn = get_conn()
    try:
        where = "conversation_id = ?"
        args: list[Any] = [conversation_id]
        if exclude_unsent:
            where += f" AND NOT ({UNSENT_MESSAGE_CONDITION})"
        if before_id is not None:
            where += " AND id < ?"
            args.append(before_id)
        args.append(limit)
        rows = conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?",
            tuple(args),
        ).fetchall()
        return list(reversed(rows))
    finally:
        conn.close()

def list_unsent_message_ids(conversation_id: int) -> set[int]:
    """这个会话里「还排着队、没发出去」的 AI 消息 id 集合。

    连发合并要用：它是唯一能准确区分「这条 AI 消息发出去了没有」的依据
    —— 消息推出去之后、客户端 ack 之前 state 仍然是 'pending'，
    所以只看 state 分不出来（见 UNSENT_MESSAGE_CONDITION 的说明）。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT message_id FROM delayed_tasks "
            "WHERE conversation_id = ? AND status IN ('pending', 'processing') "
            "AND message_id IS NOT NULL",
            (conversation_id,),
        ).fetchall()
        return {int(r["message_id"]) for r in rows}
    finally:
        conn.close()

def count_messages_in_conversation(conversation_id: int) -> int:
    conn = get_conn()
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["c"]
        )
    finally:
        conn.close()

def last_message_in_conversation(conversation_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()

def last_ai_message_at(conversation_id: int) -> Optional[str]:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT timestamp FROM messages WHERE conversation_id = ? AND sender_type = 'ai' "
            "ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row["timestamp"] if row else None
    finally:
        conn.close()

# ---------------------------------------------------------------- 记忆 / 摘要

def insert_memory(
    *,
    ai_profile_id: int,
    conversation_id: int,
    content: str,
    memory_type: str = "fact",
    vector_id: Optional[str] = None,
    importance: float = 0.5,
) -> int:
    ts = now_iso()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO memories "
            "(ai_profile_id, conversation_id, memory_type, content, vector_id, importance, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ai_profile_id,
                conversation_id,
                memory_type,
                content,
                vector_id,
                importance,
                ts,
                ts,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()

def list_memories(ai_profile_id: int, conversation_id: Optional[int] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        if conversation_id is None:
            return conn.execute(
                "SELECT * FROM memories WHERE ai_profile_id = ? ORDER BY id DESC",
                (ai_profile_id,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM memories WHERE ai_profile_id = ? AND conversation_id = ? "
            "ORDER BY id DESC",
            (ai_profile_id, conversation_id),
        ).fetchall()
    finally:
        conn.close()

def get_memories_by_ids(memory_ids: list[int]) -> list[sqlite3.Row]:
    if not memory_ids:
        return []
    marks = ",".join("?" for _ in memory_ids)
    conn = get_conn()
    try:
        return conn.execute(
            f"SELECT * FROM memories WHERE id IN ({marks})", tuple(memory_ids)
        ).fetchall()
    finally:
        conn.close()

def touch_memories_accessed(memory_ids: list[int]) -> None:
    if not memory_ids:
        return
    marks = ",".join("?" for _ in memory_ids)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE memories SET last_accessed_at = ?, updated_at = ? WHERE id IN ({marks})",
            (now_iso(), now_iso(), *memory_ids),
        )
        conn.commit()
    finally:
        conn.close()

def delete_memories(ai_profile_id: int, conversation_id: int) -> None:
    """测试与重建向量库时使用。"""
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM memories WHERE ai_profile_id = ? AND conversation_id = ?",
            (ai_profile_id, conversation_id),
        )
        conn.commit()
    finally:
        conn.close()

def clear_conversation_history(conversation_id: int) -> dict:
    """清空一个会话的**聊天记录**（用户 2026-10-04 要求）。

    对真人好友和 AI 好友都有效；对 AI 好友而言，清掉这些之后它**不再记得**
    之前聊过什么 —— 因为它的"记忆"来源就是下面这四张表：

    - `messages`    最近聊过的内容（记忆层的短期窗口直接读它）
    - `summaries`   中期摘要
    - `memories`    长期事实（记忆层的长期记忆从这里检索）
    - `delayed_tasks`  还没发出去的延迟消息 / 延迟任务（不清的话，清空之后
      它还会"补发"几条几秒前排好的消息，看起来像没清干净）

    删除顺序服从外键：delayed_tasks.message_id -> messages.id。

    会话本身**保留**（标题、亲密度、AI 档案都不动）—— 用户要的是"清空聊天记录"，
    不是"删好友"。原来的删好友那条路走 `delete_ai_profile_cascade`。
    """
    conn = get_conn()
    try:
        tasks = conn.execute(
            "DELETE FROM delayed_tasks WHERE conversation_id = ?", (conversation_id,)
        ).rowcount
        messages = conn.execute(
            "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
        ).rowcount
        summaries = conn.execute(
            "DELETE FROM summaries WHERE conversation_id = ?", (conversation_id,)
        ).rowcount
        memories = conn.execute(
            "DELETE FROM memories WHERE conversation_id = ?", (conversation_id,)
        ).rowcount
        conn.execute(
            "UPDATE conversations SET unread_count = 0, last_message_at = NULL, "
            "updated_at = ? WHERE id = ?",
            (now_iso(), conversation_id),
        )
        conn.commit()
        return {
            "messages": messages,
            "summaries": summaries,
            "memories": memories,
            "tasks": tasks,
        }
    finally:
        conn.close()

def insert_summary(
    *,
    conversation_id: int,
    summary_text: str,
    start_message_id: int,
    end_message_id: int,
    token_count: int = 0,
) -> int:
    ts = now_iso()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO summaries "
            "(conversation_id, summary_text, start_message_id, end_message_id, token_count, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                summary_text,
                start_message_id,
                end_message_id,
                token_count,
                ts,
                ts,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()

def latest_summary(conversation_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM summaries WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()

def list_summaries(conversation_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM summaries WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
    finally:
        conn.close()

# ---------------------------------------------------------------- API 池

def add_api_config(
    *,
    user_id: int,
    provider: str,
    model_name: str,
    api_key: str,
    base_url: Optional[str] = None,
    priority: int = 0,
    is_primary: int = 0,
    role: str = "chat",
) -> int:
    """写入一条 API 配置。api_key 至少 Base64 编码后存储。"""
    ts = now_iso()
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO api_pool "
            "(user_id, provider, model_name, api_key, base_url, priority, is_active, "
            " is_primary, role, failure_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?)",
            (
                user_id,
                provider,
                model_name,
                api_key,
                base_url,
                priority,
                is_primary,
                role,
                ts,
                ts,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()

def list_api_configs(user_id: Optional[int] = None, role: Optional[str] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        sql = "SELECT * FROM api_pool WHERE 1 = 1"
        args: list[Any] = []
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        if role is not None:
            sql += " AND role = ?"
            args.append(role)
        sql += " ORDER BY priority ASC, id ASC"
        return conn.execute(sql, tuple(args)).fetchall()
    finally:
        conn.close()

def get_api_config(api_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM api_pool WHERE id = ?", (api_id,)).fetchone()
    finally:
        conn.close()

def update_api_config(
    api_id: int,
    *,
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    priority: Optional[int] = None,
    is_active: Optional[int] = None,
    is_primary: Optional[int] = None,
    role: Optional[str] = None,
) -> None:
    fields = {
        "provider": provider,
        "model_name": model_name,
        "api_key": api_key,
        "base_url": base_url,
        "priority": priority,
        "is_active": is_active,
        "is_primary": is_primary,
        "role": role,
    }
    sets, args = [], []
    for col, val in fields.items():
        if val is not None:
            sets.append(f"{col} = ?")
            args.append(val)
    if not sets:
        return
    sets.append("updated_at = ?")
    args.append(now_iso())
    args.append(api_id)
    conn = get_conn()
    try:
        conn.execute(f"UPDATE api_pool SET {', '.join(sets)} WHERE id = ?", tuple(args))
        conn.commit()
    finally:
        conn.close()

def delete_api_config(api_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM api_pool WHERE id = ?", (api_id,))
        conn.commit()
    finally:
        conn.close()

def mark_api_used(api_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE api_pool SET last_used_at = ?, failure_count = 0, updated_at = ? "
            "WHERE id = ?",
            (now_iso(), now_iso(), api_id),
        )
        conn.commit()
    finally:
        conn.close()

def mark_api_failure(api_id: int, error: str = "") -> int:
    """连续失败次数 +1，返回新的失败次数（阶段4任务4.7 故障切换依据）。"""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE api_pool SET failure_count = failure_count + 1, updated_at = ? WHERE id = ?",
            (now_iso(), api_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT failure_count FROM api_pool WHERE id = ?", (api_id,)
        ).fetchone()
        return int(row["failure_count"]) if row else 0
    finally:
        conn.close()
