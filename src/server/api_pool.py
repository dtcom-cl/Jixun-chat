"""API 池（阶段2任务2.9 调用主模型；阶段4任务4.6 / 4.7 多 API 与故障切换）。

职责：
- 管理多个 API 配置（api_pool 表），为不同行为分配不同模型：
  对话用主模型（role=chat）、记忆提取用轻量模型（role=memory）、
  回复判断用快速模型（role=judge）、备用模型（role=backup）。
- 主模型故障时自动切换同 role 的其它配置，再退到 backup。

- deepseek、openai、glm（智谱）、custom（自定义 base_url）
- mock：仅测试使用，不发网络请求，返回预先排队的响应

api_key 存储： 要求“至少 Base64 编码”，本模块负责编码与解码。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

import database as db

logger = logging.getLogger("api_pool")

# provider 默认接入点与默认模型（用户不填时使用）
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    # 2026-09-29 第十三轮：默认模型从 `deepseek-chat` 改成 `deepseek-flash`。
    # 用真 Key 打 GET /models 看到的实际可用模型是：
    #   deepseek-flash   name="DeepSeek-V4.1-Flash"  input_modalities=["text","image"]
    #   deepseek-v4-pro                              input_modalities=["text"]
    # `deepseek-chat` 已经不在清单里（老名字）。而 deepseek-flash **收图片**，
    # 正好也是用户账号里在用的那个，把它当默认既跟得上服务端、又能看图。
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model_name": "deepseek-flash"},
    "openai": {"base_url": "https://api.openai.com/v1", "model_name": "gpt-4o-mini"},
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model_name": "glm-4-flash",
    },
    "mock": {"base_url": "", "model_name": "mock"},
}

REQUEST_TIMEOUT_SECONDS = 60.0
MAX_SWITCH_ATTEMPTS = 6  # 单次调用最多尝试的配置数（含备用）

# ---------------------------------------------------------------- mock（仅测试）

class _MockQueue:
    """测试用的排队响应。生产环境不会被填充，因此永远走真实 HTTP。"""

    def __init__(self) -> None:
        self.responses: list[str] = []

    def push(self, text: str) -> None:
        self.responses.append(text)

    def push_many(self, texts: list[str]) -> None:
        self.responses.extend(texts)

    def pop(self) -> Optional[str]:
        if not self.responses:
            return None
        return self.responses.pop(0)

    def clear(self) -> None:
        self.responses.clear()

mock_queue = _MockQueue()

def is_mock_provider(provider: str) -> bool:
    return (provider or "").lower() == "mock"

def mock_enabled() -> bool:
    """环境变量 DSH_LLM_MOCK=1 时，任何真实调用都改走 mock（自动化测试用）。"""
    return os.environ.get("DSH_LLM_MOCK", "") == "1"

# ---------------------------------------------------------------- Key 编解码

def normalize_api_key(raw: str) -> str:
    """清掉用户复制粘贴时带进来的杂质。

    实测用户会粘成这些样子：前后带引号、带 `Bearer ` 前缀、
    从网页复制时尾巴上挂一个换行。这些都必须在入库前清掉，
    否则 Key 看着对、请求必然 401，排查起来很费劲。
    """
    key = (raw or "").strip().strip('"').strip("'").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key

# DeepSeek / OpenAI 系：sk- 开头 + 至少 16 位（实际是 32 位十六进制）
_SK_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{16,}$")
# 智谱 GLM：<32 位十六进制>.<secret>
_GLM_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}\.[A-Za-z0-9_\-]+$")

def detect_provider(raw_key: str) -> str:
    """按 Key 的格式猜 provider，猜不出返回空串（调用方回落到 custom）。

    用户 2026-09-27 要求同时支持：
    - `sk-<你的 Key>`（DeepSeek / OpenAI 系）
    - `<32位id>.<secret>`（智谱 GLM）
    两种格式靠前缀和 `.` 就能区分，不需要用户自己选厂家。
    """
    key = normalize_api_key(raw_key)
    if _SK_KEY_RE.match(key):
        return "deepseek"
    if _GLM_KEY_RE.match(key):
        return "glm"
    return ""

def encode_api_key(raw: str) -> str:
    return base64.b64encode(normalize_api_key(raw).encode("utf-8")).decode("ascii")

def decode_api_key(stored: str) -> str:
    try:
        return base64.b64decode((stored or "").encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001 - 兼容明文写入的历史数据
        return stored or ""

# ---------------------------------------------------------------- 智谱 JWT

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def glm_jwt(api_key: str, exp_seconds: int = 3600) -> str:
    """把智谱的 `id.secret` 现场签成一个 HS256 JWT。

    为什么必须签：智谱的 API Key 是两段式，直接用整串当 Bearer 只有部分接口认，
    官方 SDK 的真实行为是——`id` 放 payload 的 api_key 字段，`secret` 当 HMAC 密钥，
    签出 JWT 再放 `Authorization: Bearer`。不签就会 401，
    然后被当成“API 故障”切换下一条，用户看到的是 AI 不回话但不知道为什么。

    这里用标准库 hmac + hashlib + base64 手写，不引入 PyJWT：
    项目连 ORM 都换成了 stdlib sqlite3，为 15 行签名加一个依赖不划算。
    """
    key = normalize_api_key(api_key)
    if "." not in key:
        raise LLMCallError("智谱 API Key 需要 `id.secret` 两段格式")
    key_id, secret = key.split(".", 1)
    now_ms = int(time.time() * 1000)
    header = {"alg": "HS256", "sign_type": "SIGN"}
    payload = {
        "api_key": key_id,
        "exp": now_ms + exp_seconds * 1000,
        "timestamp": now_ms,
    }
    seg = "{}.{}".format(
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    )
    sig = hmac.new(secret.encode("utf-8"), seg.encode("ascii"), hashlib.sha256).digest()
    return "{}.{}".format(seg, _b64url(sig))

def build_auth_header(cfg: "ApiConfig") -> str:
    """按 provider 生成 Authorization 头的值（含 Bearer 前缀）。

    - glm 且是两段式 Key：现场签 JWT（这是官方 SDK 的真实行为）
    - 其余（deepseek / openai / custom）：Key 原样当 Bearer
    """
    key = normalize_api_key(cfg.api_key)
    if cfg.provider == "glm" and _GLM_KEY_RE.match(key):
        return "Bearer " + glm_jwt(key)
    return "Bearer " + key

# ---------------------------------------------------------------- 配置读写

@dataclass
class ApiConfig:
    id: int
    provider: str
    model_name: str
    api_key: str
    base_url: str
    role: str
    priority: int
    is_primary: int
    is_active: int

    @property
    def display(self) -> str:
        return f"{self.provider}/{self.model_name}(role={self.role},id={self.id})"

def _row_to_config(row) -> ApiConfig:
    provider = (row["provider"] or "custom").lower()
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    return ApiConfig(
        id=int(row["id"]),
        provider=provider,
        model_name=row["model_name"] or defaults.get("model_name", ""),
        api_key=decode_api_key(row["api_key"]),
        base_url=(row["base_url"] or defaults.get("base_url", "")).rstrip("/"),
        role=row["role"] or "chat",
        priority=int(row["priority"] or 0),
        is_primary=int(row["is_primary"] or 0),
        is_active=int(row["is_active"] if row["is_active"] is not None else 1),
    )

def save_api_config(
    *,
    user_id: int,
    provider: str,
    api_key: str,
    model_name: str = "",
    base_url: str = "",
    role: str = "chat",
    priority: int = 0,
    is_primary: int = 0,
    api_id: Optional[int] = None,
) -> int:
    """新增或更新一条 API 配置。用户填一次即保存，第二次不用重填。

    文档未写“同 provider+role 只保留一条”，为避免重复配置导致切换混乱，
    这里按 (user_id, provider, role) 做覆盖更新（决策记录，见模块 docstring）。
    """
    api_key = normalize_api_key(api_key)
    # 用户不选厂家时按 Key 格式自动识别（用户 2026-09-27 要求两种格式都要支持）。
    # 识别不出才回落到 custom，让用户自己填 base_url。
    provider = (provider or "").lower().strip()
    auto_detected = not provider or provider == "auto"
    if auto_detected:
        provider = detect_provider(api_key) or "custom"
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    resolved_model = model_name or defaults.get("model_name", "")
    resolved_base = (base_url or defaults.get("base_url", "")).rstrip("/")
    # 只在“我们替他猜厂家、又猜不出地址”时拦下来，因为这必然发不出请求。
    # 用户显式写了 provider（含 custom）就按他的意思存，不再多嘴。
    if not resolved_base and auto_detected:
        raise ValueError(
            "无法从 Key 判断接口地址，请手动填写 base_url（例如 https://api.deepseek.com/v1）"
        )
    encoded = encode_api_key(api_key)

    if api_id is None:
        existing = [
            _row_to_config(r)
            for r in db.list_api_configs(user_id=user_id, role=role)
            if (r["provider"] or "").lower() == provider
        ]
        if existing:
            api_id = existing[0].id

    if api_id is not None:
        db.update_api_config(
            api_id,
            provider=provider,
            model_name=resolved_model,
            api_key=encoded,
            base_url=resolved_base,
            priority=priority,
            is_active=1,
            is_primary=is_primary,
            role=role,
        )
        return int(api_id)

    return db.add_api_config(
        user_id=user_id,
        provider=provider,
        model_name=resolved_model,
        api_key=encoded,
        base_url=resolved_base,
        priority=priority,
        is_primary=is_primary,
        role=role,
    )

def list_configs_public(user_id: int) -> list[dict]:
    """给客户端的配置列表：不返回明文 Key，只返回是否存在与后四位。"""
    out = []
    for row in db.list_api_configs(user_id=user_id):
        cfg = _row_to_config(row)
        key = cfg.api_key
        out.append(
            {
                "id": cfg.id,
                "provider": cfg.provider,
                "model_name": cfg.model_name,
                "base_url": cfg.base_url,
                "role": cfg.role,
                "priority": cfg.priority,
                "is_primary": cfg.is_primary,
                "is_active": cfg.is_active,
                "key_tail": key[-4:] if key else "",
                "has_key": bool(key),
            }
        )
    return out

def config_from_ai_profile(row, *, role: str = "chat") -> Optional[ApiConfig]:
    """把一个 ai_profiles 行里配置的 API 变成一个 ApiConfig；没配返回 None。

    用户 2026-09-27 要求「每一个不同的聊天点进去后可以在右上角设置，
    这个设置里面只能设置对方的头像、名称、API接口、APIKEY、人格对话数据」。
    这几列是当时新增的（见 database.SCHEMA_MIGRATIONS）。

    优先级：**会话级 > 全局池**。

    :param role: 这次调用扮演的角色（chat / memory / judge）。
        2026-09-28 第九轮修正：原来这里硬写 role="chat"，而 chat() 又只在
        role=="chat" 时才认会话级配置，于是人格蒸馏（memory）、长期记忆提取
        （memory）、主动开口评估（judge）永远拿不到用户的 Key，全部报错。
        现在 role 跟着调用方走。
    """
    if row is None:
        return None
    keys = row.keys()
    if "api_key" not in keys:
        return None
    raw_key = row["api_key"] or ""
    if not raw_key:
        return None
    provider = (row["api_provider"] or "").lower() if "api_provider" in keys else ""
    key = normalize_api_key(decode_api_key(raw_key))
    if not provider or provider == "auto":
        provider = detect_provider(key) or "custom"
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    base_url = ((row["api_base_url"] if "api_base_url" in keys else "") or "").rstrip("/")
    model_name = (row["api_model"] if "api_model" in keys else "") or ""
    return ApiConfig(
        id=0,  # 0 = 会话级配置，不写回 api_pool 表，因此不参与 mark_api_used/failure
        provider=provider,
        model_name=model_name or defaults.get("model_name", ""),
        api_key=key,
        base_url=base_url or defaults.get("base_url", ""),
        role=role,
        priority=0,
        is_primary=1,
        is_active=1,
    )

def first_profile_config(user_id: int, *, role: str = "chat") -> Optional[ApiConfig]:
    """这个账号名下第一个配了 Key 的会话级配置（兜底用）。

    2026-09-28 第九轮新增。为什么需要它：记忆提取、摘要压缩、主动开口评估
    这几条链路拿到的只有 `user_id`，没有具体的 `ai_profile_id`（摘要按会话存，
    不关心是哪个 AI）。而用户在客户端**唯一**的 Key 入口是会话设置页，
    全局 api_pool 表没有 UI 入口、永远是空的。没有这个兜底，这些链路就必然
    报「还没有可用的 API 配置」。

    取第一个配了 Key 的档案即可：同一个账号下用户填的通常就是同一份凭据。
    """
    for row in db.list_ai_profiles(user_id):
        cfg = config_from_ai_profile(row, role=role)
        if cfg is not None:
            return cfg
    return None

def _candidates(role: str, user_id: int) -> list[ApiConfig]:
    """按 role 选配置：先同 role（is_primary 优先、priority 小优先），再 backup。"""
    rows = db.list_api_configs(user_id=user_id)
    cfgs = [_row_to_config(r) for r in rows if int(r["is_active"] or 0) == 1]

    def sort_key(c: ApiConfig):
        return (0 if c.role == role else 1, 0 if c.is_primary else 1, c.priority, c.id)

    same = [c for c in cfgs if c.role == role]
    backups = [c for c in cfgs if c.role == "backup" and c.role != role]
    others = [c for c in cfgs if c not in same and c not in backups]
    ordered = sorted(same + backups + others, key=sort_key)
    return ordered

# ---------------------------------------------------------------- 调用

class LLMCallError(RuntimeError):
    pass

def _mock_response(role: str) -> str:
    """无可用 API 配置时的兜底返回。

    注意：不同 role 的调用方解析的结构不同——
    - role="judge" 的反思循环要 ReflectionResult（willingness/reason/topic）
    - 群聊抢麦要 GroupBid（willingness/reason）
    - 其余（chat/memory/backup）要 AI-001 的 AIResponse
    早期版本对所有 role 一律返回 AIResponse，导致反思循环每轮都抛
    ValidationError（willingness 缺失）。这里按 role 分开返回合法结构。
    """
    text = mock_queue.pop()
    if text is not None:
        return text

    if role == "judge":
        # 兜底意愿分 0.0：等同“不想说话”，不会误触发主动发起
        return json.dumps(
            {"willingness": 0.0, "reason": "mock（无 API 配置）", "topic": ""},
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "action": "reply",
            "delay_seconds": 0,
            "message_count": 1,
            "tone": "casual",
            "content": ["嗯"],
            "follow_up": None,
            "state_update": {
                "emotion_delta": 0.0,
                "energy_delta": 0.0,
                "intimacy_delta": 0.0,
            },
        },
        ensure_ascii=False,
    )

async def _call_once(cfg: ApiConfig, messages: list[dict], temperature: float) -> str:
    """按 OpenAI 兼容协议调用一次 /chat/completions。"""
    if is_mock_provider(cfg.provider) or mock_enabled():
        return _mock_response(cfg.role)

    url = f"{cfg.base_url}/chat/completions"
    # 智谱是两段式 Key，必须现场签 JWT；其余 provider 直接当 Bearer。
    # 见 build_auth_header / glm_jwt 的说明。
    headers = {
        "Authorization": build_auth_header(cfg),
        "Content-Type": "application/json",
    }
    body = {
        "model": cfg.model_name,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise LLMCallError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMCallError(f"返回结构异常：{str(data)[:300]}") from exc

def _resolve_candidates(
    role: str, user_id: int, ai_profile_id: Optional[int] = None
) -> list[ApiConfig]:
    """这次调用会按什么顺序试哪些配置。

    2026-09-28 第九轮修正（用户实测三个 bug 的**共同根因**）：
    这里原来写的是 `role == "chat"` 才认会话级配置，理由是 PRD 3.2.8 想让
    memory / judge 共用全局池来省费用。但实际的配置入口只有一个 ——
    会话设置页（用户明确说「只能设置对方的头像、名称、API接口、APIKEY、
    人格对话数据」），全局 api_pool 表在客户端**根本没有入口**，永远是空表。

    于是：chat 有 key 能聊天，而 role="memory" 的人格蒸馏、长期记忆提取，
    以及 role="judge" 的主动开口评估，候选列表恒为 0 -> 一律抛 LLMCallError。
    用户看到的就是「人格蒸馏必失败，退回统计式蒸馏」+
    「一直报错长期记忆提取失败」+
    「一直说还没有可用的 API 配置」（主动开口/反思时）。

    现在所有 role 都认会话级配置：用户在会话设置里填的那一份就是他唯一的
    凭据，拿它去做记忆提取和意愿评估才是符合直觉的行为。
    """
    candidates = _candidates(role, user_id)
    if ai_profile_id is not None:
        own = config_from_ai_profile(
            db.get_ai_profile(int(ai_profile_id)), role=role
        )
        if own is not None:
            candidates = [own] + candidates
    elif role != "chat":
        # 没有具体档案（摘要 / 记忆 / 反思这类按会话或按账号跑的链路）：
        # 拿账号下第一个配了 Key 的档案兜底，否则这些链路永远报“没配 API”。
        fallback = first_profile_config(user_id, role=role)
        if fallback is not None:
            candidates = [fallback] + candidates
    return candidates

def first_config(
    *,
    role: str = "chat",
    user_id: int = 1,
    ai_profile_id: Optional[int] = None,
) -> Optional[ApiConfig]:
    """这次调用**实际会用**哪一份配置（第一个候选）；一份都没有时返回 None。

    2026-09-29 第十三轮新增。dispatcher 需要知道实际会用的**模型名**，
    才能判断它收不收图片（vision.supports_vision）—— 判错了要么白扔一张图
    让接口 400，要么明明能看图却告诉用户「我看不了」。

    只看配置，不发请求，所以很便宜。
    """
    got = _resolve_candidates(role, user_id, ai_profile_id)
    return got[0] if got else None

# ------------------------------------------------- 模型能力探测（看图 / 第十三轮）
#
# 用户 2026-09-29 原话：「他妈的这不是支持视觉吗 DeepSeek有视觉 这个就是视觉模型」。
# 用户是对的 —— 我原来拿**模型名**猜视觉能力，名单里漏了 `deepseek-flash`，
# 于是服务端告诉模型「你看不到图」，模型明明看得见也只好说「我打不开」。
#
# 按名字猜能力本身就是错的做法。OpenAI 兼容的 /models 接口会返回能力清单：
#
#   GET https://api.deepseek.com/v1/models  ->
#     {"id": "deepseek-flash", "input_modalities": ["text", "image"], ...}
#     {"id": "deepseek-v4-pro", "input_modalities": ["text"], ...}
#
# 所以以**服务端说的为准**，名字只当探测不可用时的退路。

# 缓存：{(base_url, model_name): bool}。探测一次就够，不必每轮聊天都问。
_MODEL_VISION_CACHE: dict[tuple[str, str], bool] = {}
# 探测失败的模型也别反复问（比如服务端不支持 /models，或断网）。
_MODEL_VISION_FAILED: set[tuple[str, str]] = set()

MODEL_PROBE_TIMEOUT_SECONDS = 8.0

def _capabilities_url(cfg: "ApiConfig") -> str:
    """拼出 /models 的地址。base 可能带或不带 /v1，两种都要能对上。"""
    base = (cfg.base_url or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/models"

def parse_model_capabilities(data: Any) -> dict[str, dict]:
    """把 /models 的返回整理成 {model_id: {...}}。拿不到就返回空字典。

    纯函数，方便单测（不用联网）。兼容两种形状：
    OpenAI 风格 `{"data": [{"id": ...}]}`，以及直接给数组。
    """
    items = data
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    if not isinstance(items, list):
        return {}
    out: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("model") or "").strip()
        if model_id:
            out[model_id] = item
    return out

def vision_from_capabilities(caps: dict, model_name: str) -> Optional[bool]:
    """从能力清单里读出「这个模型收不收图片」。读不出来返回 None（=不知道）。

    看 `input_modalities` / `modalities` 里有没有 "image"；
    也有接口用 `supports_vision: true` 这种布尔字段，一并认。
    """
    item = caps.get(model_name)
    if item is None:
        return None
    for key in ("input_modalities", "modalities"):
        value = item.get(key)
        if isinstance(value, list):
            lowered = {str(v).lower() for v in value}
            return "image" in lowered
    if isinstance(item.get("supports_vision"), bool):
        return bool(item["supports_vision"])
    if isinstance(item.get("vision"), bool):
        return bool(item["vision"])
    return None

async def fetch_model_capabilities(cfg: "ApiConfig") -> dict[str, dict]:
    """问服务端有哪些模型、各自收什么输入。任何失败都返回空字典。

    这是**加分项**：探测失败必须退回到「按名字猜」，
    不能因为问不到就让用户干脆看不了图。
    """
    url = _capabilities_url(cfg)
    if not url:
        return {}
    try:
        headers = {
            "Authorization": build_auth_header(cfg),
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=MODEL_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            logger.info("模型能力探测失败 HTTP %s（%s）", resp.status_code, url)
            return {}
        return parse_model_capabilities(resp.json())
    except Exception as exc:  # noqa: BLE001 - 探测失败不算错，退回名字猜测
        logger.info("模型能力探测异常（%s）：%s", url, exc)
        return {}

async def config_supports_vision(cfg: Optional["ApiConfig"]) -> bool:
    """这次用的配置收不收图片。**优先问服务端，问不到才按名字猜。**

    为什么要异步：真答案在网络对面。所以 dispatcher 在拼 Prompt 之前 await 一下。
    结果带缓存，正常聊天只会多一次很便宜的 GET（每个进程一次）。
    """
    if cfg is None:
        return False
    import vision as vision_mod

    model_name = (cfg.model_name or "").strip()
    if not model_name:
        return False
    cache_key = ((cfg.base_url or "").rstrip("/"), model_name)
    cached = _MODEL_VISION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    if cache_key in _MODEL_VISION_FAILED:
        return vision_mod.supports_vision(model_name)

    caps = await fetch_model_capabilities(cfg)
    if not caps:
        _MODEL_VISION_FAILED.add(cache_key)
        return vision_mod.supports_vision(model_name)

    known = vision_from_capabilities(caps, model_name)
    if known is None:
        # 服务端答了，但没说这个模型收什么 —— 名字猜测仍是更好的猜测。
        _MODEL_VISION_FAILED.add(cache_key)
        return vision_mod.supports_vision(model_name)

    _MODEL_VISION_CACHE[cache_key] = known
    logger.info(
        "模型能力（来自服务端 /models）：%s 收图片=%s", model_name, known
    )
    return known

async def chat(
    messages: list[dict],
    *,
    role: str = "chat",
    user_id: int = 1,
    temperature: float = 0.9,
    ai_profile_id: Optional[int] = None,
) -> str:
    """调用模型并返回文本。主模型失败自动切备用。

    以前没有任何 API 配置时会静默返回 _mock_response（chat 角色 = 一个「嗯」），
    用户只看到 AI 永远回「嗯」，根本不知道是自己没配 Key —— 这是“假 AI”观感的来源。
    现在改为**直接抛 LLMCallError**，由上层明确告诉用户“这个会话还没配 API”。
    唯一的例外是测试环境（DSH_LLM_MOCK=1）与 provider=mock 的配置，
    它们本来就是要走 mock 的，不算生产路径。
    """
    candidates = _candidates(role, user_id)
    # 会话级 API 优先（规则见 _resolve_candidates 的说明）。
    candidates = _resolve_candidates(role, user_id, ai_profile_id)

    if not candidates:
        # 测试环境（DSH_LLM_MOCK=1）本来就要走 mock，不能按“没配置”报错；
        # 只有真实生产环境才抛错，让用户看到明确的“请填 API”。
        if mock_enabled():
            return _mock_response(role)
        raise LLMCallError(
            "还没有可用的 API 配置：请在会话设置里填 API 接口地址与 API Key"
        )

    last_error: Optional[str] = None
    for cfg in candidates[:MAX_SWITCH_ATTEMPTS]:
        try:
            text = await _call_once(cfg, messages, temperature)
            if cfg.id:
                db.mark_api_used(cfg.id)
            return text
        except Exception as exc:  # noqa: BLE001 - 任何失败都要能切下一个
            last_error = str(exc)
            if cfg.id:
                count = db.mark_api_failure(cfg.id, last_error)
                logger.warning("API 调用失败（%s，连续失败 %s 次）：%s", cfg.display, count, last_error)
            else:
                logger.warning("会话级 API 调用失败（%s）：%s", cfg.display, last_error)
            continue
    raise LLMCallError(f"所有 API 配置均失败，最后一次错误：{last_error}")