"""配置模块。

集中存放服务端的所有常量：路径、端口、心跳、延迟队列轮询间隔。

- 七、技术选型（FastAPI + WebSockets + SQLite）
- 一、协议总则（ws://{host}:{port}/ws，心跳 30s，超时 90s）
- 一、数据库选型（SQLite，data/app.db）

决策记录：
1. 阶段1不做注册登录（PRD 5.3 不做清单），因此固定一个默认用户和一个默认单聊会话，
   客户端直接使用 conversation_id = 1，不额外做会话列表接口。
2. 阶段1还没有状态机（阶段2任务2.5才引入），1.14 的写死回复使用固定延迟
   FIXED_REPLY_DELAY_SECONDS。阶段2接上状态机后由状态机决定延迟。
3. 时区固定使用东八区（+08:00），与 的 timestamp 示例一致。
"""

import json
import os
from pathlib import Path

# ---------------------------------------------------------------- 路径

# config.py 位于 <项目根>/src/server/config.py，向上两级即项目根目录
BASE_DIR = Path(__file__).resolve().parents[2]

# 路径支持环境变量覆盖，供自动化测试把数据库/向量库隔离到临时目录，
# 避免测试污染 data/app.db。生产运行不设这些变量时行为不变。
_DATA_DIR_ENV = os.environ.get("DSH_DATA_DIR")
DATA_DIR = Path(_DATA_DIR_ENV) if _DATA_DIR_ENV else BASE_DIR / "data"
DB_PATH = Path(os.environ.get("DSH_DB_PATH") or (DATA_DIR / "app.db"))
LOG_DIR = Path(os.environ.get("DSH_LOG_DIR") or (BASE_DIR / "logs"))
VECTOR_DIR = Path(
    os.environ.get("DSH_VECTOR_DIR") or (DATA_DIR / "lancedb")
)

# 上传的聊天记录
UPLOAD_DIR = Path(os.environ.get("DSH_UPLOAD_DIR") or (DATA_DIR / "uploads"))

# ---------------------------------------------------------------- 服务端

HOST = "0.0.0.0"
PORT = 8000
WS_PATH = "/ws"

# ---------------------------------------------------------------- WebSocket 协议（WS-001）

HEARTBEAT_INTERVAL_SECONDS = 30  # 双方每 30 秒发一次 heartbeat
HEARTBEAT_TIMEOUT_SECONDS = 90  # 90 秒未收到心跳视为断线

# ---------------------------------------------------------------- 延迟队列

DELAY_QUEUE_POLL_INTERVAL_SECONDS = 1.0  # worker 轮询间隔
DELAY_TASK_MAX_RETRIES = 3  # 与 的 max_retries 默认值一致

# ---------------------------------------------------------------- 固定回复（回归测试用）

DEFAULT_USER_ID = 1
DEFAULT_CONVERSATION_ID = 1
# 兼容旧测试的常量：仍然指向“第一个 AI 档案”，但生产环境不再创建默认档案了。
# 见 database.bootstrap_defaults 的 2026-09-27 改动说明。
DEFAULT_AI_PROFILE_ID = 1

# 历史遗留的默认 AI 名字。生产环境**不再创建**这个 AI（用户 2026-09-27
# 要求“把这个永久删掉再也不要重新出现”），这个常量现在的唯一用途是
# 启动时交给 database.purge_legacy_default_ai() 做一次清理匹配。
DEFAULT_AI_NAME = "小真"

# ---------------------------------------------------------------- 写死回复（仅测试/回归用）

# 2026-09-27 用户明确要求“必须是真的 AI 对话，不要再用固定脚本写了”。
# 因此写死回复从“默认路径”降级为“必须显式打开的测试开关”：
#   - 默认关闭 → 生产环境物理上无法再吐「刚在打游戏」这类固定台词；
#   - 只有把 DSH_FIXED_REPLY=1 打开（回归测试自行开启）才会走这条分支。
# 保留这段代码，因为多段 content、无 Key 也能跑的场景仍依赖它，
# 删除后相应断言会失效。
FIXED_REPLY_ENABLED = os.environ.get("DSH_FIXED_REPLY", "0") == "1"
FIXED_REPLY_DELAY_SECONDS = 1
FIXED_REPLY_CONTENT = ["刚在打游戏", "你找我干嘛", "？"]

# ---------------------------------------------------------------- 后续功能新增配置

# 管线开关。2026-09-27 起**默认 1**：收到消息走完整管线
#   状态机决定延迟 → 组装 Prompt（人格+状态+记忆+上下文）
#   → 调真实 API 解析结构化 JSON → 按 action 执行 → 延迟队列推送
# 同时启用后台状态衰减与后台反思循环。
#
# 需要固定回复（仅回归测试）时设 DSH_FIXED_REPLY=1，
# 与 USE_LLM 无关——即便 USE_LLM=0，写死回复也不会再自动启用。
# 设 DSH_USE_LLM=0 退回简化模式（此时若无固定回复开关，AI 不回复）。
USE_LLM = os.environ.get("DSH_USE_LLM", "1") == "1"

# 单条消息的延迟上限（秒）。 允许 delay_seconds 到 86400，
# 但实际推送延迟由状态机 clamp 后不会超过 7200；这里再加一道兜底上限。
MAX_REPLY_DELAY_SECONDS = 7200

# ---------------------------------------------------------------- 「催」机制（用户 2026-09-27 第七轮）
#
URGE_MIN_ADVANCE_SECONDS = 1
URGE_MAX_ADVANCE_SECONDS = 3
URGE_FORCE_REPLY_AFTER = 2

# 命中这些词就认为用户在催（不区分大小写、忽略空格）。
# 覆盖用户举的例子 + 常见变体；命中即触发，不再额外调模型判断
# （调一次模型判断「这算不算催」既慢又费钱，而误判的代价只是提前回复一次）。
URGE_KEYWORDS: tuple[str, ...] = (
    "赶紧回", "快点回", "快回", "回我", "回话", "说话", "说句话", "在吗", "在么", "在不在",
    "哑巴", "不理我", "又不理", "你怎么不回", "为什么不回", "怎么不回", "死了吗", "人呢",
    "你他妈", "他妈", "妈的", "草泥马", "你妈", "滚出来", "出来", "催", "急", "等着呢",
    "喂", "hello?", "hi?", "??",
)

# PRD 3.3.3 的 ignore 硬约束阈值（见 dispatcher 的 2b 步）：
# ignore 倾向分低于这个值时，模型选 ignore 会被服务端改成一句最短回复。
# 0.15 是自行决定的经验值：只有「情绪好 + 能量足 + 关系不疏远」三条同时成立
# 才会落在这个值以下，此时装死确实不合理。
IGNORE_BLOCK_BIAS = 0.15

# double_text 的 follow_up 延迟区间
DOUBLE_TEXT_MIN_DELAY_SECONDS = 30
DOUBLE_TEXT_MAX_DELAY_SECONDS = 300

# ---------------------------------------------------------------- 主动发起

REFLECTION_INTERVAL_SECONDS = 60  # 后台反思循环触发间隔
REFLECTION_IDLE_SECONDS = 120  # 用户静默多久后才进入反思
REFLECTION_WILLINGNESS_THRESHOLD = 0.6  #：超过 0.6 才可能开口
PROACTIVE_COOLDOWN_SECONDS = 1800  # 两次主动发起之间的冷却时间
PROACTIVE_DAILY_LIMIT = 5  #：每天主动发起不超过 5 次

# 主动发起（PRD 1.4 / 3.2.6 / 3.2.9 / 3.3.1 / 4.2 验收标准「AI能主动发起聊天」）。
#
# 2026-09-27 第五轮曾按用户当时那句「我们还会自动发消息，太恶心了」默认关闭；
# 主动发起默认开启，避免长期只做被动回复。
# 设 DSH_PROACTIVE=0 可以关掉（比如调试时不想被后台消息打断）。
PROACTIVE_ENABLED = os.environ.get("DSH_PROACTIVE", "1") == "1"

# 启动时清理历史遗留的默认 AI「小真」（database.purge_legacy_default_ai）。
# 生产默认打开；自动化测试里 TestClient(main.app) 会触发 lifespan，若不关掉
# 就会把测试自己建的「小真」删掉，导致一堆用例找不到 AI。测试在 conftest 里关。
PURGE_LEGACY_DEFAULT_AI = os.environ.get("DSH_PURGE_LEGACY", "1") == "1"

# ---------------------------------------------------------------- 群聊抢麦

GROUP_WILLINGNESS_THRESHOLD = 0.6  # 意愿分阈值，无人超过则静默
GROUP_COOLDOWN_PENALTY = 0.35  # 最近发言过的 AI 扣减的意愿分

GROUP_MAX_SPEAKERS_PER_TURN = 3
# 下一个人插话前等多久（在「上一条 AI 消息真正发出去」之后再加这个间隔）。
GROUP_ROUND_MIN_DELAY_SECONDS = 2
GROUP_ROUND_MAX_DELAY_SECONDS = 7

# ---------------------------------------------------------------- 账号（用户 2026-09-27 第六轮）

ACCOUNT_ID_LENGTH = 4
DEV_ACCOUNT_ID = "0000"


def _load_dev_account() -> "tuple[str, str]":
    """开发者账号的昵称与密码，存放在 data/dev_account.json。

    凭据不写进代码库。文件不存在或读不出来时返回空字符串，
    此时自动创建与同步都会跳过，已存在的账号保持原状。
    """
    path = DATA_DIR / "dev_account.json"
    if not path.exists():
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    return str(data.get("name", "")), str(data.get("password", ""))


DEV_ACCOUNT_NAME, DEV_ACCOUNT_PASSWORD = _load_dev_account()
FIRST_REAL_ACCOUNT_ID = 1        # 第一个真人账号 = 0001
LAST_REAL_ACCOUNT_ID = 9999      # 最后一个真人账号；再来人就是「服务器已满」

PASSWORD_MIN_LENGTH = 6
PASSWORD_MAX_LENGTH = 32

LOGIN_RETRY_COOLDOWN_SECONDS = 3

TOKEN_PERMANENT = True

ACCOUNTS_DIR = Path(os.environ.get("DSH_ACCOUNTS_DIR") or (DATA_DIR / "accounts"))

AUTH_REQUIRED = os.environ.get("DSH_AUTH_REQUIRED", "1") == "1"

# 启动时是否允许「DB 里没有、但 accounts/<ID>/ 文件夹在」的账号从文件夹回灌。
ACCOUNTS_RESTORE_ON_START = os.environ.get("DSH_ACCOUNTS_RESTORE", "1") == "1"

# ---------------------------------------------------------------- 日志

LOG_LEVEL = "INFO"
