"""人格管理器。

职责：
- 定义人格档案数据结构（对应 ai_profiles.personality_data）。
- 从上传的聊天记录 JSON 蒸馏出结构化人格档案。
- 写入 / 读取人格档案，后续对话中持续微调。

- personality_data 结构示例（tone / address / reply_rhythm / schedule / attitude）
- 人格层模板
- / 2.2 / 2.3 / 2.4

文档未写死的细节，自行决定并记录：
1. 聊天记录上传格式（docs 未定义）：JSON 数组，元素为
   {"sender": "user"|"friend"|"me"|"ta", "content": "...", "timestamp": "ISO8601"}。
   sender 取值宽松识别：me/user/我/自己 → 用户；其余 → 目标朋友。
   也接受 {"role": "user"|"assistant", ...} 形式。
2. 人格蒸馏使用 role="memory" 的轻量模型，
   返回 JSON；解析失败重试一次，仍失败则回退到“统计式蒸馏”（不调用模型，
   从记录里直接统计回复率、平均延迟、平均条数、活跃时段），保证功能可用。
3. 蒸馏结果字段缺失时用默认值补齐，禁止出现文档未定义的字段。
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime
from typing import Any, Optional

import api_pool
import database as db
import persona_text

logger = logging.getLogger("personality")

# 默认人格档案：完全对齐 的结构
#
DEFAULT_PERSONALITY: dict[str, Any] = {
    "tone": {
        "frequent_words": [],
        "punctuation": "",
        "sentence_length": "",
        "emoji_frequency": "",
    },
    "address": {"default": "", "variations": []},
    "reply_rhythm": {
        "reply_rate": 1.0,
        "avg_delay_seconds": 0,
        "avg_message_count": 1,
    },
    "schedule": {"active_hours": [], "sleep_hours": []},
    "attitude": "",
    "prompt_script": "",
    "delay_style": {
        # 首响延迟样本（秒），保留**离散分布**而不是只留中位数：
        # 用户要的就是「有时 2 秒、有时 3 分钟」这种真实感。
        "first_reply_samples": [],
        # 同一次回复里，段与段之间的间隔样本（秒）。
        "segment_samples": [],
        # 模型写的延迟决策规则（自然语言），例如：
        # 「聊闲事时他隔两三分钟才回，因为常在外面骑车；一旦约见面或有急事，
        #   他两三秒就回，还会连发两三条。」
        "rule": "",
    },
    # 主动发起节奏（用户 2026-09-28 要求）：
    #   「主动发消息的时间参照人格聊天记录中的朋友是什么时候主动发消息的，
    #     AI 学习到之后就按照这个来主动发消息，每天最多发主动 5 次」
    #
    # 这里存的是**朋友自己先开口**的那些时刻与间隔，来自聊天记录：
    #   - proactive_hours：他主动开口时的钟点（0-23），用来决定「几点找他」；
    #   - proactive_gaps：两次主动开口之间隔了多久（秒），用来决定「多久没说话才该开口」。
    # 两个都是离散样本 —— 只留平均值会把「他多半在深夜、偶尔早上」这种特征抹平。
    "proactive_style": {
        "proactive_hours": [],
        "proactive_gaps": [],
        # 模型写的主动开口规则（自然语言），例如：
        # 「他一般在晚上十一点多自己发消息过来，多半是无聊了或者刚下班。」
        "rule": "",
    },
}

#：reply_rate 阈值的两句话
LOW_REPLY_RATE_HINT = "你平时回复率不高，经常已读不回。"
HIGH_REPLY_RATE_HINT = "你基本句句有回应，很少已读不回。"

USER_SENDER_ALIASES = {"user", "me", "我", "自己", "用户"}

def default_personality() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_PERSONALITY, ensure_ascii=False))

# ---------------------------------------------------------------- 校验与读取

def normalize_personality(raw: Any) -> dict[str, Any]:
    """把任意输入补齐成完整人格档案；只保留文档定义的字段 + EXTRA_KEYS。"""
    base = default_personality()
    if not isinstance(raw, dict):
        return base

    tone = raw.get("tone")
    if isinstance(tone, dict):
        for key in base["tone"]:
            if key in tone and tone[key] is not None:
                base["tone"][key] = tone[key]
    base["tone"]["frequent_words"] = _as_str_list(base["tone"]["frequent_words"])

    address = raw.get("address")
    if isinstance(address, dict):
        if address.get("default"):
            base["address"]["default"] = str(address["default"])
        base["address"]["variations"] = _as_str_list(address.get("variations"))

    rhythm = raw.get("reply_rhythm")
    if isinstance(rhythm, dict):
        base["reply_rhythm"]["reply_rate"] = _clamp01(
            _as_float(rhythm.get("reply_rate"), base["reply_rhythm"]["reply_rate"])
        )
        base["reply_rhythm"]["avg_delay_seconds"] = max(
            0, int(_as_float(rhythm.get("avg_delay_seconds"), 0))
        )
        base["reply_rhythm"]["avg_message_count"] = max(
            1, int(_as_float(rhythm.get("avg_message_count"), 1))
        )

    schedule = raw.get("schedule")
    if isinstance(schedule, dict):
        base["schedule"]["active_hours"] = _as_hour_list(schedule.get("active_hours"))
        base["schedule"]["sleep_hours"] = _as_hour_list(schedule.get("sleep_hours"))

    if raw.get("attitude"):
        base["attitude"] = str(raw["attitude"])

    delay_style = raw.get("delay_style")
    if isinstance(delay_style, dict):
        base["delay_style"]["first_reply_samples"] = _as_int_list(
            delay_style.get("first_reply_samples"), limit=60, cap=86400
        )
        base["delay_style"]["segment_samples"] = _as_int_list(
            delay_style.get("segment_samples"), limit=60, cap=86400
        )
        if delay_style.get("rule"):
            base["delay_style"]["rule"] = str(delay_style["rule"])

    proactive_style = raw.get("proactive_style")
    if isinstance(proactive_style, dict):
        base["proactive_style"]["proactive_hours"] = _as_hour_list(
            proactive_style.get("proactive_hours")
        )
        base["proactive_style"]["proactive_gaps"] = _as_int_list(
            proactive_style.get("proactive_gaps"), limit=60, cap=86400 * 7
        )
        if proactive_style.get("rule"):
            base["proactive_style"]["rule"] = str(proactive_style["rule"])

    for key in EXTRA_KEYS:
        value = raw.get(key)
        if value:
            base[key] = str(value).strip()
    return base

EXTRA_KEYS: tuple[str, ...] = ("prompt_script",)

def _as_int_list(value: Any, *, limit: int = 60, cap: int = 86400) -> list[int]:
    """把任意输入规整成非负整数列表（延迟样本用）。

    截断到 limit 个：样本再多也没意义，只会把 Prompt 撑爆。
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[int] = []
    for item in value:
        try:
            number = int(float(item))
        except (TypeError, ValueError):
            continue
        out.append(max(0, min(cap, number)))
        if len(out) >= limit:
            break
    return out

def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v) != ""]
    return []

def _as_hour_list(value: Any) -> list[int]:
    out: list[int] = []
    for v in _as_str_list(value):
        try:
            hour = int(float(v))
        except ValueError:
            continue
        if 0 <= hour <= 23:
            out.append(hour)
    for v in value if isinstance(value, (list, tuple)) else []:
        if isinstance(v, (int, float)) and 0 <= int(v) <= 23:
            out.append(int(v))
    return sorted(set(out))

def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback

def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))

def load_personality(ai_profile_id: int) -> dict[str, Any]:
    """读取人格档案（任务2.4：重启服务端后仍能正确读出）。"""
    row = db.get_ai_profile(ai_profile_id)
    if row is None:
        raise ValueError(f"AI 档案不存在：{ai_profile_id}")
    try:
        raw = json.loads(row["personality_data"] or "{}")
    except (ValueError, TypeError):
        raw = {}
    return normalize_personality(raw)

def save_personality(ai_profile_id: int, personality: dict[str, Any]) -> dict[str, Any]:
    """写入人格档案。只写文档定义的字段。"""
    normalized = normalize_personality(personality)
    db.update_personality_data(
        ai_profile_id, json.dumps(normalized, ensure_ascii=False)
    )
    logger.info("人格档案已保存：ai_profile_id=%s", ai_profile_id)
    return normalized

# ---------------------------------------------------------------- 聊天记录解析

def parse_chat_log(raw: Any) -> list[dict[str, str]]:
    """把上传的人格对话数据规整为 [{"sender": "user"|"friend", "content": str, ...}]。

    **客户端只走文本格式**，旧的 `{"messages": [...]}` JSON 上传入口已删除。
    文本格式见 persona_text.py 的模块说明。

    服务端内部仍保留对结构化列表的兼容：`upload_and_distill` 被内部调用、
    以及群聊背景那边会直接给列表。这条兼容只影响服务端内部调用，
    客户端已经拿不到也不需要它。
    """
    if isinstance(raw, str):
        return persona_text.parse_persona_text(raw).records
    return normalize_records(raw)

def normalize_records(raw: Any) -> list[dict[str, str]]:
    """把结构化记录列表规整成 user/friend 两方（宽松识别 sender 别名）。"""
    if isinstance(raw, dict):
        for key in ("messages", "records", "chat_log", "data"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if content is None:
            content = item.get("text") or item.get("message") or ""
        content = str(content).strip()
        if not content:
            continue
        who = str(item.get("sender") or item.get("role") or item.get("from") or "").strip()
        lowered = who.lower()
        if lowered in ("assistant", "friend", "ta", "他", "她") or lowered in persona_text.FRIEND_ALIASES:
            sender = "friend"
        elif lowered in USER_SENDER_ALIASES or lowered in persona_text.USER_ALIASES:
            sender = "user"
        else:
            # 认不出的（多半是具体昵称）：没有称呼时按用户算，有称呼时按朋友算。
            sender = "friend" if who else "user"
        # hour / order：persona_text 从行首时间戳里抠出的钟点与序号。
        #
        # 2026-09-29 第十三轮补：以前这里只搬 timestamp，把文本格式导入时
        # persona_text 辛苦解析出来的 `hour` 丢了 —— 于是蒸馏 prompt 里那几列
        # 时间全是空的，模型没法推断作息，只能编一个 active_hours 写进人格。
        # 用户要的「让他真的学到人格」就卡在这里。
        out.append(
            {
                "sender": sender,
                "content": content,
                "timestamp": str(item.get("timestamp") or item.get("time") or ""),
                "hour": str(item.get("hour") or ""),
                "order": str(item.get("order") or ""),
            }
        )
    return out

def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None

def _statistical_distill(
    records: list[dict[str, str]],
    friend_name: str,
    parsed: Optional[persona_text.ParsedPersona] = None,
) -> dict[str, Any]:
    """不调用模型的兜底蒸馏：从记录里统计回复节奏与作息。

    2026-09-27 第七轮：延迟不再只算一个平均数，而是把**首响**与**段间隔**
    两组样本分别存进 delay_style。用户要的是「有时 2 秒、有时 3 分钟」，
    只留一个均值会把这个特征抹平。
    """
    persona = default_personality()
    if parsed is not None:
        persona["delay_style"]["first_reply_samples"] = parsed.first_reply_samples
        persona["delay_style"]["segment_samples"] = parsed.segment_samples
        # 主动开口节奏（用户 2026-09-28 要求）：朋友什么时候自己找人说话。
        persona["proactive_style"]["proactive_hours"] = parsed.proactive_hour_set
        persona["proactive_style"]["proactive_gaps"] = parsed.proactive_gaps
        # 没写时间戳时，用「朋友主动开口的钟点」当活跃时段 —— 这比拿朋友的
        # 全部发言时刻更贴近「他什么时候会想起你」。
        if parsed.proactive_hour_set and not persona["schedule"]["active_hours"]:
            persona["schedule"]["active_hours"] = parsed.proactive_hour_set[:6]
    friend_msgs = [r for r in records if r["sender"] == "friend"]
    user_msgs = [r for r in records if r["sender"] == "user"]
    if not friend_msgs:
        persona["attitude"] = ""
        return persona

    reply_rate = 1.0
    if user_msgs:
        reply_rate = _clamp01(len(friend_msgs) / max(1, len(user_msgs)))
    persona["reply_rhythm"]["reply_rate"] = round(reply_rate, 2)
    persona["reply_rhythm"]["avg_message_count"] = max(
        1, round(len(friend_msgs) / max(1, len(user_msgs) or 1))
    )

    lengths = [len(r["content"]) for r in friend_msgs]
    avg_len = sum(lengths) / len(lengths)
    if avg_len <= 6:
        persona["tone"]["sentence_length"] = "极短句，常常一两个字"
    elif avg_len <= 15:
        persona["tone"]["sentence_length"] = "短句为主"
    else:
        persona["tone"]["sentence_length"] = "偶尔长句"

    # 作息：先看 timestamp，再看 persona_text 抠出来的 hour 列。
    # 第十三轮补 hour 这一路 —— 文本格式导入的记录里 timestamp 是空的，
    # 只认 timestamp 会让统计兜底也拿不到作息（和蒸馏模型那边同一个 bug）。
    hours: list[int] = []
    for r in friend_msgs:
        ts = _parse_ts(r.get("timestamp") or "")
        if ts is not None:
            hours.append(ts.hour)
            continue
        raw_hour = str(r.get("hour") or "").strip()
        if raw_hour:
            try:
                hours.append(int(float(raw_hour)))
            except ValueError:
                continue
    if hours:
        counter = Counter(hours)
        persona["schedule"]["active_hours"] = sorted(
            [h for h, _ in counter.most_common(4)]
        )
        persona["schedule"]["sleep_hours"] = sorted(
            [h for h in range(24) if h not in set(hours)]
        )[:5]

    delays: list[float] = []
    prev_user_ts: Optional[datetime] = None
    for r in records:
        ts = _parse_ts(r.get("timestamp") or "")
        if r["sender"] == "user":
            prev_user_ts = ts
        elif r["sender"] == "friend" and prev_user_ts is not None and ts is not None:
            delta = (ts - prev_user_ts).total_seconds()
            if 0 <= delta <= 86400:
                delays.append(delta)
            prev_user_ts = None
    if delays:
        persona["reply_rhythm"]["avg_delay_seconds"] = int(sum(delays) / len(delays))
    elif parsed is not None and parsed.first_reply_samples:
        # 文本格式里没有绝对时间，延迟来自行尾括号 —— 统计不到 timestamp 时用它。
        persona["reply_rhythm"]["avg_delay_seconds"] = parsed.median_first_delay

    # 口头禅：**统计式蒸馏不猜**（2026-09-29 第十轮修）。
    #
    # 用户实测反馈：「他为什么特别爱说"草"？我的人格聊天记录中的朋友从来不说这个口头禅」。
    #
    # 根因就在这段（改前）：这里写死了一份「候选口头禅」表
    #     ("哈哈", "草", "卧槽", "嗯", "哦", "行吧", "。。。")
    # 然后用 `token in r["content"]` 做**子串**匹配，命中就记进 frequent_words。
    # 两个问题：
    #   1. 这份表是凭空猜的 —— 朋友没说过的词（"草"、"卧槽"）也会被当成他的口头禅，
    #      然后 personality_prompt 把它写进「常用语气词」，模型照着学，于是一嘴"草"。
    #   2. 子串匹配会把「草莓」「草地」「卧槽泥马」里的片段也算命中。
    #
    # 口头禅是**语义特征**，统计拿不到（本文件 3.2.7 的注释里本来就写着
    # 「tone / address / attitude 属于语义特征，统计拿不到，保持蒸馏结果不动」——
    # 这里违背了那条原则）。所以现在改为留空：宁可没有口头禅，
    # 也不要凭空给朋友安一个他从来不说、还很难听的口头禅。
    # 真正读了聊天记录的是模型蒸馏（distill_personality），那条路会填这一项。
    persona["tone"]["frequent_words"] = []
    if friend_name:
        persona["address"]["default"] = ""
    return persona

# ---------------------------------------------------------------- 人格蒸馏

DISTILL_SYSTEM_PROMPT = (
    "你是一个聊天记录分析器。你的任务是分析两个人在即时通讯软件里的聊天记录，"
    "提取其中一方（目标朋友）的人格特征。你只输出 JSON，不输出任何其他文字。"
)

DISTILL_TASK_TEMPLATE = """下面是用户和目标朋友 {name} 的聊天记录（每行格式：说话人|时间|内容，me 表示用户，ta 表示目标朋友）。

{records}

【任务】
分析目标朋友 ta 的说话风格，返回如下 JSON（不要多字段，不要少字段）：

{{
  "tone": {{
    "frequent_words": ["常用语气词，最多5个"],
    "punctuation": "标点习惯，例如：多用省略号，少用句号",
    "sentence_length": "句长偏好，例如：短句为主",
    "emoji_frequency": "表情包使用频率，例如：偶尔"
  }},
  "address": {{
    "default": "ta 对用户的默认称呼，没有就填空字符串",
    "variations": ["其他称呼，最多5个"]
  }},
  "reply_rhythm": {{
    "reply_rate": 0.8,
    "avg_delay_seconds": 120,
    "avg_message_count": 3
  }},
  "schedule": {{
    "active_hours": [20, 21, 22, 23],
    "sleep_hours": [2, 3, 4, 5, 6]
  }},
  "attitude": "ta 对用户的态度，一句话概括",
  "delay_style": {{
    "rule": "用两三句话说明 ta 在什么情况下秒回、什么情况下拖很久，为什么"
  }},
  "proactive_style": {{
    "rule": "用两三句话说明 ta 一般什么时候会主动找你、为什么"
  }}
}}

【规则】
- reply_rate 是 0-1 的小数，表示 ta 回复用户的概率。
- avg_delay_seconds 是 ta 平均隔多少秒回复，整数。
- avg_message_count 是 ta 平均一次连发几条，整数。
- active_hours / sleep_hours 是 0-23 的整数数组，从消息时间推断。
- delay_style.rule 是**重点**：不要只给一个平均延迟。请分析 ta 的回复节奏
  为什么会不同 —— 什么话题/什么时段/什么情绪下 ta 秒回（几秒），
  什么情况下 ta 隔几分钟甚至更久才回。例：
  「问他在干啥这类闲聊，他常隔两三分钟才回，多半在外面骑车；
    但一聊到约见面或有急事，他两三秒就回，还会连发两三条。」
  这段话会被原样带进以后的每次对话，所以必须具体、可执行。
- proactive_style.rule 也是**重点**：看记录里 ta **自己先开口**（不是回你，
  而是主动发第一条）的那些时刻，说明 ta 通常在什么情况下、什么时段
  会主动找人说话，以及多久没联系才可能主动开口。例：
  「他一般晚上十一点多自己发消息过来，多半是刚下班没事干；
    白天基本不会主动找人，除非好几天没说话。」
  这段话会被原样带进以后的主动发起决策，所以必须具体、可执行。
- 只输出 JSON，不要解释，不要 Markdown 代码块。

【延迟数据（从记录行尾括号统计）】
{delay_hint}

【主动开口数据（朋友自己先说话的次数与钟点）】
{proactive_hint}"""

def _record_time(r: dict[str, str]) -> str:
    """给蒸馏模型看的「时间」列。

    2026-09-29 第十三轮修（用户：「修复bug 让他能真的学到人格」）：

    改前这里只取 `r["timestamp"]`，而**文本格式导入的记录里 timestamp 是空串**
    （见 persona_text.parse_persona_text：没有绝对时间时用序号占位）。
    于是模型看到的是 `ta||哈哈哈还行` —— 时间列全空。
    结果就是 prompt 里明写「active_hours / sleep_hours 从消息时间推断」，
    而模型**根本没有时间可推断**，只能瞎猜一个作息出来写进人格档案。

    但 persona_text 其实已经辛苦从 `[2026-09-20 21:30]` 这类行首时间戳里
    抠出了钟点，存在记录的 `hour` 字段里 —— 只是这里没读它。
    现在按 timestamp > hour > 空 的顺序取，模型才真的能「看到」作息数据。
    """
    ts = str(r.get("timestamp") or "").strip()
    if ts:
        return ts
    hour = str(r.get("hour") or "").strip()
    if hour:
        try:
            return f"{int(float(hour)):02d}:00"
        except ValueError:
            return hour
    return ""

def _records_to_lines(records: list[dict[str, str]], max_lines: int = 300) -> str:
    picked = records[-max_lines:] if len(records) > max_lines else records
    lines = []
    for r in picked:
        who = "me" if r["sender"] == "user" else "ta"
        content = r["content"].replace("\n", " ")[:120]
        lines.append(f"{who}|{_record_time(r)}|{content}")
    return "\n".join(lines)

async def distill_personality(
    records: list[dict[str, str]],
    *,
    friend_name: str = "",
    user_id: int = 1,
    ai_profile_id: Optional[int] = None,
    parsed: Optional[persona_text.ParsedPersona] = None,
) -> dict[str, Any]:
    """调用轻量模型蒸馏人格；失败则回退统计式蒸馏。

    :param ai_profile_id: 这个 AI 档案的 id。**必须传** —— 用户的 API Key
        填在会话设置里（`ai_profiles.api_key`），不传这个参数时 api_pool 只能
        去查全局 api_pool 表（客户端没有入口，永远是空表），于是蒸馏 100% 失败、
        每次都退回统计式蒸馏。这就是用户报的「人格蒸馏必失败」。
    :param parsed: 文本解析结果（含首响/段间隔样本）。给定时会把样本与
        统计出的延迟提示一起喂给模型，并把样本原样写回人格档案。
    """
    if not records:
        return default_personality()

    # 内部调用可能给 me/ta 这类别名（见 normalize_records），统一一次。
    records = normalize_records(records)
    if not records:
        return default_personality()

    delay_hint = persona_text.delay_samples_hint(
        (parsed.first_reply_samples + parsed.segment_samples) if parsed else []
    )
    # 主动开口提示（用户 2026-09-28 要求）：告诉模型「朋友自己先开口」出现过几次、
    # 都在几点、两次之间隔多久 —— 它据此写 proactive_style.rule。
    hours = parsed.proactive_hour_set if parsed else []
    gaps = parsed.proactive_gaps if parsed else []
    proactive_count = (
        len([t for t in parsed.turns if t.is_proactive]) if parsed else 0
    )
    if hours or gaps:
        parts = [f"朋友自己先开口共 {proactive_count} 次"]
        if hours:
            parts.append("出现的钟点：" + "、".join(f"{h}点" for h in hours))
        if gaps:
            shown = "、".join(
                f"{g // 60}分钟" if g >= 60 else f"{g}秒" for g in sorted(set(gaps))[:10]
            )
            parts.append(f"两次主动开口的间隔：{shown}")
        proactive_hint = "；".join(parts) + "。"
    else:
        proactive_hint = "（记录里看不出朋友主动开口的规律）"

    messages = [
        {"role": "system", "content": DISTILL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": DISTILL_TASK_TEMPLATE.format(
                name=friend_name or "目标朋友",
                records=_records_to_lines(records),
                delay_hint=delay_hint,
                proactive_hint=proactive_hint,
            ),
        },
    ]

    for attempt in (1, 2):
        try:
            raw = await api_pool.chat(
                messages,
                role="memory",
                user_id=user_id,
                temperature=0.2,
                ai_profile_id=ai_profile_id,
            )
            data = _extract_json(raw)
            persona = normalize_personality(data)
            if parsed is not None:
                # 模型写规则，样本由服务端填 —— 样本是客观数据，不该让模型转述。
                persona["delay_style"]["first_reply_samples"] = parsed.first_reply_samples
                persona["delay_style"]["segment_samples"] = parsed.segment_samples
                persona["proactive_style"]["proactive_hours"] = parsed.proactive_hour_set
                persona["proactive_style"]["proactive_gaps"] = parsed.proactive_gaps
            logger.info("人格蒸馏成功（第 %s 次尝试）", attempt)
            return persona
        except Exception as exc:  # noqa: BLE001 - 蒸馏失败不能阻断上传
            logger.warning("人格蒸馏第 %s 次失败：%s", attempt, exc)

    logger.warning("人格蒸馏两次均失败，回退统计式蒸馏")
    return _statistical_distill(records, friend_name, parsed)

def _extract_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("蒸馏结果不是 JSON 对象")
    return data

async def upload_and_distill(
    *,
    ai_profile_id: int,
    raw_chat_log: Any,
    friend_name: str = "",
    user_id: int = 1,
) -> dict[str, Any]:
    """完整流程：解析文本 -> 蒸馏 -> 落库。

    2026-09-27 第七轮：只接受文本格式（见 persona_text.py）。
    解析出的首响/段间隔样本会一并写进人格档案的 delay_style。
    """
    if isinstance(raw_chat_log, str):
        parsed = persona_text.parse_persona_text(raw_chat_log)
    else:
        parsed = None
    records = parsed.records if parsed is not None else normalize_records(raw_chat_log)
    if not records:
        raise ValueError("聊天记录为空或格式不正确")
    persona = await distill_personality(
        records,
        friend_name=friend_name,
        user_id=user_id,
        ai_profile_id=ai_profile_id,
        parsed=parsed,
    )
    save_personality(ai_profile_id, persona)
    return {
        "records": len(records),
        "personality_data": persona,
    }

# ---------------------------------------------------------------- 持续微调（PRD 3.2.7 / 3.3.4）

# 微调幅度：新数据只按这个比例并入旧人格，避免几次对话就把蒸馏结果冲掉。
REFINE_WEIGHT = 0.1
REFINE_MIN_MESSAGES = 6

def refine_from_conversation(
    ai_profile_id: int, conversation_id: int, *, user_display: str = "用户"
) -> Optional[dict[str, Any]]:
    """按真实对话持续微调人格档案（PRD 3.2.7 / 3.3.4“后续对话中持续微调”）。

    文档没写微调用什么模型、改哪些字段。自行决定并记录：
    - 不额外调用模型（费用控制，PRD 3.2.9），直接从最近的对话消息里做统计式微调；
    - 只微调可统计的三个字段：reply_rhythm.reply_rate、avg_message_count、avg_delay_seconds，
      以及 schedule.active_hours；tone / address / attitude 属于语义特征，
      统计拿不到，保持蒸馏结果不动，避免把人格改坏；
    - 采用指数滑动平均 new = old * (1-w) + observed * w，w = REFINE_WEIGHT；
    - 最近消息少于 REFINE_MIN_MESSAGES 条时不动（样本太小没有意义）。
    返回微调后的人格档案；未微调时返回 None。
    """
    rows = db.list_messages(conversation_id, limit=60)
    if len(rows) < REFINE_MIN_MESSAGES:
        return None

    user_msgs = [r for r in rows if r["sender_type"] == "user"]
    ai_msgs = [r for r in rows if r["sender_type"] == "ai"]
    if not user_msgs or not ai_msgs:
        return None

    persona = load_personality(ai_profile_id)
    rhythm = persona["reply_rhythm"]

    observed_rate = _clamp01(len(ai_msgs) / max(1, len(user_msgs)))
    observed_count = max(1, round(len(ai_msgs) / max(1, len(user_msgs))))
    rhythm["reply_rate"] = round(
        _clamp01(rhythm["reply_rate"] * (1 - REFINE_WEIGHT) + observed_rate * REFINE_WEIGHT),
        3,
    )
    rhythm["avg_message_count"] = max(
        1,
        round(
            rhythm["avg_message_count"] * (1 - REFINE_WEIGHT)
            + observed_count * REFINE_WEIGHT
        ),
    )

    delays: list[float] = []
    for row in rows:
        if row["sender_type"] != "ai":
            continue
        sent = _parse_ts(str(row["timestamp"] or ""))
        created = _parse_ts(str(row["created_at"] or ""))
        if sent is None or created is None:
            continue
        delta = (sent - created).total_seconds()
        if 0 <= delta <= 86400:
            delays.append(delta)
    if delays:
        observed_delay = sum(delays) / len(delays)
        rhythm["avg_delay_seconds"] = max(
            0,
            int(
                rhythm["avg_delay_seconds"] * (1 - REFINE_WEIGHT)
                + observed_delay * REFINE_WEIGHT
            ),
        )

    hours = sorted({ts.hour for ts in (_parse_ts(str(r["timestamp"] or "")) for r in ai_msgs) if ts})
    if hours:
        persona["schedule"]["active_hours"] = hours[:6]

    saved = save_personality(ai_profile_id, persona)
    logger.info(
        "人格持续微调完成：ai=%s reply_rate=%s avg_message_count=%s avg_delay_seconds=%s",
        ai_profile_id,
        saved["reply_rhythm"]["reply_rate"],
        saved["reply_rhythm"]["avg_message_count"],
        saved["reply_rhythm"]["avg_delay_seconds"],
    )
    return saved

# ---------------------------------------------------------------- 人格层文本

def personality_prompt(personality: dict[str, Any], name: str) -> str:
    """按 模板生成人格层文本。"""
    p = normalize_personality(personality)
    tone = p["tone"]
    address = p["address"]
    rhythm = p["reply_rhythm"]
    schedule = p["schedule"]

    words = "、".join(tone["frequent_words"]) if tone["frequent_words"] else "无明显口头禅"
    variations = (
        "、".join(address["variations"]) if address["variations"] else "无"
    )
    lines = [
        "【你的人格设定】",
        f"你叫 {name}，是用户的朋友。你的性格和说话风格如下：",
        "",
        "语气特征：",
        f"- 常用语气词：{words}",
        f"- 标点习惯：{tone['punctuation'] or '无特别习惯'}",
        f"- 句长偏好：{tone['sentence_length'] or '短句为主'}",
        f"- 表情包使用频率：{tone['emoji_frequency'] or '偶尔'}",
        "",
        "称呼方式：",
        f"- 你对用户的默认称呼：{address['default'] or '直接说事，不用称呼'}",
        f"- 你偶尔会用的其他称呼：{variations}",
        "",
        "回复节奏：",
        f"- 你平时回复的概率：{rhythm['reply_rate']}",
        f"- 你平均延迟多久回复：{rhythm['avg_delay_seconds']}秒",
        f"- 你平均一次回几条：{rhythm['avg_message_count']}条",
        "",
        "作息画像：",
        f"- 你通常活跃的时段：{schedule['active_hours'] or '无固定规律'}",
        f"- 你通常睡觉的时段：{schedule['sleep_hours'] or '无固定规律'}",
        "",
        "你对用户的态度：",
        p["attitude"] or "像一个普通朋友那样随意。",
        "",
        "【人格规则】",
        "- 你必须始终按照上述人格说话，不能偏离。",
        "- 如果人格设定与用户当前话题冲突，以人格设定为准。",
        "- 如果人格设定中没有明确某件事，按“像一个真实朋友”来处理。",
    ]
    if rhythm["reply_rate"] < 0.5:
        lines.append(f"- {LOW_REPLY_RATE_HINT}")
    elif rhythm["reply_rate"] >= 0.8:
        lines.append(f"- {HIGH_REPLY_RATE_HINT}")

    # 延迟规则（用户 2026-09-27 第七轮要求）：把「什么情况秒回、什么情况拖很久」
    # 原样带进每次对话。没有规则时退回样本分布，让模型自己看着办。
    style = p.get("delay_style") or {}
    rule = str(style.get("rule") or "").strip()
    samples = list(style.get("first_reply_samples") or []) + list(
        style.get("segment_samples") or []
    )
    if rule or samples:
        lines.append("")
        lines.append("【你的回复节奏（必须模仿）】")
        if rule:
            lines.append(rule)
        if samples:
            unique = sorted(set(samples))
            shown = "、".join(f"{s}秒" for s in unique[:12])
            lines.append(f"你以前出现过的延迟：{shown}。")
        lines.append(
            "回复时按当前语境判断该用哪一档延迟，并在 segment_delays 里逐段给出来。"
            "不要把每段都设成同一个数字 —— 真实的人有时秒回、有时隔很久。"
        )

    # 主动开口节奏（用户 2026-09-28 要求）：把「他什么时候会自己找你」也带进
    # 每次对话，让模型写的主动消息在内容上像那个朋友。
    pstyle = p.get("proactive_style") or {}
    p_rule = str(pstyle.get("rule") or "").strip()
    p_hours = list(pstyle.get("proactive_hours") or [])
    if p_rule or p_hours:
        lines.append("")
        lines.append("【你主动找人说话的规律（必须模仿）】")
        if p_rule:
            lines.append(p_rule)
        if p_hours:
            lines.append(
                "你以前自己先开口的钟点："
                + "、".join(f"{h}点" for h in p_hours)
                + "。"
            )
    return "\n".join(lines)