"""AIRP 后端入口：uv run -m main"""
import asyncio
import socket
import threading
import webbrowser

import uvicorn

from app.server import app

PORT = 25530


def main() -> None:
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/")).start()
    # 手动创建 IPV6_V6ONLY=0 的监听 socket，实现单 socket 双栈（IPv4 + IPv6）
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("::", PORT))
    sock.listen(2048)
    server = uvicorn.Server(uvicorn.Config(app, log_level="info"))
    asyncio.run(server.serve(sockets=[sock]))


if __name__ == "__main__":
    main()
