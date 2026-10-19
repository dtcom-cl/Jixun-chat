"""图片消息处理（用户 2026-09-29 第十三轮要求：「让他可以看得了照片」）。

用户实测的原话：「什么鬼啊我发的是汉堡的图片，你是不是看不了图片 实话实说」，
而 AI 当时的反应是先在 21:10 点评了半天的 KDE 桌面 —— 因为它**根本没看见图**，
是从上一句「装 KDE」猜的。

根因有两层：

1. **base64 被当成正文塞进了 Prompt**。图片消息在库里存的是
   `data:image/jpeg;base64,/9j/4AAQ...` 这一整串，而记忆层
   （memory.format_window）是直接拼 `content` 的 —— 于是模型看到的是几万个
   乱码字符，既看不懂，又白烧 token。
2. **没有任何地方告诉模型「你看不到图」**。它拿不到图，又不好意思承认，
   就开始编 —— 编出来的比「我看不了」更假。

所以这里给两样东西：

- `display_text()`：把 data URL 换成一句干净占位，**所有进 Prompt 的路径都要过它**；
- `supports_vision()` / `to_image_part()`：模型真的支持视觉时，
  按 OpenAI 兼容的多模态格式把原图发给它，让它**真的看得见**。

关于「怎么知道模型支不支持视觉」：文档没写，而 OpenAI 兼容协议里没有能力查询接口。
这里按**模型名**判断（见 `_VISION_HINTS`），判断不了的当成不支持 ——
不支持时模型会如实说「我看不了图」，这是对的行为；
反过来把图发给不支持视觉的接口会直接报 400，那才是真的坏。
用户想用识图就填一个视觉模型名（deepseek 目前没有视觉模型；
可用的如 glm-4v、qwen-vl-max、gpt-4o 等）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("vision")

# 进 Prompt 的占位。用中文方括号，和表情码 `[笑哭]` 同一套写法，模型一眼能认。
IMAGE_PLACEHOLDER = "[图片]"

_DATA_URL_PREFIX = "data:image/"

# 模型名里出现这些片段就当作「支持视觉」。
#
# ⚠️ 这只是**兜底猜测**，不是权威依据。权威依据是服务端 /models 返回的
# `input_modalities`（见 api_pool.config_supports_vision）。
#
_VISION_HINTS = (
    "-vl", "_vl", "vl-", "vision", "visual",
    "gpt-4o", "gpt-4.1", "gpt-5", "chatgpt-4o",
    "claude-3", "claude-4", "claude-sonnet", "claude-opus",
    "gemini",
    "glm-4v", "glm-4.5v", "cogvlm",
    "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qwen3-vl",
    "internvl", "llava", "minicpm-v", "moonshot-v1-vision",
    "step-1v", "yi-vision", "doubao-vision", "ernie-vl",
    # DeepSeek 的视觉模型（名字里不带 vl，只能点名）：
    "deepseek-flash",
    "deepseek-v4-flash",
)

# 明确知道**不收**图片的模型名片段。用于在探测不可用时给出更准的兜底：
# 比如 deepseek-v4-pro 的 input_modalities 只有 text，名字里也没有视觉线索，
# 不点名的话会落到「猜不出来」；点名了就不会被 "deepseek-flash" 的前缀误伤
# （"deepseek-v4-pro" 并不包含 "deepseek-flash"，这里是为了将来加模型时省心）。
_TEXT_ONLY_HINTS = ("deepseek-v4-pro", "deepseek-reasoner", "deepseek-chat")

def is_data_url(text: str) -> bool:
    """这段文本是不是一张 data URL 图片。"""
    return (text or "").lstrip().lower().startswith(_DATA_URL_PREFIX)

def is_image_message(content_type: str, content: str) -> bool:
    """这条消息是不是图片。

    两个判据都要认：协议上 `content_type == "image"`，
    但历史数据里也有 content_type 写成 text、content 却是 data URL 的情况，
    只认 content_type 会漏。
    """
    if (content_type or "").lower() == "image":
        return True
    return is_data_url(content)

def display_text(content_type: str, content: str) -> str:
    """把一条消息变成「能安全写进 Prompt」的文本。

    图片 -> `[图片]`；其余原样返回。**这是唯一允许进 Prompt 的入口**，
    绕开它就会把几万个 base64 字符塞给模型（这正是本轮修的 bug）。
    """
    if is_image_message(content_type, content):
        return IMAGE_PLACEHOLDER
    return content or ""

def supports_vision(model_name: str) -> bool:
    """**只按模型名猜**这个模型收不收图片。判断不了返回 False。

    这是同步的兜底猜测，给「没法联网问服务端」的场合用（单测、离线）。
    真正决策请用 model_supports_vision —— 它会去问服务端的能力清单。

    这个函数以前是唯一判据，而名单里漏了 `deepseek-flash`，
    导致「明明能看图却被告知看不见」。名字猜能力只能当退路。
    """
    name = (model_name or "").strip().lower()
    if not name:
        return False
    if any(hint in name for hint in _TEXT_ONLY_HINTS):
        return False
    return any(hint in name for hint in _VISION_HINTS)

def to_image_part(data_url: str) -> dict:
    """拼 OpenAI 兼容的图片内容块。"""
    return {"type": "image_url", "image_url": {"url": data_url}}

def attach_image(messages: list[dict], data_url: str) -> list[dict]:
    """把最后一条 user 消息升级成多模态（文字 + 图片）。

    只改最后一条：图片是**这一轮**用户发的，历史里的图早就换过好几轮话题了，
    每次都重发既费钱又会让模型盯着旧图不放。
    """
    if not messages or not is_data_url(data_url):
        return messages
    out = [dict(m) for m in messages]
    for message in reversed(out):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = [
                {"type": "text", "text": content},
                to_image_part(data_url),
            ]
        elif isinstance(content, list):
            message["content"] = list(content) + [to_image_part(data_url)]
        logger.info("已把图片作为多模态内容附在最后一条用户消息上")
        break
    return out

__all__ = [
    "IMAGE_PLACEHOLDER",
    "is_data_url",
    "is_image_message",
    "display_text",
    "supports_vision",
    "to_image_part",
    "attach_image",
]
