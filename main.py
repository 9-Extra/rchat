"""AIRP 后端入口：uv run -m main [--port 25530] [--no-browser] [--no-keep-awake]"""
import argparse
import asyncio
import ctypes
import logging
import socket
import sys
import webbrowser

import uvicorn

from app.server import app

# SetThreadExecutionState 的标志位：持续生效 + 请求系统保持唤醒
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(enabled: bool) -> bool:
    """置位/复位当前线程的「保持唤醒」，返回该平台是否支持。

    状态按线程生效，进程退出即自动清除，因此开关需在同一线程完成。
    """
    if sys.platform != "win32":
        return False
    flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if enabled else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="AIRP 后端")
    parser.add_argument("--port", type=int, default=25530, help="监听端口（默认 25530）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument(
        "--keep-awake",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="服务运行期间阻止系统因空闲睡眠/休眠（默认开启）",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # 启动即校验模型端点配置：没有可用端点时直接报错退出，而不是等首轮生成才失败
    from app import core

    try:
        eps = core.endpoints(core.load_config())
    except ValueError as e:
        raise SystemExit(f"配置错误: {e}")
    logging.info("已加载 %d 个模型端点，默认: %s", len(eps), eps[0]["display_name"])

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

    if args.keep_awake:
        if keep_awake(True):
            logging.info("已阻止系统休眠（--no-keep-awake 可关闭）")
        else:
            logging.info("当前平台不支持保持唤醒，已忽略 --keep-awake")

    try:
        asyncio.run(serve())
    finally:
        if args.keep_awake:
            keep_awake(False)


if __name__ == "__main__":
    main()
