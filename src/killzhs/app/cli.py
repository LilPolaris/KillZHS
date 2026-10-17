"""Startup, single-instance locking and desktop/service lifetimes."""

import argparse
import json
import os
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path
from urllib.request import urlopen

from .server import LocalServer
from .store import atomic_json, load_json

ROOT = Path(__file__).resolve().parents[3]


def assets_directory():
    return Path(getattr(sys, "_MEIPASS", ROOT)) / "web"


def default_data_directory(*, frozen=None, demo=False):
    if demo:
        return Path(tempfile.gettempdir()) / "killzhs-v2-demo"
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KillZHS" / "v2"
    return ROOT / ".local" / "data"


class InstanceLock:
    """One service per data directory, regardless of its chosen HTTP port."""

    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.stream = (directory / "instance.lock").open("a+b")
        self.stream.seek(0, 2)
        if not self.stream.tell():
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise

    def close(self):
        self.stream.close()


def main(argv=None):
    from .controller import Controller

    parser = argparse.ArgumentParser(description="KillZHS 本地学习工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--demo", action="store_true", help="仅使用本地演示数据，不连接平台")
    parser.add_argument("--data-dir", type=Path, help="独立的用户数据目录")
    parser.add_argument("--desktop", action="store_true", help="显示后台控制窗口")
    parser.add_argument("--headless", action="store_true", help="以服务模式运行")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    data_dir = args.data_dir or default_data_directory(demo=args.demo)
    try:
        instance = InstanceLock(data_dir)
    except OSError:
        metadata = load_json(data_dir / "instance.json", {})
        port = metadata.get("port", args.port) if isinstance(metadata, dict) else args.port
        if type(port) is int and 1 <= port <= 65535 and not args.no_browser:
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as response:
                    if json.load(response).get("app") == "KillZHS":
                        webbrowser.open(f"http://127.0.0.1:{port}")
                        return 0
            except (OSError, ValueError):
                pass
        print("KillZHS 已在运行，或用户目录暂时无法使用。")
        return 1
    controller = None
    server = None
    try:
        controller = Controller(demo=args.demo, data_dir=data_dir)
        try:
            server = LocalServer(("127.0.0.1", args.port), controller, assets_directory())
        except OSError:
            print(f"端口 {args.port} 无法使用，请换一个端口：killzhs --port 8766")
            return 1
        atomic_json(data_dir / "instance.json", {"port": args.port, "pid": os.getpid()})
        controller.start_scheduler()
        url = f"http://127.0.0.1:{args.port}"
        print(f"KillZHS 已启动：{url}" + ("（演示模式）" if args.demo else ""), flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        if (args.desktop or getattr(sys, "frozen", False)) and not args.headless:
            from .desktop import desktop_window

            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .3}, daemon=True)
            thread.start()
            try:
                desktop_window(server, url)
            finally:
                server.closing.set()
                server.shutdown()
                thread.join(timeout=5)
        else:
            server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        print("正在关闭工作台…")
    finally:
        if server is not None:
            server.closing.set()
            server.server_close()
        if controller is not None:
            controller.close()
        instance.close()
    return 0
