"""人格对话数据的文本格式解析（用户 2026-09-27 第七轮要求）。

用户给的格式长这样（原话的例子）：

    用户：兄弟你在干啥？
    朋友：我在外面骑车呢，你要来不（3分钟后）

    用户：你在哪里？我也要来（10秒后）

    朋友：我在公园，，（2秒后）
    朋友：这里捡到了300块钱，赶快过来，请你吃串！！（3秒后）

    用户：可以可以！好兄弟一辈子，我来了（5秒后）

解析规则（用户确认过的口径）：

1. 一行 = 一条消息，格式 `说话人：内容`，冒号中英文都认。
2. 行尾括号里的时间是这条消息**相对上一条消息**的间隔，
   认 `3分钟后` / `10秒后` / `180秒` / `3min` / `2小时` 这些写法。
   用户那一行的括号时间全部丢掉，否则用户自己回得快会把 AI 的节奏带偏。
4. **空行**表示一次发言结束。空行之间连续的同一个人发言 = 同一次回复的多段。

由此拆出两类延迟（用户明确要求区分）：

- **首响延迟**：上一条是「用户」时，朋友这条的括号时间；
- **段间隔**：上一条是「朋友」时，朋友这条的括号时间。

两套格式并存时，用户粘了文本进来却被当 JSON 解析失败，提示会很难懂。
只留一种，报错也就能写得直白。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median
from typing import Optional

# 一行：说话人：内容（时间）
_LINE_RE = re.compile(r"^\s*(?P<who>[^：:\n]{1,12})\s*[：:]\s*(?P<body>.+?)\s*$")

# 行尾时间：全角/半角括号都认，单位覆盖秒/分钟/小时与英文缩写。
_TIME_TAIL_RE = re.compile(
    r"[（(]\s*(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>秒|s|sec|secs|second|seconds|"
    r"分钟|分|min|mins|minute|minutes|m|"
    r"小时|时|h|hr|hrs|hour|hours)?\s*后?\s*[）)]\s*$",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "秒": 1, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "分钟": 60, "分": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "m": 60,
    "小时": 3600, "时": 3600, "h": 3600, "hr": 3600, "hrs": 3600,
    "hour": 3600, "hours": 3600,
}

# 行首或独立成行的绝对时间（可选）。
#
# 用户 2026-09-28 要求「主动发消息的时间参照人格聊天记录中的朋友是什么时候主动
# 发消息的」—— 只靠行尾括号的**间隔**是推不出「几点钟」的：间隔只能说明
# 「他隔了 3 分钟才回」，说明不了「他总在晚上 11 点自己找我」。
# 所以导出聊天记录时如果带了时间，这里要能认出来，才能学到他主动开口的钟点。
#
# 认这几种写法（都是导出聊天记录时常见的）：
#     2026-09-20 21:30
#     [2026-09-20 21:30]
#     2026/09/20 21:30
#     2026-09-20 21:30:05
#     21:30                （只有时间，没有日期）
_TS_PREFIX_RE = re.compile(
    r"^\s*[\[\(【]?\s*"
    r"(?:(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*[\]\)】]?\s*)?"
    r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})(?:[:：]\d{2})?"
    r"\s*(?:[\]\)】]\s*)?"
)

# 独立成行的时间分隔线：`--- 2026-09-20 21:30 ---` / `=== 21:30 ===`
_TS_LINE_RE = re.compile(
    r"^\s*[-=—*·]{0,6}\s*"
    r"(?:\[|\()?\s*"
    r"(?:(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*)?"
    r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})(?:[:：]\d{2})?"
    r"\s*(?:\]|\))?\s*[-=—*·]{0,6}\s*$"
)


def _hour_of(text: str) -> Optional[int]:
    """从一行开头（或一整行）里取出小时数；取不到返回 None。"""
    m = _TS_LINE_RE.match(text) or _TS_PREFIX_RE.match(text)
    if m is None:
        return None
    try:
        hour = int(m.group("hour"))
    except (TypeError, ValueError):
        return None
    return hour if 0 <= hour <= 23 else None


def _strip_ts_prefix(text: str) -> str:
    """把行首的绝对时间剪掉，剩下「说话人：内容」。"""
    m = _TS_PREFIX_RE.match(text)
    if m is None:
        return text
    return text[m.end():].lstrip()

# 哪些称呼算「用户」，哪些算「朋友」。
USER_ALIASES = {
    "我", "用户", "自己", "me", "user", "本人", "你", "自己（我）",
}
FRIEND_ALIASES = {
    "朋友", "ta", "他", "她", "对方", "好友", "target", "friend", "assistant",
}

# 延迟样本最多留这么多条：再多只会把 Prompt 撑爆，边际信息为零。
MAX_SAMPLES = 60


@dataclass
class ParsedTurn:
    """一次发言（可能包含多段）。"""

    sender: str                 # "user" | "friend"
    messages: list[str] = field(default_factory=list)
    # 首响延迟（秒）：这一轮第一条相对上一条消息的间隔；用户方恒为 None
    first_delay: Optional[int] = None
    # 段间隔（秒）：第 2 段起各自的间隔
    segment_delays: list[int] = field(default_factory=list)
    # 这一轮是不是**朋友自己先开口**（前面没有用户在说话）。
    #
    # 用户 2026-09-28 原话：「主动发消息的时间参照人格聊天记录中的朋友是什么时候
    # 主动发消息的，AI 学习到之后就按照这个来主动发消息」。
    # 所以「主动开口」必须能从记录里认出来 —— 它就是「朋友说了话，而在此之前
    # 不是用户刚说过话」的那些轮次（聊天记录的第一轮、或空行隔开后的朋友轮）。
    is_proactive: bool = False
    # 这一轮所处的钟点（0-23）；记录里没有绝对时间时为 None。
    hour: Optional[int] = None


@dataclass
class ParsedPersona:
    """解析结果：规整后的消息序列 + 从朋友方抽出的延迟分布。"""

    records: list[dict[str, str]]          # [{sender, content, timestamp}]
    first_reply_samples: list[int]         # 首响延迟样本（秒）
    segment_samples: list[int]             # 段间隔样本（秒）
    turns: list[ParsedTurn] = field(default_factory=list)
    # 朋友「主动开口」时的钟点分布（0-23，可能重复）。
    proactive_hours: list[int] = field(default_factory=list)
    # 朋友两次主动开口之间隔了多久（秒）。同样是离散样本，不做中位数。
    proactive_gaps: list[int] = field(default_factory=list)

    @property
    def proactive_hour_set(self) -> list[int]:
        """朋友主动开口出现过的钟点（去重、升序）。"""
        return sorted(set(self.proactive_hours))

    @property
    def median_first_delay(self) -> int:
        return int(median(self.first_reply_samples)) if self.first_reply_samples else 0

    @property
    def median_segment_delay(self) -> int:
        return int(median(self.segment_samples)) if self.segment_samples else 0


# 说话人名字里不该出现的东西。
#
# 为什么必须判这个：`_LINE_RE` 只要求「冒号前不超过 12 个字」，
# 于是「这里没有一行是「说话人：内容」的格式」这种说明文字也会被当成
# 说话人=「这里没有一行是「说话人」、内容=「内容」的格式」——
# 用户粘错东西时不会得到任何提示，只会静默生成一份垃圾人格。
# 引号、括号、书名号、空格都不可能是昵称里的一部分，拿它们当闸门。
_BAD_NAME_CHARS = "「」『』\"'“”‘’<>《》（）()【】[]{}\\ \t\u3000"


def _looks_like_name(who: str) -> bool:
    """这一小段是不是一个像样的说话人名字。"""
    name = (who or "").strip()
    if not name or len(name) > 12:
        return False
    return not any(ch in _BAD_NAME_CHARS for ch in name)


def _classify(who: str) -> str:
    """把称呼归成 user / friend。认不出的一律当朋友（用户上传的多半是对方）。"""
    name = (who or "").strip().lower()
    if name in USER_ALIASES:
        return "user"
    if name in FRIEND_ALIASES:
        return "friend"
    # 「我」「用户」之外的短称呼：如果包含「我」就当用户，否则当朋友。
    if "我" in name or "user" in name or "me" in name:
        return "user"
    return "friend"


def parse_delay_text(text: str) -> tuple[Optional[int], str]:
    """从一行内容尾部摘出 `（3分钟后）`。返回 (秒数, 去括号后的正文)。"""
    raw = text or ""
    m = _TIME_TAIL_RE.search(raw)
    if m is None:
        return None, raw.strip()
    value = float(m.group("num"))
    unit = (m.group("unit") or "秒").lower()
    seconds = int(round(value * _UNIT_SECONDS.get(unit, 1)))
    return max(0, min(86400, seconds)), raw[: m.start()].strip()


def parse_persona_text(raw: str) -> ParsedPersona:
    """解析用户粘贴的人格对话文本。

    :raises ValueError: 一行都认不出来时（多半是用户粘错了东西），
        交给上层翻成一句能看懂的中文提示。
    """
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    records: list[dict[str, str]] = []
    turns: list[ParsedTurn] = []
    first_samples: list[int] = []
    segment_samples: list[int] = []
    proactive_hours: list[int] = []
    proactive_gaps: list[int] = []

    current: Optional[ParsedTurn] = None
    prev_sender: Optional[str] = None
    seq = 0
    # 累计假时钟（秒）：把行尾括号里的间隔一路加下去，
    # 用它算「朋友两次主动开口之间隔了多久」。
    clock = 0
    last_proactive_clock: Optional[int] = None
    known_hour: Optional[int] = None

    for raw_line in text.split("\n"):
        if not raw_line.strip():
            # 空行 = 一次发言结束
            current = None
            continue

        line = raw_line
        hour = _hour_of(line)
        if hour is not None:
            known_hour = hour
            # 只有「整行就是时间」时才需要剪；带时间戳前缀的消息行剪完继续走解析。
            line = _strip_ts_prefix(line)
            if not line.strip():
                continue

        m = _LINE_RE.match(line)
        if m is None or not _looks_like_name(m.group("who")):
            # 认不出的行（比如用户手写的说明）直接跳过，不因此整份失败。
            continue
        sender = _classify(m.group("who"))
        delay, body = parse_delay_text(m.group("body"))
        if not body:
            continue
        if delay is not None:
            clock += delay

        new_turn = current is None or current.sender != sender
        if new_turn:
            # 「主动开口」= 朋友说了话，而在此之前**不是**用户刚说过话。
            # 两种情形：文件/整段的第一轮；或者空行隔开后又是朋友先开口。
            proactive = sender == "friend" and prev_sender != "user"
            current = ParsedTurn(
                sender=sender,
                is_proactive=proactive,
                hour=known_hour,
            )
            turns.append(current)
            if proactive:
                if known_hour is not None:
                    proactive_hours.append(known_hour)
                if last_proactive_clock is not None:
                    gap = clock - last_proactive_clock
                    if gap > 0:
                        proactive_gaps.append(gap)
                last_proactive_clock = clock
            elif sender == "friend" and delay is not None and prev_sender == "user":
                # 上一轮是用户在说话 -> 这条是「首响」
                current.first_delay = delay
                first_samples.append(delay)
        else:
            if sender == "friend" and delay is not None:
                # 同一个人连着说 -> 段间隔
                current.segment_delays.append(delay)
                segment_samples.append(delay)

        current.messages.append(body)
        prev_sender = sender
        seq += 1
        records.append(
            {
                "sender": sender,
                "content": body,
                # 记录里没有绝对时间，用序号占位；蒸馏模型只看顺序与间隔。
                "timestamp": "",
                "order": str(seq),
                "hour": "" if known_hour is None else str(known_hour),
            }
        )

    if not records:
        raise ValueError(
            "没认出任何对话。格式应该是每行「说话人：内容」，"
            "比如「用户：在干啥？」/「朋友：在外面骑车（3分钟后）」，"
            "空行分隔每一次发言。"
        )

    return ParsedPersona(
        records=records,
        first_reply_samples=first_samples[:MAX_SAMPLES],
        segment_samples=segment_samples[:MAX_SAMPLES],
        turns=turns,
        proactive_hours=proactive_hours[:MAX_SAMPLES],
        proactive_gaps=proactive_gaps[:MAX_SAMPLES],
    )


def delay_samples_hint(samples: list[int]) -> str:
    """把延迟样本写成一句给模型看的话（保留离散度，不做中位数）。"""
    if not samples:
        return "（没有可参考的延迟数据）"
    unique = sorted(set(samples))
    shown = "、".join(f"{s}秒" for s in unique[:12])
    if len(unique) > 12:
        shown += " 等"
    return f"他出现过的延迟有：{shown}（共 {len(samples)} 次）"
