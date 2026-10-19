"""LingChat 输出解析（第 36 轮，2026-10-04）。

搬自他们的 `src-tauri/src/ai_service/message_system/producer.rs`（流式按 `【】` 切句）
与 `processor.rs`（正则拆 情绪 / 正文 / 动作 / 日语翻译 + 文本清洗）。

本项目不用流式切句（我们是 JSON 数组，每一项本来就是一条台词），
所以这里保留的是**正则与清洗规则**，用途有三：

1. **单测**：验证格式规范真的能产出可解析的输出；
2. **日志/校验**：模型跑偏时能在服务端看出来（比如一条里塞了四句话）；
3. **给客户端一份参考实现**：Flutter 那边 `ling_text.dart` 用的是同一套规则。

正则（与他们一致）：

    【([^】]*)】([^【】]*)   情绪标签 + 正文
    （([^）]*)）             动作
    <([^>]*)>               日语翻译（本项目暂不用，保留以便将来双语）

清洗（`fix_ai_generated_text`）：半角括号统一成全角、修未闭合的 `【`、
丢掉**同一轮里逐字重复**的句子（他们的复读去重，短句豁免）。
"""

from __future__ import annotations

import re
from typing import Any, Optional

# 情绪标签 + 紧跟的正文（直到下一个 【）
EMOTION_RE = re.compile(r"【([^】]*)】([^【】]*)")
# 动作（括号）——**只认结尾那一对**。
MOTION_RE = re.compile(r"([（(])([^）)]*)([）)])\s*$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 日语翻译
JAPANESE_RE = re.compile(r"<([^>]*)>")
# 只保留书名号以外的内容（他们 preprocess_text 里的一步）
BOOK_TITLE_RE = re.compile(r"[《》]")

#: 短句豁免：归一化后不足 8 个字不去重（免得误伤「嗯」「好哒」）
DEDUPE_MIN_CHARS = 8


def normalize(text: str) -> str:
    """去掉书名号（对应他们的 preprocess_text 的一部分）。

    **不做**半角→全角括号转换：那一步会把代码 `qsort(a)` 改成 `qsort（a）`，
    用户复制去运行就报错。动作识别改成"判断"而不是"改写"（见 MOTION_RE）。
    """
    return BOOK_TITLE_RE.sub("", text or "")


def fix_unclosed_tags(text: str) -> str:
    """修未闭合的 `【`：补一个 `】`（对应他们的 fix_unclosed_tags，简化版）。"""
    fixed = text
    if fixed.count("【") > fixed.count("】"):
        fixed = fixed.rstrip() + "】" * (fixed.count("【") - fixed.count("】"))
    if fixed.count("（") > fixed.count("）"):
        fixed = fixed.rstrip() + "）" * (fixed.count("（") - fixed.count("）"))
    return fixed


def segment_to_dict(segment: str) -> dict[str, Any]:
    """把一条台词拆成 `{emotion, text, action}`。

    一条台词的形状（服务端的 content 数组项）：
        【情绪】正文（可选动作）

    :return: emotion = 情绪标签原文（没有标签时为空串）；
             text    = 去掉标签与动作之后的正文；
             action  = 动作内容（没有则为空串）。
    """
    item = fix_unclosed_tags(normalize(str(segment or "").strip()))
    emotion = ""
    body = item

    m = EMOTION_RE.match(item)
    if m:
        emotion = m.group(1).strip()
        # 一条台词里可能有多个【情绪】段（模型偶尔会这样），后面的都当正文
        body = EMOTION_RE.sub(lambda mm: mm.group(2), item)

    action = ""
    am = MOTION_RE.search(body)
    if am:
        opener, inner, _closer = am.group(1), am.group(2), am.group(3)
        # 全角括号一律当动作；半角括号要求里面是中文，否则那是代码（f(x)、a[i]）
        if opener == "（" or _CJK_RE.search(inner):
            action = inner.strip()
            if action:
                body = (body[: am.start()] + body[am.end() :]).strip()

    return {"emotion": emotion, "text": body.strip(), "action": action}


def parse_segments(items: Any) -> list[dict[str, Any]]:
    """解析一次回复的 content 数组。"""
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, (list, tuple)):
        return []
    return [segment_to_dict(x) for x in items]


def has_emotion_tag(segment: str) -> bool:
    """这条台词有没有按规范写【情绪】（校验用）。"""
    return EMOTION_RE.match(normalize(str(segment or "").strip())) is not None


def dedupe_repeated(items: list[str]) -> list[str]:
    """丢掉同一轮里逐字重复的句子（忽略空白），短句豁免。

    对应他们的 producer：模型在多轮工具调用间会把开场白复读一遍。
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        key = re.sub(r"\s+", "", str(raw or ""))
        if len(key) < DEDUPE_MIN_CHARS:
            out.append(raw)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def format_report(items: Any) -> str:
    """给日志用的一行摘要：几条台词、情绪分别是什么、有没有动作。"""
    segs = parse_segments(items)
    emotions = [s["emotion"] or "?" for s in segs]
    actions = sum(1 for s in segs if s["action"])
    return "台词 %d 条 | 情绪 %s | 带动作 %d 条" % (
        len(segs),
        "/".join(emotions),
        actions,
    )
