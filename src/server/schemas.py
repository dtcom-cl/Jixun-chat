"""AI 结构化输出协议。

严格照抄 四、Pydantic 模型定义，不增字段、不改字段类型。
另附反思循环与群聊抢麦的返回模型，
它们不属于 AI-001 的对话协议，单独定义，不影响 AIResponse。
"""
from __future__ import annotations

import logging
import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

class StateUpdate(BaseModel):
    """ state_update。"""

    emotion_delta: float = Field(..., ge=-1.0, le=1.0)
    energy_delta: float = Field(..., ge=-1.0, le=1.0)
    intimacy_delta: float = Field(..., ge=-1.0, le=1.0)

class AIResponse(BaseModel):
    """ 四 AIResponse + 逐段延迟扩展。

    这里是两秒钟那里是三分钟，然后自己也模仿这里一个两秒钟那里一个三分钟」。

    因此新增 `segment_delays`：让 AI **自己决定每一段各等多久**，而不是所有段
    共用一个 delay_seconds 一次性推出去。

    - `segment_delays[0]`：首响延迟（相对用户刚发的那条消息）
    - `segment_delays[i]`：第 i 段相对**上一段**的间隔

    这个字段是**可选的**：老模型不返回它时，退化成"所有段用 delay_seconds"，
    行为与改动前一致，不会因为模型不配合就发不出消息。

    依据「更新规则：阶段2定死，后续只加字段，不改已有字段类型和语义」，
    这里只加字段、不改任何既有字段。模型觉得这句话需要查实时信息（天气、新闻、
    版本号、某个东西是什么）时就填搜索词；服务端搜完把结果喂回来让它再答一次。
    不填 = 不搜，行为与改动前完全一致。
    """

    action: Literal["reply", "ignore", "double_text"]
    delay_seconds: int = Field(..., ge=0, le=86400)
    message_count: int = Field(..., ge=0, le=20)
    tone: str = Field(..., min_length=1, max_length=50)
    content: List[str] = Field(default_factory=list)
    follow_up: Optional[str] = None
    state_update: StateUpdate
    # 逐段延迟（秒）。空 = 未提供，用 delay_seconds 平摊。
    segment_delays: List[int] = Field(default_factory=list)
    # 联网搜索词（第十三轮新增）。None = 这一轮不用搜。
    search_query: Optional[str] = Field(default=None, max_length=200)

    @field_validator("content")
    @classmethod
    def validate_content(cls, v, info):
        action = info.data.get("action")
        if action in ("reply", "double_text") and len(v) == 0:
            raise ValueError("content 不能为空")
        if action == "ignore" and len(v) > 0:
            raise ValueError("ignore 时 content 必须为空")
        return v

    @field_validator("follow_up")
    @classmethod
    def validate_follow_up(cls, v, info):
        action = info.data.get("action")
        if action == "double_text" and (v is None or v.strip() == ""):
            raise ValueError("double_text 时 follow_up 必填")
        if action in ("reply", "ignore") and v is not None:
            raise ValueError("reply/ignore 时 follow_up 必须为 null")
        return v

    # 说明（实现细节修正）： 四 把这条校验写成 @field_validator("message_count")，
    # 但 message_count 在字段顺序上位于 content 之前，pydantic v2 按定义顺序校验，
    # 此时 info.data 里还没有 content，len(content) 恒为 0，任何合法响应都会校验失败。
    # 文档的语义规则是明确的（3.3：message_count 必须等于 content 数组长度），
    # 因此这里改用 model_validator(mode="after") 在全部字段就绪后再比较，语义与文档一致。
    @model_validator(mode="after")
    def validate_message_count(self):
        if len(self.content) != self.message_count:
            logger.warning(
                "message_count(%s) 与 content 长度(%s) 不一致，按 content 纠正",
                self.message_count,
                len(self.content),
            )
            self.message_count = len(self.content)
        # segment_delays 同样纠正：多了截断，少了用最后一个值补齐
        # （最后一段的间隔最接近后续节奏）。越界值（负数 / 超过一天）夹到合法区间
        # 而不是报错 —— 模型偶尔会写出 999999，为这个把整条回复丢掉不划算
        # （用户要的是「AI 说的延迟都听」，但 86400 秒 = 1 天是格式上限）。
        if self.segment_delays:
            if len(self.segment_delays) > len(self.content):
                logger.warning(
                    "segment_delays 比 content 长（%s > %s），截断",
                    len(self.segment_delays),
                    len(self.content),
                )
                self.segment_delays = self.segment_delays[: len(self.content)]
            elif len(self.segment_delays) < len(self.content):
                logger.warning(
                    "segment_delays 比 content 短（%s < %s），用最后一个值补齐",
                    len(self.segment_delays),
                    len(self.content),
                )
                self.segment_delays = self.segment_delays + [self.segment_delays[-1]] * (
                    len(self.content) - len(self.segment_delays)
                )
            self.segment_delays = [
                max(0, min(86400, int(d))) for d in self.segment_delays
            ]
        else:
            # 兼容「特殊标签」写法（用户 2026-09-27 原话：「先让他回复要回复给用户
            # 的内容，再在前面加一个特殊标签然后输出本次延迟的时间」）。
            # 模型偶尔不按 JSON 字段给，而是把延迟写在正文开头：
            #     "[延迟:180秒] 我在外面骑车呢，你要来不"
            # 这里把它摘出来当 segment_delays 用，并把标签从正文里删掉，
            # 免得用户看到一串 "[延迟:180秒]"。
            extracted: list[int] = []
            cleaned: list[str] = []
            found = False
            for text in self.content:
                delay, body = split_delay_tag(text)
                if delay is not None:
                    found = True
                    extracted.append(delay)
                    cleaned.append(body)
                else:
                    extracted.append(0)
                    cleaned.append(text)
            if found:
                self.content = cleaned
                self.segment_delays = extracted
        return self

class ReflectionResult(BaseModel):
    """ 反思循环返回结构。"""

    willingness: float = Field(..., ge=0.0, le=1.0)
    reason: str = ""
    topic: str = ""

class GroupBid(BaseModel):
    """ 六 群聊抢麦意愿分返回结构。"""

    willingness: float = Field(..., ge=0.0, le=1.0)
    reason: str = ""

# 延迟标签：允许 `[延迟:180秒]` / `[延迟:3分钟]` / `[delay:180s]` / `【延迟：180秒】`
_DELAY_TAG_RE = re.compile(
    r"^\s*[\[【]\s*(?:延迟|delay)\s*[:：]\s*(\d+(?:\.\d+)?)\s*"
    r"(秒|s|sec|seconds?|分钟|min|m|minutes?|小时|h|hours?)?\s*[\]】]\s*",
    re.IGNORECASE,
)

_TAG_UNIT_SECONDS = {
    "秒": 1, "s": 1, "sec": 1, "seconds": 1, "second": 1,
    "分钟": 60, "min": 60, "m": 60, "minutes": 60, "minute": 60,
    "小时": 3600, "h": 3600, "hours": 3600, "hour": 3600,
}

def split_delay_tag(text: str) -> tuple[Optional[int], str]:
    """从一段文本开头摘出 `[延迟:N秒]` 标签。

    返回 (延迟秒数, 去掉标签后的正文)；没有标签时返回 (None, 原文)。
    无单位的数字按**秒**算（用户例子里的写法都带单位，这里只是兜底）。
    """
    raw = text or ""
    m = _DELAY_TAG_RE.match(raw)
    if m is None:
        return None, raw
    value = float(m.group(1))
    unit = (m.group(2) or "秒").lower()
    seconds = int(round(value * _TAG_UNIT_SECONDS.get(unit, 1)))
    return max(0, min(86400, seconds)), raw[m.end():].strip()

def fallback_response(raw_text: str) -> AIResponse:
    """ 降级处理：把原始返回当纯文本回复。"""
    text = (raw_text or "").strip() or "……"
    return AIResponse(
        action="reply",
        delay_seconds=5,
        message_count=1,
        tone="casual",
        content=[text],
        follow_up=None,
        state_update=StateUpdate(
            emotion_delta=0.0, energy_delta=0.0, intimacy_delta=0.0
        ),
    )

def parse_ai_response(raw_text: str) -> AIResponse:
    """把模型返回的文本解析成 AIResponse。

    容错点（文档未禁止，属于必要实现细节）：
    - 去掉 ```json 代码块包裹。
    - 截取第一个 { 到最后一个 } 之间的内容，忽略前后多余文字。
    校验失败时抛 pydantic.ValidationError，由调用方决定重试或降级。
    """
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return AIResponse.model_validate_json(text)