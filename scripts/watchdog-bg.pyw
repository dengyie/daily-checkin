#!/usr/bin/env pythonw
"""端口看门狗：8765/8766 未监听时后台拉起对应服务。

供 Windows 计划任务每 5 分钟无窗口调用；pythonw 运行，不创建控制台。
只做「没在监听才拉起」，不做重启，避免打断正在运行的实例。
"""
from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "launch-bg.pyw"
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    for service, port in (("api", 8765), ("web", 8766)):
        if listening(port):
            continue
        subprocess.Popen(
            [sys.executable, str(LAUNCHER), service],
            cwd=str(ROOT),
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    main()