"""账号模块（用户 2026-09-27 第六轮要求）。

    「电脑服务端增加账号功能，给每个填写完名称的用户分配一个账号，我作为开发者，
      我的昵称叫乐，分配的ID为0000，只会给真人分配ID，人机不要分配，昵称可以重复
      ID不能重复，下一个人的ID就是0001，最多到9999，再多就会注册失败，显示服务器已满」
    「填写的API key、各种接口、各种自定义都会上传到电脑服务器，不会存储在本地」
    「为什么要放在服务端账号，就是为了换手机删除软件之后重新下回来登陆上还是之前的配置」
    「在项目里面专门弄一个文件夹放各种账户的各种数据，全部明文存储」

所以这个模块负责三件事：

1. **账号**：发号（0000 开发者保留，0001~9999 给真人）、注册、登录、改密码。
   这是用户对自己局域网自建服务的明确选择，已记录在验收文档的风险说明里。

   同一 ID 禁止多设备同时登录 —— 新登录生成新令牌覆盖旧的，
   旧设备下一次请求就会拿到 401 并被踢回登录页。

3. **明文数据文件夹**：`data/accounts/<4位ID>/`，实时镜像该账号的全部数据，
   目录结构见下面 `_mirror_*` 系列函数。用户要的是「随手能打开看、能拷走」。

关于「谁说了算」：用户确认 **app.db 是运行时权威，文件夹是实时镜像**。
理由：单一写入者不会出现两边不一致；同时启动时支持从文件夹回灌，
数据库万一被删也能把账号找回来（`restore_from_folders`）。

按**账号 ID** 记（用户选定），不按 IP —— 免得同一台设备上的人互相连累。
令牌本身用 HMAC-SHA256 自签，密钥落在 `data/.secret_key`，标准库实现，不加依赖。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import database as db
from config import (
    ACCOUNT_ID_LENGTH,
    ACCOUNTS_DIR,
    DEV_ACCOUNT_ID,
    DEV_ACCOUNT_NAME,
    DEV_ACCOUNT_PASSWORD,
    FIRST_REAL_ACCOUNT_ID,
    LAST_REAL_ACCOUNT_ID,
    LOGIN_RETRY_COOLDOWN_SECONDS,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
)

logger = logging.getLogger("accounts")


# ================================================================
# 一、校验规则
# ================================================================


class AccountError(Exception):
    """账号相关的业务错误。上层把它翻成 HTTP 400/401/403/409。"""

    def __init__(self, message: str, *, code: str = "account_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class ServerFullError(AccountError):
    """9999 个真人号发完了。用户要求显示「服务器已满」。"""

    def __init__(self) -> None:
        super().__init__(
            f"服务器已满（最多 {LAST_REAL_ACCOUNT_ID} 个账号，已全部注册）",
            code="server_full",
        )


class LoginCooldownError(AccountError):
    """上一次密码输错还不满 3 秒。带剩余秒数，客户端可以倒计时。"""

    def __init__(self, retry_after: float) -> None:
        super().__init__(
            f"密码错误，请 {int(retry_after) + 1} 秒后再试",
            code="login_cooldown",
        )
        self.retry_after = max(0.0, float(retry_after))


_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT = re.compile(r"[0-9]")


def validate_nickname(raw: str) -> str:
    """昵称：非空、去掉首尾空白、最长 32 字符（沿用上一轮定下的 32）。

    用户明确「昵称可以重复」，所以这里**不做唯一性检查**。
    """
    name = (raw or "").strip()
    if not name:
        raise AccountError("昵称不能为空", code="bad_nickname")
    if len(name) > 32:
        raise AccountError("昵称最长 32 个字符", code="bad_nickname")
    return name


def validate_password(raw: str) -> str:
    """密码规则（用户原话）：

    「密码中至少含一个数字和一个字母，总字符不少于6位数，最长不超过32位数」

    注意「字符」不是「字节」——用 len() 数的是字符，中文密码也算。
    """
    pwd = raw or ""
    if len(pwd) < PASSWORD_MIN_LENGTH:
        raise AccountError(
            f"密码至少 {PASSWORD_MIN_LENGTH} 位", code="bad_password"
        )
    if len(pwd) > PASSWORD_MAX_LENGTH:
        raise AccountError(
            f"密码最长 {PASSWORD_MAX_LENGTH} 位", code="bad_password"
        )
    if not _HAS_LETTER.search(pwd):
        raise AccountError("密码至少要有一个字母", code="bad_password")
    if not _HAS_DIGIT.search(pwd):
        raise AccountError("密码至少要有一个数字", code="bad_password")
    return pwd


def validate_account_id(raw: str) -> str:
    """账号 ID：4 位数字字符串。登录时用户可能少打前导零，这里补齐。"""
    text = (raw or "").strip()
    if not text.isdigit():
        raise AccountError("账号只能是数字", code="bad_account_id")
    if len(text) > ACCOUNT_ID_LENGTH:
        raise AccountError(
            f"账号最多 {ACCOUNT_ID_LENGTH} 位", code="bad_account_id"
        )
    return text.zfill(ACCOUNT_ID_LENGTH)


# ================================================================
# 二、登录失败冷却（3 秒）
# ================================================================

# 只放在进程内存里：重启服务端就清空。
# 用户要防的是「脚本狂刷把服务器挤爆」，不是持久化的封禁策略；
# 重启即清空对自用服务端更友好（忘了密码也不用等）。
_login_failures: dict[str, float] = {}


def _cooldown_remaining(account_id: str) -> float:
    last = _login_failures.get(account_id)
    if last is None:
        return 0.0
    elapsed = time.monotonic() - last
    remaining = LOGIN_RETRY_COOLDOWN_SECONDS - elapsed
    return remaining if remaining > 0 else 0.0


def assert_can_attempt_login(account_id: str) -> None:
    """距上次输错不到 3 秒就拒绝，避免有人拿脚本狂试。"""
    remaining = _cooldown_remaining(account_id)
    if remaining > 0:
        raise LoginCooldownError(remaining)


def record_login_failure(account_id: str) -> None:
    _login_failures[account_id] = time.monotonic()


def clear_login_failures(account_id: Optional[str] = None) -> None:
    """登录成功或测试用。不传参数就全清。"""
    if account_id is None:
        _login_failures.clear()
    else:
        _login_failures.pop(account_id, None)


# ================================================================
# 三、令牌（永久有效 + 单设备）
# ================================================================

_SECRET_PATH_NAME = ".secret_key"
_secret_cache: Optional[bytes] = None


def _secret_key() -> bytes:
    """服务端签名密钥。首次运行生成并落盘，之后固定不变。

    为什么不用随机密钥：令牌是永久的，重启服务端不能把所有用户踢下线。
    """
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    path = Path(ACCOUNTS_DIR).parent / _SECRET_PATH_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _secret_cache = path.read_bytes().strip()
    else:
        key = base64.urlsafe_b64encode(secrets.token_bytes(32))
        path.write_bytes(key)
        _secret_cache = key
    return _secret_cache


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_token(account_id: str) -> str:
    """签一个永久令牌：`<payload>.<签名>`。payload 里带账号 ID 和随机串。

    随机串的作用：同一个账号每次登录拿到的令牌都不一样，
    这样「新登录覆盖旧令牌」才真的能把老设备踢下线
    （否则两次登录拿到同一个字符串，覆盖了也没区别）。
    """
    payload = json.dumps(
        {"id": account_id, "nonce": secrets.token_hex(8), "iat": int(time.time())},
        separators=(",", ":"),
    ).encode("utf-8")
    seg = _b64url(payload)
    sig = hmac.new(_secret_key(), seg.encode("ascii"), hashlib.sha256).digest()
    return f"{seg}.{_b64url(sig)}"


def token_account_id(token: str) -> Optional[str]:
    """校验签名并取出账号 ID。签名不对 / 格式不对返回 None。

    注意这里**只验签名**，不验「是不是当前有效令牌」——
    后者要查库（users.token），由 `resolve_token()` 做。
    """
    if not token or "." not in token:
        return None
    seg, _, sig = token.partition(".")
    try:
        expected = hmac.new(
            _secret_key(), seg.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig)):
            return None
        payload = json.loads(_b64url_decode(seg).decode("utf-8"))
    except Exception:  # noqa: BLE001 - 任何异常都当令牌无效
        return None
    account_id = str(payload.get("id") or "")
    return account_id or None


def resolve_token(token: str) -> Optional[Any]:
    """令牌 -> users 行。三重校验：签名、账号还在、**是当前有效令牌**。

    第三条就是「同 ID 禁止多设备」的落点：登录时把新令牌写进 users.token，
    旧设备的令牌对不上，于是立刻失效。
    """
    account_id = token_account_id(token)
    if account_id is None:
        return None
    row = db.get_user_by_account(account_id)
    if row is None:
        return None
    current = row["token"] if "token" in row.keys() else None
    if not current or not hmac.compare_digest(str(current), str(token)):
        return None
    return row


# ================================================================
# 四、注册 / 登录 / 改密码
# ================================================================


def ensure_developer_account() -> str:
    """保证开发者账号 0000 存在（幂等）。服务端每次启动调用。

    这个账号**不参与发号**，也不会被注册流程占用。
    """
    existing = db.get_user_by_account(DEV_ACCOUNT_ID)
    if not DEV_ACCOUNT_PASSWORD:
        # 未提供凭据（data/dev_account.json 缺失）时不创建也不改写，
        # 避免把密码写进代码库，也避免覆盖已存在的账号。
        return DEV_ACCOUNT_ID if existing is not None else ""
    if existing is not None:
        # 昵称/密码以代码里的常量为准（用户改过 config 就跟着变），
        # 但不动令牌，免得每次重启都把开发者踢下线。
        if existing["display_name"] != DEV_ACCOUNT_NAME or existing["password_hash"] != DEV_ACCOUNT_PASSWORD:
            db.update_user_account(
                DEV_ACCOUNT_ID,
                display_name=DEV_ACCOUNT_NAME,
                password=DEV_ACCOUNT_PASSWORD,
            )
        return DEV_ACCOUNT_ID

    user_id = db.create_user_row(
        account_id=DEV_ACCOUNT_ID,
        nickname=DEV_ACCOUNT_NAME,
        password=DEV_ACCOUNT_PASSWORD,
    )
    logger.info("已创建开发者账号 %s（%s），users.id=%s", DEV_ACCOUNT_ID, DEV_ACCOUNT_NAME, user_id)
    mirror_account(DEV_ACCOUNT_ID)
    return DEV_ACCOUNT_ID


def register(*, nickname: str, password: str, avatar_path: str = "") -> dict:
    """注册一个新账号，返回 {account_id, user_id, nickname, token}。

    流程：校验昵称与密码 -> 发号 -> 建 users 行 -> 签令牌 -> 镜像文件夹。
    昵称可重复（用户明确），所以这里没有重名检查。
    真人号发完（超过 9999）抛 ServerFullError，上层翻成「服务器已满」。
    """
    name = validate_nickname(nickname)
    pwd = validate_password(password)

    account_id = _allocate_account_id()
    user_id = db.create_user_row(
        account_id=account_id, nickname=name, password=pwd, avatar_url=avatar_path
    )
    token = issue_token(account_id)
    db.set_user_token(account_id, token)
    mirror_account(account_id)
    logger.info("新账号注册成功：%s（%s），users.id=%s", account_id, name, user_id)
    return {
        "account_id": account_id,
        "user_id": user_id,
        "nickname": name,
        "token": token,
    }


def _allocate_account_id() -> str:
    """发号：当前最大真人号 +1，从 0001 开始，到 9999 为止。

    在数据库事务里做（`db.allocate_next_account_id`），防止两个人同时注册
    抢到同一个号。0000 不参与，所以扫描范围是 [1, 9999]。
    """
    number = db.allocate_next_account_id(FIRST_REAL_ACCOUNT_ID, LAST_REAL_ACCOUNT_ID)
    if number is None:
        raise ServerFullError()
    return str(number).zfill(ACCOUNT_ID_LENGTH)


def login(*, account_id: str, password: str) -> dict:
    """登录：ID + 密码 -> 令牌。

    三重关卡：
    1. 3 秒冷却（上次输错还没满 3 秒直接拒绝，带剩余秒数）；
    2. 账号存在 + 密码正确；
    """
    normalized = validate_account_id(account_id)
    assert_can_attempt_login(normalized)

    row = db.get_user_by_account(normalized)
    # 账号不存在和密码错误回同一句话：不泄露「这个号存在」。
    if row is None or not hmac.compare_digest(
        str(row["password_hash"] or ""), str(password or "")
    ):
        record_login_failure(normalized)
        raise AccountError("账号或密码不对", code="bad_credentials")

    clear_login_failures(normalized)
    token = issue_token(normalized)
    db.set_user_token(normalized, token)
    mirror_account(normalized)
    logger.info("账号 %s（%s）登录成功，旧设备的令牌已失效", normalized, row["display_name"])
    return {
        "account_id": normalized,
        "user_id": int(row["id"]),
        "nickname": row["display_name"],
        "token": token,
    }


def logout(account_id: str) -> None:
    """退出登录：清掉服务端记的令牌，之后旧令牌立刻失效。"""
    db.set_user_token(account_id, "")


def change_password(*, account_id: str, old_password: str, new_password: str) -> None:
    """改密码：要先验旧密码（否则拿到令牌就能改掉别人的密码）。"""
    row = db.get_user_by_account(account_id)
    if row is None:
        raise AccountError("账号不存在", code="bad_account_id")
    if not hmac.compare_digest(
        str(row["password_hash"] or ""), str(old_password or "")
    ):
        record_login_failure(account_id)
        raise AccountError("原密码不对", code="bad_credentials")
    pwd = validate_password(new_password)
    db.update_user_account(account_id, password=pwd)
    clear_login_failures(account_id)
    mirror_account(account_id)
    logger.info("账号 %s 修改了密码", account_id)


# ================================================================
# 五、明文数据文件夹（data/accounts/<ID>/）
# ================================================================
#


def account_dir(account_id: str) -> Path:
    return Path(ACCOUNTS_DIR) / account_id


# ---------------------------------------------------------------- 写盘前的备份
OWNER_ACCOUNT_ID = "0000"
BACKUP_KEEP_ROUNDS = 2
CHAT_BACKUP_MAX_BYTES = 1024
CHAT_BACKUP_NAMES = frozenset({"chat.jsonl"})
CHAT_BACKUP_MIN_INTERVAL_SECONDS = 30 * 60


def _backup_dir(root: Path) -> Path:
    return root / "Bak"


def _tail_bytes_utf8(data: bytes, limit: int) -> bytes:
    """取最后 limit 个字节，并把开头可能被截断的 UTF-8 字节去掉。"""
    if len(data) <= limit:
        return data
    tail = data[-limit:]
    # 去掉开头那些「续接字节」（10xxxxxx），保证解码从字符边界开始
    i = 0
    while i < len(tail) and (tail[i] & 0xC0) == 0x80:
        i += 1
    return tail[i:]


def _backups_of(bak_dir: Path, rel_flat: str) -> list[Path]:
    """某个文件现有的备份（按名字排序 = 按时间从旧到新）。"""
    try:
        return sorted(
            (p for p in bak_dir.iterdir()
             if p.is_file() and p.name.startswith(rel_flat + ".") and p.name.endswith(".bak")),
            key=lambda p: p.name,
        )
    except OSError:
        return []


def _prune_backups(bak_dir: Path, rel_flat: str) -> None:
    """同一个文件的备份只留最近 BACKUP_KEEP_ROUNDS 轮，更早的删掉。

    文件名的形式是 `<rel_flat>.<时间戳>.bak`，时间戳是 `%Y%m%d-%H%M%S-%f`，
    所以按文件名排序就等于按时间排序 —— 名字大的更新。
    """
    mine = _backups_of(bak_dir, rel_flat)
    for old in (mine[:-BACKUP_KEEP_ROUNDS] if len(mine) > BACKUP_KEEP_ROUNDS else []):
        try:
            old.unlink()
        except OSError:
            logger.exception("删旧备份失败：%s", old)


def _chat_backup_too_soon(bak_dir: Path, rel_flat: str) -> bool:
    """聊天记录是否「离上一份备份还不到 30 分钟」，是就别再备了。

    为什么需要：刷镜像也发生在收发消息之后，聊得勤时几秒刷一次，
    不限制的话两份备份几乎一样，「最近 2 轮」就没意义了。
    """
    mine = _backups_of(bak_dir, rel_flat)
    if not mine:
        return False
    try:
        newest = mine[-1].stat().st_mtime
    except OSError:
        return False
    return (time.time() - newest) < CHAT_BACKUP_MIN_INTERVAL_SECONDS


def _backup_one(root: Path, path: Path) -> None:
    """把 `path` 的**旧内容**备份到 `<root>/Bak/`（写盘前调用）。

    · 文件不存在 / 是空的 → 没什么可备份，直接返回；
    · 聊天记录只留最后 1KB，而且**最多每 30 分钟一份**；
    · 任何出错都只记日志 —— 备份失败绝不能把「保存设置」整个搞失败。
    """
    try:
        if not path.is_file():
            return
        data = path.read_bytes()
        if not data:
            return
        bak_dir = _backup_dir(root)
        bak_dir.mkdir(parents=True, exist_ok=True)
        try:
            rel_flat = path.relative_to(root).as_posix().replace("/", "__")
        except ValueError:
            rel_flat = path.name
        is_chat = path.name in CHAT_BACKUP_NAMES
        if is_chat:
            if _chat_backup_too_soon(bak_dir, rel_flat):
                return  # 30 分钟内已经备过一份了
            data = _tail_bytes_utf8(data, CHAT_BACKUP_MAX_BYTES)
        # 时间戳精确到微秒：同一秒里连续写两次也会各留一份备份（按名字排序仍然等于按时间排序）
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = bak_dir / f"{rel_flat}.{stamp}.bak"
        target.write_bytes(data)
        _prune_backups(bak_dir, rel_flat)
    except Exception:  # noqa: BLE001 - 备份失败不影响主流程
        logger.exception("备份旧文件失败：%s", path)


def _dump_json(path: Path, data: Any, bak_root: Path | None = None) -> None:
    if bak_root is not None:
        _backup_one(bak_root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe_name(text: str) -> str:
    """把名字里不能做文件名的字符换掉（Windows 上尤其重要）。

    非法字符集合：反斜杠、斜杠、冒号、星号、问号、双引号、尖括号、竖线。
    """
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", (text or "").strip())
    cleaned = cleaned.strip(". ")
    return cleaned[:40] or "friend"


def friend_dir_name(ai_profile_id: int, name: str) -> str:
    """好友文件夹名 = `<AI档案ID>_<名字>`。

    用户对「两个好友重名怎么办」的答复是「撞名就撞名不用怕，ID不是不一样的吗」——
    所以前缀 ID 就足以保证唯一，名字只用来让人一眼看出是谁。
    """
    return f"{int(ai_profile_id)}_{_safe_name(name)}"


def mirror_account(account_id: str) -> None:
    """把某个账号的全部数据刷进 `data/accounts/<ID>/`（安全版，不抛异常）。

    调用点：注册、登录、改设置、改好友、发消息之后。
    镜像失败只记日志 —— 明文文件夹是给用户看的副本，不是主数据，
    它写不进去绝不能把「保存设置」这种事整个搞失败。
    """
    try:
        write_account_mirror(account_id)
    except Exception:  # noqa: BLE001 - 镜像失败不影响主流程
        logger.exception("刷新账号文件夹失败：%s", account_id)


def write_account_mirror(account_id: str) -> None:
    """镜像的真正实现（会抛异常，供测试直接调用）。

    这是一个**快照式**写入：按当前 DB 状态全量重写，
    再删掉已经不存在的友文件夹（删好友之后不留残骸）。
    """
    row = db.get_user_by_account(account_id)
    if row is None:
        return
    user_id = int(row["id"])
    root = account_dir(account_id)
    root.mkdir(parents=True, exist_ok=True)

    bak_root: Path | None = None if account_id == OWNER_ACCOUNT_ID else root

    # ---- account.json：账号本身 ----
    _dump_json(
        root / "account.json",
        {
            "account_id": account_id,
            "user_id": user_id,
            "nickname": row["display_name"],
            # 用户明确要求明文存储，方便自己查看
            "password": row["password_hash"],
            "avatar": _avatar_file_name(row),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
        bak_root=bak_root,
    )

    # ---- settings.json：软件名称 / 图标 / 桌面身份等自定义 ----
    _dump_json(
        root / "settings.json",
        {
            "app_name": _col(row, "app_name", ""),
            "icon_id": _col(row, "icon_id", ""),
            "launcher_index": _col(row, "launcher_index", 0),
        },
        bak_root=bak_root,
    )

    # ---- apis.json：这个账号的全局 API 池 ----
    configs = []
    for cfg_row in db.list_api_configs(user_id=user_id):
        configs.append(
            {
                "id": int(cfg_row["id"]),
                "provider": cfg_row["provider"],
                "model_name": cfg_row["model_name"],
                "base_url": cfg_row["base_url"],
                "role": cfg_row["role"],
                "priority": int(cfg_row["priority"] or 0),
                "is_primary": int(cfg_row["is_primary"] or 0),
                "api_key": _decode_key(cfg_row["api_key"]),
            }
        )
    _dump_json(root / "apis.json", configs, bak_root=bak_root)

    # ---- 头像文件 ----
    _mirror_avatar(root, row, bak_root)

    # ---- 每个好友一个文件夹 ----
    friends_root = root / "friends"
    friends_root.mkdir(parents=True, exist_ok=True)
    alive: set[str] = set()
    for ai_row in db.list_ai_profiles(user_id):
        ai_id = int(ai_row["id"])
        dir_name = friend_dir_name(ai_id, ai_row["name"])
        alive.add(dir_name)
        fdir = friends_root / dir_name
        fdir.mkdir(parents=True, exist_ok=True)
        _mirror_friend(fdir, user_id, ai_row, bak_root)

    # 人类好友也要落盘（用户 2026-09-27 第七轮：「要存进」明文文件夹）。
    #
    # 目录名用 `h<用户ID>_<昵称>` 前缀 `h` 区分：AI 好友是 `<AI档案ID>_<名字>`，
    # 而 users 与 ai_profiles 是两套各自从 1 开始的自增 ID，一定会撞号
    # （users.id=1 是账号主人，ai_profiles.id=1 是某个 AI）。不加前缀的话
    # `1_老王` 到底指哪个朋友就说不清了，两边还会互相覆盖。
    for peer_row in db.list_friend_links(user_id):
        peer_id = int(peer_row["id"])
        dir_name = f"h{peer_id}_{_safe_name(peer_row['display_name'] or '')}"
        alive.add(dir_name)
        fdir = friends_root / dir_name
        fdir.mkdir(parents=True, exist_ok=True)
        _mirror_human_friend(fdir, user_id, peer_row, bak_root)

    # 清掉已经被删掉的好友文件夹（删好友之后不留残骸）
    for child in friends_root.iterdir():
        if child.is_dir() and child.name not in alive:
            _rmtree(child)


def _col(row: Any, key: str, default: Any) -> Any:
    try:
        return row[key] if key in row.keys() and row[key] is not None else default
    except (IndexError, KeyError):
        return default


def _decode_key(stored: Any) -> str:
    """api_pool 里存的是 Base64，文件夹里按用户要求写明文。"""
    try:
        return base64.b64decode(str(stored or "").encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001 - 兼容明文历史数据
        return str(stored or "")


def _avatar_file_name(user_row: Any) -> str:
    avatar = _col(user_row, "avatar_url", "")
    if not avatar:
        return ""
    return Path(str(avatar)).name


def _mirror_avatar(root: Path, user_row: Any, bak_root: Path | None = None) -> None:
    """把用户头像复制成 `avatar<后缀>`。找不到源文件就跳过。"""
    avatar = _col(user_row, "avatar_url", "")
    if not avatar:
        return
    src = Path(str(avatar))
    if not src.is_file():
        return
    try:
        target = root / f"avatar{src.suffix or '.jpg'}"
        if src.resolve() != target.resolve():
            if bak_root is not None:
                _backup_one(bak_root, target)
            target.write_bytes(src.read_bytes())
    except OSError:
        logger.exception("复制头像失败：%s", avatar)


def _mirror_friend(
    fdir: Path, user_id: int, ai_row: Any, bak_root: Path | None = None
) -> None:
    """写一个好友的文件夹：profile.json / api.json / chat.jsonl / uploaded/。"""
    import personality as persona

    ai_id = int(ai_row["id"])
    raw_key = _col(ai_row, "api_key", "")
    _dump_json(
        fdir / "profile.json",
        {
            "ai_profile_id": ai_id,
            "name": ai_row["name"],
            "avatar": Path(str(ai_row["avatar_url"] or "")).name,
            "personality_data": persona.load_personality(ai_id),
            "state": {
                "emotion": float(ai_row["state_emotion"]),
                "energy": float(ai_row["state_energy"]),
                "intimacy": float(ai_row["state_intimacy"]),
            },
        },
        bak_root=bak_root,
    )
    _dump_json(
        fdir / "api.json",
        {
            "provider": _col(ai_row, "api_provider", ""),
            "base_url": _col(ai_row, "api_base_url", ""),
            "model": _col(ai_row, "api_model", ""),
            "api_key": _decode_key(raw_key),
        },
        bak_root=bak_root,
    )

    # 聊天记录：一行一条 JSON（jsonl 便于追加，也便于用户拿编辑器翻）
    lines: list[str] = []
    conv_id = db.find_single_conversation(user_id, ai_id)
    if conv_id is not None:
        for msg in db.list_messages(conv_id, limit=100000):
            lines.append(
                json.dumps(
                    {
                        "id": int(msg["id"]),
                        "sender": msg["sender_type"],
                        "content": db.content_to_array(msg["content"]),
                        "content_type": msg["content_type"],
                        "timestamp": msg["timestamp"],
                        "is_read": int(msg["is_read"] or 0),
                    },
                    ensure_ascii=False,
                )
            )
    text = "\n".join(lines)
    if text:
        text += "\n"
    if bak_root is not None:
        _backup_one(bak_root, fdir / "chat.jsonl")  # 聊天记录也备份（只留 1KB）
    (fdir / "chat.jsonl").write_text(text, encoding="utf-8")

    # uploaded/：这个好友名下上传过的聊天记录原文件（PRD 5.2）
    (fdir / "uploaded").mkdir(exist_ok=True)


def _mirror_human_friend(
    fdir: Path, user_id: int, peer_row: Any, bak_root: Path | None = None
) -> None:
    """写一个**人类好友**的文件夹：profile.json / chat.jsonl。

    用户 2026-09-27 第七轮原话：「人类好友的消息要存进明文文件夹」，
    格式与 AI 好友一致（`data/accounts/<ID>/friends/<对方ID>_<昵称>/chat.jsonl`）。

    人类好友没有 API 配置也没有人格档案，所以不写 api.json —— 写一个空文件
    只会让用户以为「这里本该有 Key，怎么是空的」。profile.json 里如实写
    `kind: "human"`，一眼能看出这是真人不是 AI。
    """
    peer_id = int(peer_row["id"])
    _dump_json(
        fdir / "profile.json",
        {
            "kind": "human",
            "user_id": peer_id,
            "account_id": peer_row["username"],
            "name": peer_row["display_name"],
            "avatar": _avatar_file_name(peer_row),
        },
        bak_root=bak_root,
    )

    lines: list[str] = []
    conv_id = db.find_human_conversation(user_id, peer_id)
    if conv_id is not None:
        for msg in db.list_messages(conv_id, limit=100000):
            lines.append(
                json.dumps(
                    {
                        "id": int(msg["id"]),
                        "sender": msg["sender_type"],
                        "content": db.content_to_array(msg["content"]),
                        "content_type": msg["content_type"],
                        "timestamp": msg["timestamp"],
                        "is_read": int(msg["is_read"] or 0),
                    },
                    ensure_ascii=False,
                )
            )
    text = "\n".join(lines)
    if text:
        text += "\n"
    if bak_root is not None:
        _backup_one(bak_root, fdir / "chat.jsonl")  # 聊天记录也备份（只留 1KB）
    (fdir / "chat.jsonl").write_text(text, encoding="utf-8")


def _rmtree(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path)
    except OSError:
        logger.exception("删除文件夹失败：%s", path)


# ================================================================
# 六、启动回灌（DB 没了也能从明文文件夹重建）
# ================================================================


def restore_from_folders() -> list[str]:
    """扫描 `data/accounts/`，把 DB 里没有的账号建回去。返回恢复的账号 ID 列表。

    这是用户要「换手机 / 删库重装也能拿回配置」的第二道保险：
    只要明文文件夹还在，账号就不会丢。

    只恢复**账号本身**（account.json + settings.json + apis.json）。
    好友、人格、聊天记录属于会话数据，需要 DB 与向量库配合，
    不在这里做——真遇到整库丢失，人工按文件夹恢复更稳妥。
    """
    root = Path(ACCOUNTS_DIR)
    if not root.is_dir():
        return []
    restored: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.isdigit():
            continue
        account_id = child.name.zfill(ACCOUNT_ID_LENGTH)
        if db.get_user_by_account(account_id) is not None:
            continue
        data = _load_json(child / "account.json")
        if not isinstance(data, dict):
            continue
        nickname = str(data.get("nickname") or "").strip()
        password = str(data.get("password") or "")
        if not nickname or not password:
            continue
        db.create_user_row(
            account_id=account_id, nickname=nickname, password=password
        )
        settings = _load_json(child / "settings.json")
        if isinstance(settings, dict):
            db.update_user_account(
                account_id,
                app_name=str(settings.get("app_name") or "") or None,
                icon_id=str(settings.get("icon_id") or "") or None,
            )
        restored.append(account_id)
        logger.info("已从明文文件夹恢复账号 %s（%s）", account_id, nickname)
    return restored


def account_snapshot(account_id: str) -> dict:
    """给客户端的账号资料（含全部自定义项）。

    密码**不出现在这里** —— 虽然库里是明文，但没有理由把它发回手机；
    用户在服务端文件夹里随时能看到。
    """
    row = db.get_user_by_account(account_id)
    if row is None:
        raise AccountError("账号不存在", code="bad_account_id")
    return {
        "account_id": account_id,
        "user_id": int(row["id"]),
        "nickname": row["display_name"],
        "avatar_url": _col(row, "avatar_url", "") or "",
        "app_name": _col(row, "app_name", "") or "",
        "icon_id": _col(row, "icon_id", "") or "",
        "launcher_index": int(_col(row, "launcher_index", 0) or 0),
        "is_developer": account_id == DEV_ACCOUNT_ID,
        "created_at": row["created_at"],
    }
