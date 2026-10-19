"""角色卡加载器（第 36 轮，2026-10-04）。

      取消掉用户自定义人格，全部用同行项目中的。」

## 一张卡有什么

LingChat 的卡是 `data/game_data/characters/<角色>/settings.yml`，本项目抄成
`src/server/cards/<卡名>.yml`，字段保留原名：

| 字段 | 作用 |
| --- | --- |
| `system_prompt` | 人设原文（喂给模型的第一段） |
| `system_prompt_example` | **人设示范对话**——"说话有那味"的关键，比任何形容词都管用 |
| `ai_name` / `ai_subtitle` / `user_name` / `user_subtitle` / `title` | 称呼与展示信息 |
| `info` | 角色简介（App 里可以显示） |

## 「取消用户自定义人格」怎么落地

走卡的 AI **不再使用** App 里那份可编辑人格（`personality_data` 的
语气词 / 称呼 / 回复节奏 / attitude 等），也不再用 `persona.personality_prompt()`。
人格文案 100% 来自卡片文件。`personality_data` 里只留一个 `prompt_script` 当路由开关。

## 依赖

用 PyYAML 读（venv 里已有 6.0.3，已补进 requirements.txt）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

#: 卡片目录：src/server/cards/*.yml
CARD_DIR = Path(__file__).resolve().parent / "cards"

#: 脚本名 → 卡文件名（不带扩展名）。用户的历史标记是 `example`，沿用不动，
#: 免得还要改数据库里的 personality_data。
SCRIPT_TO_CARD: dict[str, str] = {
    "example": "example",
}


def card_path(name: str) -> Path:
    return CARD_DIR / f"{name}.yml"


def load_card(name: str) -> Optional[dict[str, Any]]:
    """读一张卡；文件不存在或格式坏了返回 None（调用方负责兜底）。"""
    path = card_path(name)
    if not path.exists():
        logger.warning("角色卡不存在：%s", path)
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 卡坏了不能让整个聊天挂掉
        logger.error("角色卡解析失败 %s：%r", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def card_for_script(script_name: str) -> Optional[dict[str, Any]]:
    """按脚本名取卡（`prompt_script` → 卡）。"""
    name = SCRIPT_TO_CARD.get((script_name or "").strip())
    if not name:
        return None
    return load_card(name)


def card_text(card: dict[str, Any], key: str, default: str = "") -> str:
    """取卡里的文本字段（自动去尾部空白，缺字段给默认值）。"""
    value = card.get(key)
    if value is None:
        return default
    return str(value).strip()


RULE_FIELD_SUFFIX = "_rules"
RULE_FIELD_ORDER = ("care_rules", "tone_rules")


def rule_fields(card: dict[str, Any]) -> list[str]:
    """卡片里所有非空的 `*_rules` 字段名（按固定顺序）。

    注意判空要用 strip()：YAML 里写了个只有空格/换行的字段也该当成"没写"。
    """
    names = [
        str(k)
        for k in card
        if str(k).endswith(RULE_FIELD_SUFFIX) and str(card.get(k) or "").strip()
    ]
    ordered = [n for n in RULE_FIELD_ORDER if n in names]
    ordered += sorted(n for n in names if n not in RULE_FIELD_ORDER)
    return ordered


def rule_texts(card: dict[str, Any]) -> list[str]:
    """卡片里所有规则字段的正文，按顺序返回（空字段自动跳过）。"""
    out: list[str] = []
    for name in rule_fields(card):
        text = card_text(card, name)
        if text:
            out.append(text)
    return out
