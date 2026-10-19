"""双栈启动器：让服务端**同时**收 IPv4 和 IPv6。

为什么需要这个文件（2026-10-01 第二十五轮，用户要走 IPv6 广域网）：
    Windows 上 `uvicorn --host ::` 建出来的 socket 默认带 `IPV6_V6ONLY=1`
    （Windows 的默认行为），结果**只收 IPv6** —— 手机在同一个 WiFi 下用 IPv4
    反而连不上了。而 `--host 0.0.0.0` 又只收 IPv4，IPv6 直连进不来。

    所以这里自己建一个 socket：`IPV6_V6ONLY=0`（真正的双栈，IPv4 走 v4-mapped 地址），
    再通过 `fd=` 交给 uvicorn。这样：
      · 同一个 WiFi：手机填 ws://<局域网IPv4>:8000/ws          —— 照旧
      · 任何网络：  手机填 ws://[<公网IPv6>]:8000/ws           —— 新增
    两种方式同时可用，不需要两条命令、不需要两个进程。

用法：
    <venv python> run_server.py            # 默认 8000 端口
    <venv python> run_server.py --port 9000 --host ::
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys


def make_dual_stack_socket(host: str, port: int, backlog: int = 2048) -> socket.socket:
    """建一个 IPv6 socket 并关掉 V6ONLY，于是 IPv4/IPv6 都收。"""
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    except OSError as exc:  # pragma: no cover - 平台不支持时才走到
        print(f"[warn] 关闭 IPV6_V6ONLY 失败（将只收 IPv6）：{exc}", file=sys.stderr)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(backlog)
    return sock


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="::", help="监听地址，默认 ::（双栈）")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--app", default="main:app")
    args = ap.parse_args()

    # 局域网自动发现（discovery.py）挂在 FastAPI 的 lifespan 上，它需要知道真实端口：
    # 用环境变量把端口传进去，免得它写死 8000（测试/换端口时会绑错）。
    os.environ["ZUICHENG_PORT"] = str(args.port)

    try:
        sock = make_dual_stack_socket(args.host, args.port)
    except OSError as exc:
        print(f"[错误] 绑定 {args.host}:{args.port} 失败：{exc}", file=sys.stderr)
        return 1

    v6only = sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
    print(
        f"[启动] 双栈监听 {args.host}:{args.port}"
        f"（IPV6_V6ONLY={v6only}，0 = IPv4 也收）",
        flush=True,
    )

    import uvicorn

    # 注意：不能用 uvicorn 的 fd= 参数 —— 它在 Windows 上会走
    # `socket.fromfd(fd, socket.AF_UNIX, ...)`，而这套 Python 没有 AF_UNIX，
    # 直接 AttributeError（实测过）。用 sockets= 把已经 bind 好的双栈 socket
    # 交给它，才是 Windows 上正确的做法。
    #
    # 局域网自动发现的应答器不在这里起：它跟延迟队列一样挂在 main.py 的 lifespan 上
    # （2026-10-01 第二十八轮）。之前在这里用 asyncio.ensure_future 起过，报
    # 「There is no current event loop」——那时候 uvicorn 的事件循环还没建起来。
    config = uvicorn.Config(args.app, log_level="info")
    server = uvicorn.Server(config)
    asyncio.run(server.serve(sockets=[sock]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
