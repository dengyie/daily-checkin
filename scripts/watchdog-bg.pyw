#!/usr/bin/env pythonw
"""执行节点看护：只保 dispatch-agent(9777) 在线，不再拉起本地面板。

2026-11-09 起 Windows 节点收敛为「纯执行节点」：本地 8765/8766 前端已停用，
签到查看/操作统一走线上统一面板，面板通过 agent 派发作业。本脚本仍由计划
任务 ``DailyCheckinWatchdog`` 每 5 分钟无窗口调用：

  * 9777 未监听 → 拉起 dispatch agent（隐藏窗口）；
  * 9777 未监听但已有 agent 进程（启动中）→ 不重复拉起；
  * 清扫 agent 被强杀后遗留的 ssh 隧道：子 ssh 仍占住面板 19778/19780，
    新 agent 会反复报 ``remote port forwarding failed``（2026-11-09 事故）；
    agent 启动时也会自行清扫一次，这里是双保险；
  * **绝不重启 8765/8766**：旧实例若仍在监听只记录提示，不再拉起。

pythonw 运行，无控制台。只做「没在监听才拉起」，不重启健康实例。
"""
from __future__ import annotations

import datetime
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_VBS = ROOT / "scripts" / "start-dispatch-agent-win-hidden.vbs"
LOG_FILE = ROOT / "log" / "watchdog.log"
AGENT_PORT = 9777
CALLBACK_PORT = 19780
REVERSE_PORT = 19778
CREATE_NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    line = f"[watchdog {datetime.datetime.now():%Y-%m-%dT%H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _ps(script: str) -> str:
    """Best-effort PowerShell probe; a watchdog pass must not die on a probe."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=30,
        )
        return proc.stdout or ""
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"powershell probe failed: {type(exc).__name__}: {exc}")
        return ""


def agent_process_running() -> bool:
    out = _ps(
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
        "| Where-Object { $_.CommandLine -like '*checkin_core.dispatch_agent*' } "
        "| Measure-Object | Select-Object -ExpandProperty Count"
    )
    return out.strip().isdigit() and int(out.strip()) > 0


def sweep_orphan_tunnels() -> int:
    """Kill ssh.exe children still forwarding the agent's panel ports."""
    specs = [
        f"127.0.0.1:{REVERSE_PORT}:127.0.0.1:{AGENT_PORT}",
        f"127.0.0.1:{CALLBACK_PORT}:127.0.0.1:8765",
    ]
    killed = 0
    for spec in specs:
        out = _ps(
            "Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" "
            f"| Where-Object {{ $_.CommandLine -like '*{spec}*' }} "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "
            "Write-Output $_.ProcessId }"
        )
        killed += sum(1 for line in out.splitlines() if line.strip().isdigit())
    return killed


def launch_agent() -> None:
    subprocess.Popen(
        ["wscript.exe", "//B", str(AGENT_VBS)],
        cwd=str(ROOT),
        creationflags=CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    # 本地面板已下线：只提示，不复活。
    for port in (8765, 8766):
        if listening(port):
            log(f"legacy local UI port {port} still listening; leave it alone (panel is authoritative)")

    if listening(AGENT_PORT):
        return
    if agent_process_running():
        log("agent process present but 9777 not listening yet; skip relaunch")
        return

    killed = sweep_orphan_tunnels()
    if killed:
        log(f"killed {killed} orphaned tunnel process(es) before relaunch")
        time.sleep(1.0)  # let the panel sshd release the forwarded ports
    log("dispatch agent not listening on 9777; relaunching")
    launch_agent()


if __name__ == "__main__":
    main()
