"""
launcher.py — 进程管家的桌面一键启动器

1. 检测 127.0.0.1:8791 是否已在监听（防多开）
2. 没在跑 → 用 CREATE_NO_WINDOW 静默拉起 manager_server.py（绝无黑框）
3. 最多等 5 秒，直到端口真正连通
4. 用默认浏览器打开控制台
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8791
BASE = Path(__file__).resolve().parent
URL = f"http://{HOST}:{PORT}/"


def port_ready(timeout: float = 0.4) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((HOST, PORT)) == 0
    except OSError:
        return False


def main() -> int:
    if not port_ready():
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pythonw) if pythonw.exists() else sys.executable
        # CREATE_BREAKAWAY_FROM_JOB：让管家脱离启动者所在的 Job Object
        base_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        cmd = [exe, str(BASE / "manager_server.py")]
        common = dict(cwd=str(BASE), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, close_fds=True)
        try:
            subprocess.Popen(cmd, creationflags=base_flags | breakaway, **common)
        except OSError:
            subprocess.Popen(cmd, creationflags=base_flags, **common)
        deadline = time.time() + 5.0
        while time.time() < deadline and not port_ready():
            time.sleep(0.5)

    webbrowser.open(URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
