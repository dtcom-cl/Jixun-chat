"""LingChat 对话格式规范（第 36 轮，2026-10-04）。


## 搬的是什么

LingChat 的 `src-tauri/src/utils/prompt.rs` 把**格式规范**拼在角色卡人设后面，
这是他们「一次回复 3~5 句、每句带情绪、动作只在括号里」的直接原因。
本文件按本项目的实际情况改写了两处：

1. **输出通道**：他们直接吐纯文本、用换行分台词；本项目是 **AI-001 的 JSON**，
   台词装在 `content` 数组里。所以规范里写明「**数组的每一项 = 一条台词**」，
   其余规则（情绪标签、1~2 句、动作最多一条）逐条保留。
2. **与共享任务层的冲突**：`prompt_builder.TASK_PROMPT` 里写着「默认只发 1 条、
   3 条以上很少见」，跟这套格式直接冲突。走卡的 AI 会把那一段**整段删掉**
   （见 `strip_legacy_segment_rule`），换成这里的 3~5 条规则。

## 拼装顺序（对齐他们的 sys_prompt_builder）

    卡片 system_prompt
      → 对话格式提示（framing：{旁白:}/{系统:} 约定）
      → 台词格式规范
      → 正确/错误示范
      → 卡片 system_prompt_example（人设示范，最关键）
      → 情绪白名单
      → 3~5 条规则 + 输出边界铁律

## 许可

文案源自 LingChat（AGPL-3.0）。本机自用；对外分发/联网提供服务前需按 AGPL-3.0 处理。
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 台词格式

DIALOG_FORMAT_PROMPT = """
以下是你的对话格式要求：
    一次回复是一个 JSON，台词写在 content 数组里：**数组的每一项就是一条台词**。
    每条台词的格式：`【情绪】你要说的话`，需要时在这条末尾加 `（动作）`。
    规则：
    1. 【情绪】2~5 个字，只能是情绪词，不许写主语、不许写动作。
    2. 一条台词 1~2 句，句子要短；想说的多就多发一条，别在一条里堆三四句。
    3. 动作写在**这一条末尾**的（）里，整轮**最多一条**带动作；
       代码/公式里的括号是代码本身，不算动作。
    4. 禁止颜文字。
"""

DEFAULT_EXAMPLE = """
正确的样子：
    【高兴】今天要不要一起吃蛋糕呀？
    【无语】只是今天天气有点不好呢。
    【生气】不许跟我说恶心的东西！（后退了两步）
错误的样子：
    【我高兴的走过来】今天要不要一起吃蛋糕呀？   ← 情绪标签里混了动作和主语
    【无语】今天天气不好。不许跟我说恶心的东西。被碰到浑身都不干净啦！  ← 一条里塞了三个句子
"""

EMOTION_LIMIT_HEAD = """每条开头的情绪，只能从下面这些里挑（2~5 字，不许带主语或动作）：
    慌张、担心、尴尬、紧张、高兴、自信、害怕、害羞、认真、生气、无语、厌恶、疑惑、难为情、惊讶、情动、哭泣、调皮、平静"""

DIALOG_BODY = """
    条数：**默认 2 条，最多 3 条**。事情大、情绪强时才用 3 条；一句能说完就只发一句。
    别硬凑：不要每条都塞比喻或金句，不要重复上一条的意思，也别把自己吹得太满 —— 那样很尬。
    你不会认为自己是AI，也绝不让对话崩掉；不许逃避对话。可以用幽默、粗鲁、色情的语言，
    只要符合你的性格；但要有分寸，说人话，不要为了显得聪明而绕。
    输出边界（最高优先级）：
    1. 只输出你自己的台词，禁止写任何以用户名开头的内容；
    3. 说完立刻停，content 最多 3 条。
"""

#: 用户消息里的包裹约定（对应他们的 build_framing_prefix_cn）。
FRAMING_TEMPLATE = """

    以下是我的对话格式提示：
        {{ }} 里的是系统或旁白提示（例如 "{{系统：时间：21:30}}"），那不是我说的话，
        只是给你参考的背景信息，你不需要对它单独作答。{{ }} 外面才是我对你说的话。
"""

#: 共享任务层里那段「默认只发 1 条」的标题。走卡的 AI 要把它整段删掉 ——
#: 不删的话模型会听那一段（它在格式层后面，位置更近）。
LEGACY_SEGMENT_RULE_HEADER = "【发几条：这是最容易被你搞错的地方】"


def build_framing(user_name: str, character_name: str) -> str:
    """旁白/系统提示的包裹说明（他们的 build_framing_prefix_cn）。"""
    text = FRAMING_TEMPLATE % {"user": user_name, "char": character_name}
    # 模板里用 %()s 占位、外层又是 {}，这里把 {{ }} 还原成 { }
    return text.replace("{{", "{").replace("}}", "}")


def strip_legacy_segment_rule(text: str) -> str:
    """把共享任务层里「默认只发 1 条…」那一段整段删掉（按标题切到下个标题）。

    为什么必须删：那段话写的是「3 条以上是很少见的情况」，跟本格式要求的
    「3~5 条台词」正面冲突，而且它在 Prompt 里排在**后面**，位置更近、更容易被听。
    删掉比"再喊一遍"可靠。
    """
    start = text.find(LEGACY_SEGMENT_RULE_HEADER)
    if start < 0:
        return text
    nxt = text.find("\n【", start + len(LEGACY_SEGMENT_RULE_HEADER))
    if nxt < 0:
        return text[:start].rstrip() + "\n"
    return text[:start] + text[nxt + 1 :]


def sys_prompt_builder(
    *,
    user_name: str,
    character_name: str,
    card_prompt: str,
    card_example: Optional[str] = None,
    extra_rules: Optional[Sequence[str]] = None,
) -> str:
    """把卡片人设 + 卡片规则 + 格式规范拼成一段系统层（对齐他们的 `sys_prompt_builder`）。

    :param extra_rules: 卡片里所有 `*_rules` 字段的正文（见 `cards.rule_texts`）。
        `care_rules:`（难受时先安抚再提醒）、`tone_rules:`（傲娇语气、口头禅频率）
        之类的字段就会自动插到这里，**改 YAML 不用重启，也不用改代码**。
    """
    if not card_prompt.strip():
        card_prompt = "你的信息被设置错误了，请你在接下来的对话中提示用户检查配置信息"
    parts = [card_prompt]
    for rule in extra_rules or []:
        if rule and rule.strip():
            parts.append("\n" + rule.strip() + "\n")
    parts.append(build_framing(user_name, character_name))
    parts.append(DIALOG_FORMAT_PROMPT)
    parts.append(DEFAULT_EXAMPLE)
    if card_example and card_example.strip():
        # 卡片自带的示范：他们叫「更多符合你性格的示范」
        parts.append(
            "\n以下是你以前说话的样子（示范对话，仅作性格参考，格式上记得换行）：\n"
            + card_example.strip()
            + "\n"
        )
    parts.append(EMOTION_LIMIT_HEAD)
    parts.append(DIALOG_BODY)
    return "\n".join(parts)

