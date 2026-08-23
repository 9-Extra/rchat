"""AIRP 后端入口：uv run -m main [--port 25530] [--no-browser]"""
import argparse
import asyncio
import logging
import socket
import webbrowser

import uvicorn

from app.server import app


def main() -> None:
    parser = argparse.ArgumentParser(description="AIRP 后端")
    parser.add_argument("--port", type=int, default=25530, help="监听端口（默认 25530）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # 手动创建 IPV6_V6ONLY=0 的监听 socket，实现单 socket 双栈（IPv4 + IPv6）
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("::", args.port))
    sock.listen(2048)
    server = uvicorn.Server(uvicorn.Config(app, log_level="info"))

    async def serve() -> None:
        if not args.no_browser:
            # 等服务器真正完成启动（server.started 置位）后再打开浏览器
            async def open_when_ready() -> None:
                while not server.started:
                    await asyncio.sleep(0.05)
                webbrowser.open(f"http://127.0.0.1:{args.port}/")

            asyncio.create_task(open_when_ready())
        await server.serve(sockets=[sock])

    asyncio.run(serve())


if __name__ == "__main__":
    main()
