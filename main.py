"""AIRP 后端入口：uv run -m main [--port 25530] [--no-browser]"""
import argparse
import asyncio
import socket
import threading
import webbrowser

import uvicorn

from app.server import app


def main() -> None:
    parser = argparse.ArgumentParser(description="AIRP 后端")
    parser.add_argument("--port", type=int, default=25530, help="监听端口（默认 25530）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    # 手动创建 IPV6_V6ONLY=0 的监听 socket，实现单 socket 双栈（IPv4 + IPv6）
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    # 不设 SO_REUSEADDR：Windows 上它会让第二个实例静默抢占同一端口
    sock.bind(("::", args.port))
    sock.listen(2048)
    server = uvicorn.Server(uvicorn.Config(app, log_level="info"))
    asyncio.run(server.serve(sockets=[sock]))


if __name__ == "__main__":
    main()
