"""AIRP 后端入口：uv run -m main [--host 127.0.0.1] [--port 25530] [--no-browser] [--no-keep-awake]"""
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
    parser.add_argument(
        "--host",
        default="::",
        help="监听地址（默认 :: 双栈；服务器上建议 127.0.0.1 只让反代访问）",
    )
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
    from app import auth, core

    try:
        eps = core.endpoints(core.load_config())
    except ValueError as e:
        raise SystemExit(f"配置错误: {e}")
    logging.info("已加载 %d 个模型端点，默认: %s", len(eps), eps[0]["display_name"])

    # 鉴权配置同样启动即校验：开了鉴权却没有账号，等于谁都进不来（也提示别把用户名写错）
    try:
        auth_cfg = auth.auth_settings()
    except ValueError as e:
        raise SystemExit(f"配置错误: {e}")
    if auth_cfg["enabled"]:
        if not auth_cfg["users"]:
            raise SystemExit("配置错误: auth.enabled 为 true 但 auth.users 里没有任何账号")
        logging.info(
            "已启用用户名密码鉴权，账号: %s（登录态 %g 天）",
            "、".join(auth_cfg["users"]),
            auth_cfg["ttl"] / 86400,
        )
    else:
        logging.info("未启用鉴权（auth.enabled: false）：任何能访问该端口的人都能读写全部会话")

    # 手动创建监听 socket：默认 :: 且 IPV6_V6ONLY=0 实现单 socket 双栈（IPv4 + IPv6）。
    # 指定 IPv4 地址（如 127.0.0.1）时按解析结果用 AF_INET——AF_INET6 绑不了 v4 字面量。
    family = socket.getaddrinfo(args.host, args.port, type=socket.SOCK_STREAM)[0][0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.listen(2048)
    # proxy_headers：放在 Caddy/nginx 后面时，日志与限速用得到真实的客户端地址
    server = uvicorn.Server(uvicorn.Config(app, log_level="info", proxy_headers=True))

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
    except KeyboardInterrupt:
        # 优雅关闭后 uvicorn 会把捕获到的 SIGINT 原样重放（Server.capture_signals 的 finally），
        # asyncio.run 再把它包成 KeyboardInterrupt 抛出来。uvicorn 自己的 run() 就是这么吞掉的；
        # 不接住的话进程会带着一串 CancelledError → KeyboardInterrupt 的回溯退出。
        pass
    finally:
        if args.keep_awake:
            keep_awake(False)

    logging.info("已停止")


if __name__ == "__main__":
    main()
