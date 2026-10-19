"""状态机。

职责：维护每个 AI 角色的情绪（emotion）、能量（energy）、亲密度（intimacy）。

- 状态机
- 状态机驱动的回复生成
- 服务端应用规则（clamp 到 0~1）
- 服务端二次调整规则（最终延迟 = clamp(AI 值, min_delay, max_delay)）

文档未写死的数值，按 冲突处理规则自行决定并记录：
0. 用户持续发消息时的能量变化（PRD 3.3.3“能量可能因被打扰而进一步降低，也可能因被需要
   而回升，取决于人格设定”）：文档只说“取决于人格设定”，没写具体依据。这里用
   personality.reply_rhythm.reply_rate 当判据——回复率高的角色把用户持续发消息理解为
   “被需要”，能量回升；回复率低的角色理解为“被打扰”，能量下降；中间区间不变化。
   单次变化量 DISTURBANCE_ENERGY_DELTA = 0.06，只在“连续 ≥2 条用户消息未被回复”时生效。
1. 情绪基线 0.5（PRD 3.2.2 明写“缓慢衰减回归基线0.5”）。
2. 能量不对话时缓慢恢复（PRD 3.2.2“后台持续衰减或恢复”，ARCH 4.3.1“能量慢慢恢复”），
   恢复到基线 0.5，不是恢复到 1.0。
3. 亲密度不衰减（ARCH 4.3.1“亲密度不变”）。
4. 衰减定时任务间隔 DECAY_INTERVAL_SECONDS = 60 秒，每次按速率向基线靠拢。
5. 延迟区间映射：
   - 能量低 + 情绪低落 → min_delay=300, max_delay=7200
   - 能量高 + 情绪愉悦 → min_delay=0,   max_delay=60
   定义 readiness = (energy + emotion) / 2，在 [0,1] 上线性插值。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import database as db

logger = logging.getLogger("state_machine")

EMOTION_BASELINE = 0.5
ENERGY_BASELINE = 0.5

# 后台衰减/恢复：每次调用按此比例向基线靠拢
DECAY_RATE = 0.05
DECAY_INTERVAL_SECONDS = 60

# 延迟区间两端
MIN_DELAY_WORST = 300
MAX_DELAY_WORST = 7200
MIN_DELAY_BEST = 0
MAX_DELAY_BEST = 60

# 被打扰 / 被需要（PRD 3.3.3）
DISTURBANCE_ENERGY_DELTA = 0.06
# 连续未被回复的用户消息达到这个条数才触发（第 1 条不算“持续发消息”）
DISTURBANCE_MIN_UNANSWERED = 2
NEEDED_REPLY_RATE_THRESHOLD = 0.8
DISTURBED_REPLY_RATE_THRESHOLD = 0.5

def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))

@dataclass(frozen=True)
class State:
    emotion: float
    energy: float
    intimacy: float

    def as_dict(self) -> dict:
        return {
            "emotion": round(self.emotion, 4),
            "energy": round(self.energy, 4),
            "intimacy": round(self.intimacy, 4),
        }

def get_state(ai_profile_id: int) -> State:
    """读取某个 AI 的当前状态（任务2.5：三个字段可读写）。"""
    row = db.get_ai_profile(ai_profile_id)
    if row is None:
        raise ValueError(f"AI 档案不存在：{ai_profile_id}")
    return State(
        emotion=clamp01(row["state_emotion"]),
        energy=clamp01(row["state_energy"]),
        intimacy=clamp01(row["state_intimacy"]),
    )

def set_state(ai_profile_id: int, state: State) -> State:
    """直接写入状态（任务2.5：能手动修改三个值，值限制在 0-1）。"""
    fixed = State(
        emotion=clamp01(state.emotion),
        energy=clamp01(state.energy),
        intimacy=clamp01(state.intimacy),
    )
    db.update_ai_state(
        ai_profile_id,
        emotion=fixed.emotion,
        energy=fixed.energy,
        intimacy=fixed.intimacy,
    )
    return fixed

def apply_update(ai_profile_id: int, state_update) -> State:
    """应用 AI 返回的 state_update。

    new = clamp(old + delta, 0, 1)。无论 action 是什么都要应用，
    ignore 时状态照常更新是已读不回逻辑的核心。
    """
    old = get_state(ai_profile_id)
    new = State(
        emotion=clamp01(old.emotion + state_update.emotion_delta),
        energy=clamp01(old.energy + state_update.energy_delta),
        intimacy=clamp01(old.intimacy + state_update.intimacy_delta),
    )
    db.update_ai_state(
        ai_profile_id,
        emotion=new.emotion,
        energy=new.energy,
        intimacy=new.intimacy,
    )
    logger.info(
        "状态更新 ai=%s emotion %.3f->%.3f energy %.3f->%.3f intimacy %.3f->%.3f",
        ai_profile_id,
        old.emotion,
        new.emotion,
        old.energy,
        new.energy,
        old.intimacy,
        new.intimacy,
    )
    return new

def disturbance_effect(reply_rate: float) -> float:
    """按人格决定“用户持续发消息”时能量该涨还是该跌（PRD 3.3.3）。

    - reply_rate >= 0.8：把用户持续发消息理解为“被需要”，能量回升。
    - reply_rate <= 0.5：理解为“被打扰”，能量下降。
    - 中间区间：人格没给出明确倾向，不变化。
    """
    rate = clamp01(reply_rate)
    if rate >= NEEDED_REPLY_RATE_THRESHOLD:
        return DISTURBANCE_ENERGY_DELTA
    if rate <= DISTURBED_REPLY_RATE_THRESHOLD:
        return -DISTURBANCE_ENERGY_DELTA
    return 0.0

def apply_disturbance(ai_profile_id: int, *, reply_rate: float) -> Optional[float]:
    """应用一次“被打扰 / 被需要”的能量变化（PRD 3.3.3）。

    只改能量，不动情绪与亲密度；变化量为 0 时不动数据库并返回 None。
    """
    delta = disturbance_effect(reply_rate)
    if abs(delta) < 1e-9:
        return None
    old = get_state(ai_profile_id)
    new_energy = clamp01(old.energy + delta)
    if abs(new_energy - old.energy) < 1e-9:
        return None
    db.update_ai_state(
        ai_profile_id, emotion=old.emotion, energy=new_energy, intimacy=old.intimacy
    )
    logger.info(
        "被打扰/被需要：ai=%s reply_rate=%.2f energy %.3f->%.3f",
        ai_profile_id,
        reply_rate,
        old.energy,
        new_energy,
    )
    return new_energy

def decay_step(state: State, rate: float = DECAY_RATE) -> State:
    """一次后台衰减/恢复。纯函数，便于测试。

    - 情绪向 0.5 靠拢
    - 能量向 0.5 靠拢（低能量恢复、高能量回落）
    - 亲密度不变
    """
    return State(
        emotion=clamp01(state.emotion + (EMOTION_BASELINE - state.emotion) * rate),
        energy=clamp01(state.energy + (ENERGY_BASELINE - state.energy) * rate),
        intimacy=state.intimacy,
    )

def decay_all(rate: float = DECAY_RATE) -> int:
    """对所有启用中的 AI 执行一次衰减，返回处理数量（后台定时任务调用）。"""
    count = 0
    for row in db.list_ai_profiles():
        current = State(
            emotion=clamp01(row["state_emotion"]),
            energy=clamp01(row["state_energy"]),
            intimacy=clamp01(row["state_intimacy"]),
        )
        nxt = decay_step(current, rate)
        if (
            abs(nxt.emotion - current.emotion) > 1e-9
            or abs(nxt.energy - current.energy) > 1e-9
        ):
            db.update_ai_state(
                int(row["id"]),
                emotion=nxt.emotion,
                energy=nxt.energy,
                intimacy=nxt.intimacy,
            )
            count += 1
    if count:
        logger.info("后台状态衰减完成，更新 %s 个 AI 档案", count)
    return count

def delay_range(state: State) -> tuple[int, int]:
    """由状态决定延迟区间 [min_delay, max_delay]。

    2026-09-27 起**生产链路不再使用**这个区间做夹取，只保留给阶段2的回归测试
    （test_phase2_state_machine.py 断言了这两端极值），以及作为 prompt_builder
    给模型的一句参考。真正决定延迟的是 AI 自己（见 resolve_delay）。
    """
    readiness = clamp01((state.energy + state.emotion) / 2.0)
    min_delay = MIN_DELAY_WORST + (MIN_DELAY_BEST - MIN_DELAY_WORST) * readiness
    max_delay = MAX_DELAY_WORST + (MAX_DELAY_BEST - MAX_DELAY_WORST) * readiness
    return int(round(min_delay)), int(round(max_delay))

def clamp_delay(ai_delay_seconds: int, state: State) -> int:
    """把延迟夹进状态区间。

    ⚠️ 2026-09-27 起**已从生产链路移除**，只保留给阶段2回归测试。

    为什么必须撤掉：状态默认值（energy=0.5, emotion=0.5）下这个区间是
    150~3630 秒，AI 就算说「立即回」也会被抬到 2 分 30 秒 —— 用户实测到的
    「固定延迟两分钟」就是这个。而那个延迟本该是 AI 从上传的聊天记录里
    学来的朋友节奏，服务端不该替他改。
    """
    lo, hi = delay_range(state)
    if hi < lo:
        hi = lo
    return int(max(lo, min(hi, int(ai_delay_seconds))))

# AI 单条消息延迟的**格式**上限（秒）。这不是业务夹取，是安全边界：
# 模型偶尔会吐 999999999，若不拦，execute_at 会算到几百年后，
# 那条消息就永远沉在库里。86400 = 1 天，与 的字段定义一致。
FORMAT_MAX_DELAY_SECONDS = 86400

def resolve_delay(ai_delay_seconds: int) -> int:
    """AI 说等多久就等多久（用户 2026-09-27 明确要求）。

    只做两件事：负数归零、超过 1 天的按 1 天算（格式边界，不是业务夹取）。
    状态机不再参与延迟决策 —— 它只影响 Prompt 里的语气描述。
    """
    return int(max(0, min(FORMAT_MAX_DELAY_SECONDS, int(ai_delay_seconds))))

# ---------------------------------------------------------------- 亲密度（PRD 3.2.2 / 3.3.3）

INTIMACY_PER_TURN = 0.002
# 一轮里 AI 说的段数多，说明聊得深，按段数给一点点加成（每段 +0.0005，最多 +0.004）。
INTIMACY_PER_EXTRA_SEGMENT = 0.0005
INTIMACY_SEGMENT_BONUS_CAP = 0.004

def accumulate_intimacy(
    ai_profile_id: int, *, segments: int = 1, extra: float = 0.0
) -> Optional[float]:
    """一轮有效对话后的基础亲密度累积（PRD 3.2.2「随对话频率和深度缓慢积累」）。

    :param segments: 这一轮 AI 实际回了几段，段数多说明聊得深。
    :param extra: 额外增量（比如用户催了之后 AI 秒回，属于正向互动）。
    :return: 新的亲密度；没有变化时返回 None。
    """
    gain = INTIMACY_PER_TURN + extra
    gain += min(
        INTIMACY_SEGMENT_BONUS_CAP,
        max(0, segments - 1) * INTIMACY_PER_EXTRA_SEGMENT,
    )
    old = get_state(ai_profile_id)
    new_intimacy = clamp01(old.intimacy + gain)
    if abs(new_intimacy - old.intimacy) < 1e-9:
        return None
    db.update_ai_state(
        ai_profile_id,
        emotion=old.emotion,
        energy=old.energy,
        intimacy=new_intimacy,
    )
    logger.info(
        "亲密度累积：ai=%s %.4f -> %.4f（segments=%s）",
        ai_profile_id,
        old.intimacy,
        new_intimacy,
        segments,
    )
    return new_intimacy

# 亲密度五档：客户端在聊天页右上角显示成百分值 + 一句人话。
INTIMACY_LEVELS: tuple[tuple[float, str], ...] = (
    (0.15, "陌生"),
    (0.35, "认识"),
    (0.60, "普通朋友"),
    (0.80, "亲近"),
    (1.01, "很亲密"),
)

def intimacy_label(intimacy: float) -> str:
    """把 0~1 的亲密度翻成一句人话（用户在聊天页能看见「关系 37% 普通朋友」）。"""
    value = clamp01(intimacy)
    for threshold, label in INTIMACY_LEVELS:
        if value < threshold:
            return label
    return INTIMACY_LEVELS[-1][1]

def intimacy_percent(intimacy: float) -> int:
    """亲密度百分值（0~100，四舍五入）。用户要求「关系变好是一个百分值」。"""
    return int(round(clamp01(intimacy) * 100))

# 亲密度对「主动开口阈值」的影响（PRD 3.2.2：亲密度影响角色主动开口的阈值）。
#
# 2026-09-29 第十轮**从 0.4 降到 0.2**。原因（用户实测「他真的能主动找我聊天吗？
# 为什么一直不找？我都等烦了」）：
#   系数 0.4 时亲密度 0 → 阈值 +0.2，再叠加「不在活跃时段 +0.1」，
#   基础 0.6 直接变成 0.9。而实测模型给出的 willingness 上限只有 0.72 左右
#   （见 prompt_builder.build_reflection_prompt 的说明）—— 阈值比模型能给的
#   最高分还高，这个功能等于永远不会触发。而新关系恰恰是最需要它主动开口的阶段，
#   不能再让低亲密度把它压死。
# 现在：亲密度 0 → +0.1，1 → -0.1，0.5 → 不增不减。
INTIMACY_THRESHOLD_WEIGHT = 0.2

def threshold_shift_from_intimacy(intimacy: float) -> float:
    """亲密度带来的主动开口阈值偏移量（负数=更容易开口）。"""
    return -(clamp01(intimacy) - 0.5) * INTIMACY_THRESHOLD_WEIGHT

def ignore_bias(emotion: float, energy: float, intimacy: float) -> float:
    """已读不回的倾向分（PRD 3.3.3）。

    能量低、情绪低落、或**亲密度未达阈值**时，action 更可能为 ignore。
    返回 0~1 的倾向分，越高越该沉默；dispatcher 用它做一次硬约束
    （倾向分很高且模型说要 ignore 时，就不再重试逼它回话）。
    """
    readiness = clamp01((emotion + energy) / 2.0)
    low_intimacy = 1.0 - clamp01(intimacy / 0.35)  # 亲密度 0.35 以上不再惩罚
    return clamp01((1.0 - readiness) * 0.7 + max(0.0, low_intimacy) * 0.3)