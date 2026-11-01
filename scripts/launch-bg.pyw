#!/usr/bin/env pythonw
"""无窗口后台启动 daily-checkin 服务（供 Windows 计划任务调用）。

用法：
    pythonw.exe scripts\\launch-bg.pyw api
    pythonw.exe scripts\\launch-bg.pyw web

不弹控制台；自动加载仓库根 .env；stdout/stderr 追加写入 data/<service>.log，
未捕获异常额外写入 data/launch-error.log（pythonw 下无控制台，否则静默失败）。
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    service = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if service not in {"api", "web"}:
        raise SystemExit("usage: launch-bg.pyw api|web")

    load_env(ROOT / ".env")
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.path.insert(0, str(ROOT))

    log = open(DATA / f"{service}.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log

    if service == "api":
        import runpy
        sys.argv = ["checkin_core.web", "--host", "127.0.0.1", "--port", "8765"]
        run = lambda: runpy.run_module("checkin_core.web", run_name="__main__")
    else:
        from checkin_core.frontend import main as run
        sys.argv = ["frontend", "--host", "127.0.0.1", "--port", "8766"]

    print(f"\n=== launch-bg {service} pid={os.getpid()} ===", flush=True)
    run()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        with open(DATA / "launch-error.log", "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {Path(__file__).name} pid={os.getpid()} ===\n")
            traceback.print_exc(file=fh)
        raise