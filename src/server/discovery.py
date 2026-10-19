"""局域网自动发现应答器（2026-10-01 第二十八轮，用户「找找其他的办法」）。

背景（用户实测）：
    电脑挂在**手机热点**下时（192.168.43.x），手机热点按设计就不允许入站，
    运营商那边又是 CGNAT —— 公网入站在这个环境下**不可能**。
    但热点下「手机和电脑本来就在同一个网里」，局域网直连一直是好的。
    用户真正的痛点是：**换个网络 / 重连热点后电脑 IP 变了，App 里就得手改地址。**

做法（纯本地、不经过任何第三方、不需要账号）：
    App 在同一个局域网里发一个 UDP 广播探测包，本模块收到就回一句
    「我在这儿，端口是多少」，App 据此自动把服务器地址填好。

协议（客户端 `src/client/lib/services/server_discovery.dart` 必须与这里一致）：
    探测：任意 UDP 报文，正文里包含 PROBE_MAGIC
    应答：REPLY_MAGIC + 空格 + 一个 JSON：{"name": "<电脑名>", "port": 8000}

生命周期跟 delay_queue 一样：main.py 的 lifespan 里 start()，退出时 stop()。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger("discovery")

PROBE_MAGIC = "ZUICHENG_PROBE"
REPLY_MAGIC = "ZUICHENG_SERVER"

# 虚拟网卡的 host-only 网段（VMware / VirtualBox 的默认值）。
# 手机永远到不了这些地址，报给 App 只会让它白试一轮，所以直接不发。
VIRTUAL_IPV4_PREFIXES = ("192.168.17.", "192.168.73.")

_transports: list = []
_task: asyncio.Task | None = None


def cloud_url(path: Path | None = None) -> str | None:
    """电脑当前对外的**云端地址**（Cloudflare 隧道），没有就返回 None。

    2026-10-02（用户改用 Cloudflare Tunnel）：快速隧道的网址**每次重启都会变**，
    所以没法写死在 APK 里。做法改成「电脑告诉手机」：

      · `start_cloud.bat` 每拿到一个新地址就写进项目根目录的 `cloud_url.txt`；
      · 这里在读应答时读一次那个文件，作为 `cloud` 字段放进探测应答；
      · 手机存下来，出门（局域网连不上）时自动切过去用。

    也支持用环境变量 `ZUICHENG_CLOUD_URL` 直接指定（测试/临时用）。
    """
    env = (os.environ.get("ZUICHENG_CLOUD_URL") or "").strip()
    if env.startswith(("ws://", "wss://")):
        return env
    if path is None:
        # discovery.py 在 src/server/ 下，项目根目录是上两级
        path = Path(__file__).resolve().parents[2] / "cloud_url.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if text.startswith(("ws://", "wss://")) else None


def prefer_same_subnet(addrs: list[str], prober: str) -> list[str]:
    """把「和探测方同一个网段」的地址排到最前面（第二十九轮实测后加的）。

    实测（2026-10-02）：电脑上装了 VMware，虚拟网卡有 192.168.x.x / 192.168.x.x。
    手机广播探测时，电脑会在**每一张网卡**上都收到一份，回包从哪张网卡出去
    取决于系统路由 —— 实测回包从虚拟网卡出去了，App 拿到 192.168.x.x 必然连不上。
    把和探测方同网段的地址排前面，手机就能一次连上真实的那张网卡。
    """
    if not prober or ":" in prober:
        return list(addrs)
    prefix = prober.rsplit(".", 1)[0] + "." if "." in prober else ""
    if not prefix:
        return list(addrs)
    same = [a for a in addrs if a.startswith(prefix)]
    other = [a for a in addrs if not a.startswith(prefix)]
    return same + other


def local_addresses() -> list[str]:
    """本机所有「别人可能用来找到我」的**局域网 IPv4** 地址（第二十九轮新增）。

    用户实测：「同一网络下 IPv4 能连、手抄 IPv6 连不上」。与其让用户去
    start.bat 里抄地址，不如让电脑把每个地址都报一遍，App 挨个真连一下。

    2026-10-02 用户明确说「局域网 IPV4 就够了，没必要再用 IPV6」—— 所以这里
    **只报 IPv4**（回环、169.254、虚拟网卡的 host-only 网段一律不要）。
    以前报 IPv6 只是多给 App 几个候选，现在没必要了，也少一层不确定性。
    """
    v4: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
    except OSError:
        return []
    for info in infos:
        try:
            addr = str(info[4][0]).split("%")[0]
        except (IndexError, TypeError):  # pragma: no cover
            continue
        if ":" in addr:
            continue  # IPv6 不报（用户要求只走局域网 IPv4）
        if addr in ("127.0.0.1",) or addr.startswith("127.") or addr.startswith("169.254."):
            continue
        # 虚拟网卡的 host-only 网段：外人根本到不了，报出去只会让 App 白试
        # （和 scripts/net_addresses.ps1 里的排除保持一致）
        if addr.startswith(VIRTUAL_IPV4_PREFIXES):
            continue
        if addr not in v4:
            v4.append(addr)
    return v4[:8]


def build_discovery_reply(
    port: int,
    hostname: str,
    addrs: list[str] | None = None,
    cloud: str | None = None,
) -> bytes:
    """构造应答字节（抽出来方便直接测）。

    `cloud` 是电脑当前对外可用的云端地址（Cloudflare 隧道），手机存下来出门用。
    没有隧道时不带这个字段，老客户端也会忽略它。
    """
    payload: dict = {"name": hostname, "port": int(port)}
    if addrs:
        payload["addrs"] = list(addrs)
    if cloud:
        payload["cloud"] = cloud
    return f"{REPLY_MAGIC} {json.dumps(payload, ensure_ascii=False)}".encode("utf-8")


def parse_discovery_reply(data: bytes) -> dict | None:
    """解析应答（客户端逻辑的镜像，服务端测试里用来验证自己发的东西能被读懂）。"""
    text = data.decode("utf-8", errors="ignore")
    if not text.startswith(REPLY_MAGIC):
        return None
    body = text[len(REPLY_MAGIC):].strip()
    try:
        obj = json.loads(body)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_discovery_probe(data: bytes) -> bool:
    """判断一个 UDP 报文是不是我们的探测包。"""
    try:
        return PROBE_MAGIC in data.decode("utf-8", errors="ignore")
    except Exception:  # pragma: no cover
        return False


async def _bind(port: int) -> list:
    """在两个协议族上各绑一个 UDP socket（哪个绑不上都不影响另一个）。"""
    loop = asyncio.get_running_loop()
    bound = []

    async def _one(family: int, host: str) -> None:
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                try:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                except OSError:
                    pass
            sock.bind((host, port))
            sock.setblocking(False)
        except OSError as exc:
            logger.warning("发现应答器没绑上 %s:%s/udp：%s", host, port, exc)
            return
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Proto(port), sock=sock
        )
        bound.append(transport)

    await _one(socket.AF_INET, "0.0.0.0")
    await _one(socket.AF_INET6, "::")
    return bound


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, port: int) -> None:
        self.port = port
        self.transport = None
        # 地址列表在启动时算一次就够（换网络要重启 start.bat，那时会重算）
        self._addrs = local_addresses()
        self._hostname = socket.gethostname()

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        if not parse_discovery_probe(data):
            return
        try:
            # 每次按探测方所在网段重排：同网段的地址排最前，手机一次就能连上
            ordered = prefer_same_subnet(self._addrs, addr[0] if addr else "")
            # 云端地址（Cloudflare 隧道）每次现读 —— 隧道重启换了地址也能马上告诉手机
            cloud = cloud_url()
            reply = build_discovery_reply(self.port, self._hostname, ordered, cloud)
            self.transport.sendto(reply, addr)
            logger.info(
                "回应了来自 %s 的自动发现探测（%s:%s，候选地址 %s%s）",
                addr[0], self._hostname, self.port, ",".join(ordered) or "无",
                "，云端地址 " + cloud if cloud else "",
            )
        except Exception:  # pragma: no cover
            logger.exception("回应自动发现探测失败")


def start(port: int = 8000) -> None:
    """启动应答器（main.py 的 lifespan 调用）。"""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_run(port))


async def _run(port: int) -> None:
    global _transports
    try:
        _transports = await _bind(port)
    except Exception:  # pragma: no cover
        logger.exception("自动发现应答器启动失败")
        return
    if not _transports:
        return
    logger.info("局域网自动发现已就绪：UDP %s（同一网络下的手机可自动找到这台电脑）", port)
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        for t in _transports:
            try:
                t.close()
            except Exception:
                pass
        _transports = []
        raise


async def stop() -> None:
    """停止应答器（lifespan 退出时调用）。"""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None
