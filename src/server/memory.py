"""记忆管理器（阶段3任务3.1 ~ 3.6）。

职责：管理三层记忆架构。

层级           内容                      触发条件            存储位置
短期滑动窗口   最近8-10轮原始对话        始终保留            内存 + 数据库
中期滚动摘要   窗口外历史≥20条时压缩     窗口外历史≥20条     数据库 summaries
长期向量记忆   从对话提取的事实性信息    对话中产生事实时    LanceDB/Chroma + memories

- 上下文管理（三层记忆架构）
- 记忆管理器
- memories / 3.7 summaries
- 记忆层模板

文档未写死的细节，自行决定并记录：
1. 短期窗口轮数 SHORT_TERM_ROUNDS = 9（文档写“8-10轮”，取中间值）。
   “一轮”= 一条用户消息及其之后到下一个用户消息之前的 AI 消息，成对取。
2. Token 上限 TOKEN_LIMIT = 3000。
   Token 计算优先用 tiktoken；不可用时退化为“字符数 / 2”估算，保证功能可用。
3. 向量库使用 LanceDB。
   向量由“字符 n-gram 哈希”方式生成（见 _embed），不引入额外 embedding API：
   目的是让长期记忆检索在本地、零依赖外部服务的前提下可用。检索用余弦相似度。
   向量库不可用时（导入失败 / 磁盘异常）降级为不使用长期记忆。
4. 摘要生成使用 role="memory" 的轻量模型；失败时退化为“拼接式摘要”，不阻断对话。
5. 事实提取同样用轻量模型，返回 JSON 数组；失败则不写入长期记忆。
6. 事实提取只在用户消息里出现“值得记的事实”时进行，由模型判断，服务端不额外写规则。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import api_pool
import database as db
import vision
from config import VECTOR_DIR

logger = logging.getLogger("memory")

SHORT_TERM_ROUNDS = 9
SUMMARY_TRIGGER_MESSAGES = 20
TOKEN_LIMIT = 3000
VECTOR_DIM = 256
VECTOR_TABLE = "memories"
# VECTOR_DIR 来自 config，支持 DSH_VECTOR_DIR 覆盖（测试隔离）

# ---------------------------------------------------------------- Token 计算

def count_tokens(text: str) -> int:
    """计算文本 Token 数。tiktoken 不可用时用“字符数 / 2”估算。"""
    if not text:
        return 0
    try:
        import tiktoken  # 局部导入，缺失时走估算

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:  # noqa: BLE001 - 任何异常都退化为估算
        return max(1, len(text) // 2)

def _row_field(row, name: str, default: str = "") -> str:
    """安全读一行里的某个字段（拿不到就给默认值）。"""
    try:
        value = row[name]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else str(value)

def display_parts(row) -> list[str]:
    """把一行消息拆成「可以写进 Prompt」的文本段。

    图片消息在库里存的是 `data:image/jpeg;base64,...` 一整串，而这里以前是
    直接拼 `content` 的 —— 几万个 base64 字符会原样进 Prompt：模型既看不懂，
    又白烧 token，还会**假装自己看懂了**（那天它把汉堡点评成了 KDE 桌面截图）。

    所以所有进 Prompt 的路径都必须经过这里，由 vision 统一换成 `[图片]` 占位。
    """
    ctype = _row_field(row, "content_type")
    return [
        vision.display_text(ctype, part)
        for part in db.content_to_array(row["content"])
    ]

def content_text(row) -> str:
    """把 messages 行还原成纯文本（多段消息拼成一段，便于计数与注入）。"""
    return " ".join(display_parts(row))

# ---------------------------------------------------------------- 向量（本地、无外部依赖）

def _embed(text: str) -> list[float]:
    """把文本映射成 VECTOR_DIM 维向量。

    实现：对文本的字符 1-gram 与 2-gram 做哈希投影，再 L2 归一化。
    这是本地确定性哈希嵌入，不含语义理解，但足以支撑“同一话题能召回相关记忆”
    的阶段性验收；后续接入真实 embedding 只需替换本函数。
    """
    vec = [0.0] * VECTOR_DIM
    text = (text or "").strip()
    if not text:
        return vec
    grams: list[str] = []
    grams.extend(text)
    grams.extend(text[i : i + 2] for i in range(len(text) - 1))
    for gram in grams:
        if not gram:
            continue
        digest = hashlib.md5(gram.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % VECTOR_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-9:
        return vec
    return [v / norm for v in vec]

def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

class VectorStore:
    """LanceDB 向量库封装。不可用时 available=False，调用方降级。"""

    def __init__(self, path: Path = VECTOR_DIR) -> None:
        self.path = path
        self.available = False
        self._db = None
        self._table = None
        try:
            import lancedb

            self.path.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.path))
            self.available = True
        except Exception as exc:  # noqa: BLE001 - 向量库不可用必须降级而不是崩溃
            logger.warning("向量库不可用，长期记忆降级：%s", exc)
            self._db = None

    def _open_table(self):
        if not self.available or self._db is None:
            return None
        if self._table is not None:
            return self._table
        try:
            names = list(self._db.table_names())
            if VECTOR_TABLE in names:
                self._table = self._db.open_table(VECTOR_TABLE)
            return self._table
        except Exception as exc:  # noqa: BLE001
            logger.warning("打开向量表失败：%s", exc)
            self.available = False
            return None

    def add(self, vector_id: str, text: str, vector: list[float]) -> bool:
        table = self._open_table()
        try:
            row = {"vector_id": vector_id, "text": text, "vector": vector}
            if table is None:
                self._table = self._db.create_table(VECTOR_TABLE, data=[row])
            else:
                table.add([row])
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入向量失败：%s", exc)
            return False

    def search(self, vector: list[float], limit: int = 5) -> list[tuple[str, float]]:
        """返回 [(vector_id, score)]，score 为余弦相似度。"""
        table = self._open_table()
        if table is None:
            return []
        try:
            rows = table.to_arrow().to_pylist()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取向量表失败：%s", exc)
            return []
        scored: list[tuple[str, float]] = []
        for row in rows:
            stored = row.get("vector")
            if not stored:
                continue
            try:
                score = _cosine(vector, [float(x) for x in stored])
            except (TypeError, ValueError):
                continue
            scored.append((str(row.get("vector_id")), score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    def delete_by_conversation(self, conversation_id: int) -> None:
        table = self._open_table()
        if table is None:
            return
        try:
            table.delete(f"vector_id LIKE 'conv{conversation_id}_%'")
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除向量失败：%s", exc)

_store: Optional[VectorStore] = None

def vector_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
    return _store

def forget_conversation(conversation_id: int) -> None:
    """清空会话时，把长期记忆在**向量库**里的那一份也删掉（用户 2026-10-04 要求）。

    `memories` 行由 `db.clear_conversation_history` 在同一个事务里删；
    这里删向量库里对应的那批（vector_id 形如 `conv<会话id>_*`）。

    不删会怎样：检索时是先查向量、再按 vector_id 回 `memories` 找内容
    （见 `search_memories`），行没了就匹配不上 —— 它**不会**记起来，
    但孤儿向量会一直占着库，以后 id 复用还会串味。所以顺手清掉。
    """
    vector_store().delete_by_conversation(conversation_id)

# ---------------------------------------------------------------- 短期滑动窗口（3.1）

def short_term_window(conversation_id: int, rounds: int = SHORT_TERM_ROUNDS) -> list:
    """返回最近 rounds 轮的原始消息行（按 id 升序），逐字保留。"""
    if rounds <= 0:
        return []
    rows = db.list_messages(conversation_id, limit=rounds * 4)
    if not rows:
        return []
    # 从后往前找出 rounds 个“用户消息”作为轮的分界
    user_indexes = [i for i, r in enumerate(rows) if r["sender_type"] == "user"]
    if not user_indexes:
        return rows
    keep_from = user_indexes[-rounds] if len(user_indexes) >= rounds else 0
    return rows[keep_from:]

def format_window(rows: list, user_display: str = "用户") -> str:
    """把窗口消息拼成 的“最近聊过的内容”。"""
    lines: list[str] = []
    for row in rows:
        who = user_display if row["sender_type"] == "user" else "你"
        # display_parts：图片换成 [图片] 占位（第十三轮，见它的说明）
        for part in display_parts(row):
            if part.strip():
                lines.append(f"{who}：{part}")
    return "\n".join(lines)

# ---------------------------------------------------------------- 中期摘要（3.2 / 3.5）

SUMMARY_PROMPT = """你在压缩一段聊天记录，用于长期记忆。

【已有摘要】
{previous}

【需要压缩的新对话】
{transcript}

【任务】
把上面的新对话压缩成一段简短的中文摘要，保留：聊了什么话题、用户提到的事实（喜好、习惯、近况、计划）、双方的情绪变化。
要求：
- 不超过 200 字。
- 只输出摘要正文，不要标题，不要解释，不要 Markdown。
"""

def _rows_to_transcript(rows: list, user_display: str = "用户") -> str:
    lines = []
    for row in rows:
        who = user_display if row["sender_type"] == "user" else "AI"
        # display_parts：图片换成 [图片] 占位。
        # 摘要 / 事实提取也走这条路，否则 base64 会被写成「用户发了
        # data:image/jpeg;base64,...」存进长期记忆，以后每次对话都带着它。
        for part in display_parts(row):
            if part.strip():
                lines.append(f"{who}：{part}")
    return "\n".join(lines)

async def maybe_generate_summary(
    conversation_id: int, *, user_id: int = 1, user_display: str = "用户"
) -> Optional[str]:
    """窗口外历史积累到 SUMMARY_TRIGGER_MESSAGES 条时压缩为摘要。"""
    window = short_term_window(conversation_id)
    oldest_in_window = window[0]["id"] if window else None
    if oldest_in_window is None:
        return None

    latest = db.latest_summary(conversation_id)
    covered_until = int(latest["end_message_id"]) if latest else 0
    if covered_until >= oldest_in_window - 1:
        return None

    rows = [
        r
        for r in db.list_messages(conversation_id, limit=200)
        if covered_until < r["id"] < oldest_in_window
    ]
    if len(rows) < SUMMARY_TRIGGER_MESSAGES:
        return None

    transcript = _rows_to_transcript(rows, user_display)
    previous = latest["summary_text"] if latest else "（无）"
    summary_text = ""
    try:
        raw = await api_pool.chat(
            [
                {
                    "role": "user",
                    "content": SUMMARY_PROMPT.format(previous=previous, transcript=transcript),
                }
            ],
            role="memory",
            user_id=user_id,
            temperature=0.3,
        )
        summary_text = (raw or "").strip()
    except Exception as exc:  # noqa: BLE001 - 摘要失败不能阻断对话
        logger.warning("摘要生成失败，改用拼接摘要：%s", exc)

    if not summary_text:
        joined = transcript.replace("\n", "；")
        summary_text = (previous + " " if latest else "") + joined[:200]

    db.insert_summary(
        conversation_id=conversation_id,
        summary_text=summary_text,
        start_message_id=rows[0]["id"],
        end_message_id=rows[-1]["id"],
        token_count=count_tokens(summary_text),
    )
    logger.info(
        "生成中期摘要 conversation=%s 覆盖消息 %s~%s",
        conversation_id,
        rows[0]["id"],
        rows[-1]["id"],
    )
    return summary_text

def latest_summary_text(conversation_id: int) -> str:
    row = db.latest_summary(conversation_id)
    return row["summary_text"] if row else ""

def total_context_tokens(conversation_id: int) -> int:
    """当前上下文的 Token 总量：短期窗口 + 最新摘要（任务3.5 的触发依据）。"""
    window = short_term_window(conversation_id)
    tokens = count_tokens(_rows_to_transcript(window))
    tokens += count_tokens(latest_summary_text(conversation_id))
    return tokens

async def maybe_compress_by_tokens(
    conversation_id: int, *, user_id: int = 1, user_display: str = "用户"
) -> bool:
    """历史 Token 达到 TOKEN_LIMIT 时触发压缩。

    压缩 = 生成摘要（把窗口外的旧对话固化到 summaries）+ 短期窗口自然只保留最近若干轮。
    返回是否执行了压缩。
    """
    if total_context_tokens(conversation_id) < TOKEN_LIMIT:
        return False
    done = await maybe_generate_summary(
        conversation_id, user_id=user_id, user_display=user_display
    )
    if done is None:
        # Token 超限但窗口外没有足够的新消息：直接压缩窗口外的全部历史
        window = short_term_window(conversation_id)
        if not window:
            return False
        oldest = window[0]["id"]
        rows = [r for r in db.list_messages(conversation_id, limit=200) if r["id"] < oldest]
        if not rows:
            return False
        summary_text = _rows_to_transcript(rows, user_display)[:200]
        db.insert_summary(
            conversation_id=conversation_id,
            summary_text=summary_text,
            start_message_id=rows[0]["id"],
            end_message_id=rows[-1]["id"],
            token_count=count_tokens(summary_text),
        )
        logger.info("Token 超限，强制压缩 %s 条历史消息", len(rows))
    return True

# ---------------------------------------------------------------- 长期向量记忆（3.3 / 3.4）

FACT_PROMPT = """从下面这段对话里提取关于【用户】的、值得长期记住的事实。

【对话】
{transcript}

【任务】
只输出一个 JSON 数组，每个元素是一个事实，最多 3 条。例如：
["用户不喜欢吃香菜", "用户养了一只猫叫年糕"]

【规则】
- 只提取用户明确说过的、稳定的信息：喜好、厌恶、习惯、家人宠物、工作学习、长期计划、重要事件。
- 不要提取一次性的寒暄、情绪、天气、时间。
- 如果没有值得记住的事实，输出空数组 []。
- 只输出 JSON 数组，不要解释，不要 Markdown 代码块。
"""

async def extract_and_store_facts(
    *,
    ai_profile_id: int,
    conversation_id: int,
    rows: list,
    user_id: int = 1,
    user_display: str = "用户",
) -> list[str]:
    """从对话提取事实并写入向量库 + memories 表。"""
    transcript = _rows_to_transcript(rows, user_display)
    if not transcript.strip():
        return []
    try:
        raw = await api_pool.chat(
            [{"role": "user", "content": FACT_PROMPT.format(transcript=transcript)}],
            role="memory",
            user_id=user_id,
            temperature=0.2,
            # 2026-09-28 第九轮：把会话级 Key 带进来。不传的话 role="memory"
            # 会去查全局 api_pool 表（客户端没有入口，永远是空的）→ 必然报
            # 「长期记忆提取失败」。见 api_pool.chat 的说明。
            ai_profile_id=ai_profile_id,
        )
        facts = _parse_fact_list(raw)
    except Exception as exc:  # noqa: BLE001 - 提取失败不影响对话
        logger.warning("长期记忆提取失败：%s", exc)
        return []

    stored: list[str] = []
    for fact in facts:
        fact = fact.strip()
        if not fact:
            continue
        if _fact_exists(ai_profile_id, conversation_id, fact):
            continue
        vector_id = f"conv{conversation_id}_{hashlib.md5(fact.encode('utf-8')).hexdigest()[:12]}"
        vector_store().add(vector_id, fact, _embed(fact))
        db.insert_memory(
            ai_profile_id=ai_profile_id,
            conversation_id=conversation_id,
            content=fact,
            memory_type="fact",
            vector_id=vector_id,
            importance=0.6,
        )
        stored.append(fact)
    if stored:
        logger.info("写入长期记忆 %s 条：%s", len(stored), stored)
    return stored

def _parse_fact_list(raw: str) -> list[str]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if isinstance(x, (str, int, float))]

def _fact_exists(ai_profile_id: int, conversation_id: int, fact: str) -> bool:
    for row in db.list_memories(ai_profile_id, conversation_id):
        if row["content"].strip() == fact:
            return True
    return False

def search_memories(
    *, ai_profile_id: int, conversation_id: int, query: str, limit: int = 5
) -> list[str]:
    """检索相关长期记忆。

    优先走向量库；向量库不可用时退化为 memories 表的关键字匹配。
    """
    if not query.strip():
        return []
    store = vector_store()
    hits: list[str] = []
    if store.available:
        for vector_id, score in store.search(_embed(query), limit=limit):
            if score <= 0.05:
                continue
            for row in db.list_memories(ai_profile_id, conversation_id):
                if row["vector_id"] == vector_id:
                    hits.append(row["content"])
                    db.touch_memories_accessed([int(row["id"])])
                    break
    if hits:
        return hits

    # 降级：关键字包含匹配
    keywords = [w for w in re.split(r"[\s，。！？、,.!?]+", query) if len(w) >= 2]
    fallback: list[str] = []
    for row in db.list_memories(ai_profile_id, conversation_id):
        content = row["content"]
        if any(k in content for k in keywords):
            fallback.append(content)
        if len(fallback) >= limit:
            break
    return fallback

# ---------------------------------------------------------------- 记忆层装配（3.6）

@dataclass
class MemoryBundle:
    short_term_window: str
    mid_term_summary: str
    long_term_memories: str

def build_memory_bundle(
    *,
    ai_profile_id: int,
    conversation_id: int,
    query: str,
    user_display: str = "用户",
) -> MemoryBundle:
    """装配记忆层三部分。"""
    window_rows = short_term_window(conversation_id)
    memories = search_memories(
        ai_profile_id=ai_profile_id,
        conversation_id=conversation_id,
        query=query,
    )
    return MemoryBundle(
        short_term_window=format_window(window_rows, user_display),
        mid_term_summary=latest_summary_text(conversation_id),
        long_term_memories="\n".join(f"- {m}" for m in memories),
    )

def conversation_history_text(conversation_id: int, user_display: str = "用户") -> str:
    """上下文层的最近几轮对话（与短期窗口一致）。"""
    return format_window(short_term_window(conversation_id), user_display)