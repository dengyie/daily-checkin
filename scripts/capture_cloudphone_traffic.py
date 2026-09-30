#!/usr/bin/env python3
"""CDP Network Capture tool for Yunzhi Cloudphone check-in flows.

Attaches to Chrome 9222 via CDP, listens to all network requests and responses
matching yunzhi / new-gm domains, and writes full payloads into JSONL.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

OUT_FILE = Path(os.environ.get("DAILY_CHECKIN_LOG_DIR", Path.home() / "daily-checkin-staging" / "log")) / "yunzhi_capture.jsonl"
MATCH_DOMAINS = ["yunzhi", "new-gm", "play.cn"]
# P0 凭据边界:秘密不进 JSONL —— 落盘前对敏感头做截断脱敏
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key"}


def redact_headers(headers: dict) -> dict:
    out = {}
    for k, v in (headers or {}).items():
        if k.lower() in SENSITIVE_HEADERS and isinstance(v, str) and len(v) > 24:
            out[k] = v[:12] + "…" + v[-6:]
        else:
            out[k] = v
    return out

async def main():
    timeout_s = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    print(f"[*] Starting CDP network capture for {timeout_s}s (log: {OUT_FILE})...", flush=True)

    captured_count = 0
    start_time = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        
        async def handle_request(req):
            url = req.url
            if not any(d in url for d in MATCH_DOMAINS):
                return
            if any(ext in url for ext in [".png", ".jpg", ".jpeg", ".gif", ".css", ".woff", ".woff2", ".svg", ".ico"]):
                return
            
            entry = {
                "time": datetime.now().isoformat(),
                "type": "request",
                "method": req.method,
                "url": url,
                "headers": redact_headers(dict(req.headers)),
                "post_data": req.post_data,
            }
            with open(OUT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            print(f" -> [REQ] {req.method} {url[:90]}", flush=True)

        async def handle_response(resp):
            url = resp.url
            if not any(d in url for d in MATCH_DOMAINS):
                return
            if any(ext in url for ext in [".png", ".jpg", ".jpeg", ".gif", ".css", ".woff", ".woff2", ".svg", ".ico"]):
                return
            
            nonlocal captured_count
            captured_count += 1
            body_text = ""
            try:
                if "application/json" in resp.headers.get("content-type", "") or "text" in resp.headers.get("content-type", ""):
                    body_text = await resp.text()
            except Exception:
                pass

            entry = {
                "time": datetime.now().isoformat(),
                "type": "response",
                "status": resp.status,
                "url": url,
                "headers": redact_headers(dict(resp.headers)),
                "body": body_text[:4000],
            }
            with open(OUT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            print(f" <- [RESP {resp.status}] {url[:90]} (body_len={len(body_text)})", flush=True)

        hooked_pages = set()
        
        def hook_page(pg):
            if pg in hooked_pages:
                return
            hooked_pages.add(pg)
            pg.on("request", lambda r: asyncio.create_task(handle_request(r)))
            pg.on("response", lambda r: asyncio.create_task(handle_response(r)))

        for ctx in browser.contexts:
            ctx.on("page", hook_page)
            for page in ctx.pages:
                hook_page(page)

        print(f"[*] Attached to {len(hooked_pages)} existing pages. Listening...", flush=True)

        while time.time() - start_time < timeout_s:
            for ctx in browser.contexts:
                for page in ctx.pages:
                    hook_page(page)
            await asyncio.sleep(1)

        print(f"[*] Capture window of {timeout_s}s finished. Total items captured: {captured_count}.", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
