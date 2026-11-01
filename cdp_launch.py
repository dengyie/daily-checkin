"""共享 Chrome CDP（9222）生命周期契约 —— 端口真空态允许 Agent 代启。

契约来源：[[Note/Infra/本机 Windows 常驻进程与内存清理]]（canonical P0 铁律）
+ [[Note/Infra/公益站签到运维手册]] §CDP 生命周期契约。

两种形态（2026-10-30 用户定调）：
  - **运行中实例**：attach-only。绝不 kill、绝不重启、绝不按原参再拉一个实例
    （同 --user-data-dir 撞 SingletonLock 会报 ``Browser is already in use``）。
  - **端口真空态**：允许代启。启动 = **恢复**，不是重启，零运行中会话可破坏。

代启前必须三项前置体检全过（任一不满足即判定为"运行中实例"，只 attach 不启动）：
  1. 目标端口无监听；
  2. 无任何 Chrome 进程；
  3. profile 目录内无 ``Singleton*`` 锁文件。

启动参数必须带 ``--restore-last-session``，否则 Chrome 打开空 profile，
所有站点的登录态（LINUX DO / GitHub / 签到站 / MetaMask 等）全部表现为已登出。
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

SHARED_CDP_PORT: int = 9222
DEFAULT_SHARED_PROFILE = Path.home() / "chrome-checkin-profile"

#: profile 目录名关键字 —— 只有命中它的 profile 才是共享签到浏览器
PROFILE_MARKER = "chrome-checkin-profile"


class SharedBrowserState:
    """9222 三项体检结果。"""

    __slots__ = ("port_listening", "chrome_running", "profile_locked")

    def __init__(self, port_listening: bool, chrome_running: bool, profile_locked: bool):
        self.port_listening = bool(port_listening)
        self.chrome_running = bool(chrome_running)
        self.profile_locked = bool(profile_locked)

    @property
    def is_vacant(self) -> bool:
        """三项全空 = 端口真空态，允许代启。"""
        return not self.port_listening and not self.chrome_running and not self.profile_locked

    def describe(self) -> str:
        return (
            f"port_listening={self.port_listening} "
            f"chrome_running={self.chrome_running} "
            f"profile_locked={self.profile_locked}"
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"SharedBrowserState({self.describe()})"


def probe_port(port: int = SHARED_CDP_PORT, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    """端口是否有人监听（只做 TCP 探测，不发 HTTP 请求）。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _is_windows() -> bool:
    return os.name == "nt"


def chrome_processes() -> list[str]:
    """列出本机 Chrome/Chromium 进程的命令行片段（找不到就是空列表）。"""
    if _is_windows():
        cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
            "| Select-Object -ExpandProperty CommandLine",
        ]
    else:
        cmd = ["ps", "-axo", "command="]
    try:
        raw = subprocess.check_output(
            cmd, text=True, errors="replace", stderr=subprocess.DEVNULL
        )
    except Exception:
        return []
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if _is_windows():
        return [ln for ln in lines if "chrome" in ln.lower()]
    return [
        ln
        for ln in lines
        if ("google chrome" in ln.lower() or "chromium" in ln.lower() or "/chrome" in ln.lower())
        and "--type=" not in ln
    ]


def chrome_running() -> bool:
    """本机是否还有 Chrome 进程（含 helper 之外的任意实例）。"""
    return bool(chrome_processes())


def profile_locked(profile: Path | str = DEFAULT_SHARED_PROFILE) -> bool:
    """profile 目录内是否存在 ``Singleton*`` 锁文件。"""
    try:
        path = Path(profile).expanduser()
    except Exception:
        return True
    if not path.is_dir():
        return False
    try:
        return any(path.glob("Singleton*"))
    except OSError:
        return True


def inspect_state(
    port: int = SHARED_CDP_PORT,
    profile: Path | str = DEFAULT_SHARED_PROFILE,
    port_probe=probe_port,
    proc_probe=chrome_running,
    lock_probe=profile_locked,
) -> SharedBrowserState:
    """三项体检。默认探针可注入，便于测试。"""
    return SharedBrowserState(
        port_listening=bool(port_probe(port)),
        chrome_running=bool(proc_probe()),
        profile_locked=bool(lock_probe(profile)),
    )


def default_chrome_bin() -> str:
    """探测 Chrome 可执行文件（Windows / macOS）。"""
    if _is_windows():
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
        ]
    else:
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    for c in candidates:
        if Path(c).exists():
            return c
    return "chrome"


def launch_shared_browser(
    port: int = SHARED_CDP_PORT,
    profile: Path | str = DEFAULT_SHARED_PROFILE,
    chrome_bin: str = "",
    launcher=None,
    wait_seconds: float = 30.0,
    sleep_fn=None,
    port_probe=probe_port,
) -> None:
    """在端口真空态拉起共享 Chrome CDP 浏览器。

    绝不 kill 任何进程；启动后轮询端口直至可达。
    """
    chrome = chrome_bin or default_chrome_bin()
    profile_path = Path(profile).expanduser()
    profile_path.mkdir(parents=True, exist_ok=True)
    argv = [
        chrome,
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session",
    ]
    if launcher is not None:
        launcher(argv)
    else:
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if _is_windows():
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, **kwargs)

    nap = sleep_fn or time.sleep
    deadline = time.monotonic() + float(wait_seconds)
    while time.monotonic() < deadline:
        if port_probe(port):
            return
        nap(0.5)
    raise RuntimeError(
        f"shared Chrome CDP did not come up on port {int(port)} within {wait_seconds}s"
    )
