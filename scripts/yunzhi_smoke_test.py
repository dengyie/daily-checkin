#!/usr/bin/env python3
"""yunzhi(云智手机)RPC 签到只读烟测 — 可用性验证,零业务副作用.

复用生产适配器同一条代码路径(stealth_checkin_runner._yunzhi_api / 签名函数),
仅调用只读接口,不触发任何领取/开通:
  1. 9222 登录态(localStorage cloud_phone_token);
  2. GET  /api/content/home-popups/init      (头签名 + 网络路径);
  3. POST /api/benefit/user/benefit          (体签名 + 头签名对最终 body);
  4. GET  /api/benefit/user/list             (权益目录含 benefit 158);
  5. GET  /api/benefit/user/memberStatus     (会员态)。

用法(与生产同环境变量,凭据只读自 9222 profile):
  source ~/daily-checkin-staging/env.sh
  /Users/mango/project/hermes/daily-checkin/.venv/bin/python \
      /Users/mango/project/hermes/daily-checkin/scripts/yunzhi_smoke_test.py
退出码:0 全绿 / 1 有失败。输出不含任何 token 明文。
"""
import asyncio
import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stealth_checkin_runner as m  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

CDP_HTTP = os.environ.get("CDP_HTTP", "http://127.0.0.1:9222")


def _cdp(method: str, path: str) -> bytes:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(CDP_HTTP.rstrip("/") + path, method=method)
    return urllib.request.urlopen(req, context=ctx, timeout=10).read()


async def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    print("=== yunzhi RPC 只读烟测 ===", flush=True)

    tab_id = None
    page = None
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_HTTP)
        ctx = browser.contexts[0]

        # 0) 双签名离线自检(不联网,验证签名函数未被改动)
        try:
            vec = m._yunzhi_md5_sign({"benefitConfigId": "158", "timestamp": "1790794831629"})
            check("MD5 体签名自检", vec == "2164c5c88db4005310250a27f3b7807e", vec[:16] + "…")
        except Exception as e:
            check("MD5 体签名自检", False, str(e)[:120])

        # 1) 开临时 tab(按 target ID 管理,P0 契约);只认新建 tab,
        #    避免误挂到用户已打开的 yunzhi 页面上做检查
        try:
            before = {id(pg) for pg in ctx.pages}
            info = json.loads(_cdp("PUT", f"/json/new?{m.YUNZI_ENTRY_URL}"))
            tab_id = info["id"]
            page = None
            for _ in range(12):
                await asyncio.sleep(0.5)
                new_pages = [pg for pg in ctx.pages if id(pg) not in before]
                page = next((pg for pg in new_pages if "yunzhi.play.cn" in pg.url), None)
                if page is not None:
                    break
            check("临时 tab + 页面加载", page is not None, m.YUNZI_ENTRY_URL)
        except Exception as e:
            check("临时 tab + 页面加载", False, str(e)[:120])

        if page is not None:
            # 2) 登录态
            token = device_no = ""
            try:
                creds = await page.evaluate(m._YUNZI_CREDS_JS)
                token = str((creds or {}).get("token") or "")
                device_no = str((creds or {}).get("deviceNo") or "")
            except Exception as e:
                check("登录态读取", False, str(e)[:120])
            if token:
                check("登录态", True, f"token {token[:12]}…{token[-6:]} / device {device_no or '(缺省)'}")
            else:
                check("登录态", False, "localStorage 无 cloud_phone_token(请在 9222 浏览器登录云智手机)")

            # 3) 只读探活(生产同路径 _yunzhi_api:双签名 + 页面内 fetch)
            if token:
                init = await m._yunzhi_api(page, "GET", "/api/content/home-popups/init", token, device_no)
                d, err = m._yunzhi_check_business(init, "home-popups/init")
                popups = d.get("popups") or []
                claimable = sum(1 for x in popups if isinstance(x, dict) and (x.get("canClaim") or x.get("state") == "CAN_CLAIM"))
                check("init(头签名+网络路径)", not err, err or f"popups={len(popups)} 可领={claimable}")

                det = await m._yunzhi_api(
                    page, "POST", "/api/benefit/user/benefit", token, device_no,
                    body_params={"benefitConfigId": m.YUNZI_BENEFIT_ID},
                )
                bd, err = m._yunzhi_check_business(det, "benefit/user/benefit")
                items = bd.get("userItems") or []
                devs = bd.get("cloudDevices") or []
                running = [x for x in devs if isinstance(x, dict) and x.get("status") == 2]
                detail = (err or
                          f"userItems={len(items)} 云机={len(devs)}(运行中{len(running)})"
                          + (f" {running[0].get('vendorResourceId')}" if running else "")
                          + f" remainingQuota={bd.get('remainingQuota')}")
                check("user/benefit(体签名+头签名)", not err, detail)

                ul = await m._yunzhi_api(page, "GET", "/api/benefit/user/list", token, device_no)
                dl, err = m._yunzhi_check_business(ul, "benefit/user/list")
                bens = (dl.get("benefits") or []) if isinstance(dl, dict) else []
                b158 = next((x for x in bens if isinstance(x, dict) and x.get("benefitConfigId") == 158), None)
                check("user/list(目录含 158)", not err and b158 is not None,
                      err or f"benefits={len(bens)} benefit158={bool(b158)}")

                ms = await m._yunzhi_api(page, "GET", "/api/benefit/user/memberStatus", token, device_no)
                md, err = m._yunzhi_check_business(ms, "benefit/user/memberStatus")
                check("memberStatus", not err, err or f"memberStatus={md.get('memberStatus')} 到期={md.get('memberExpireTime')}")

    ok_all = bool(results) and all(ok for _, ok, _ in results)
    if tab_id:
        try:
            _cdp("PUT", f"/json/close/{tab_id}")
            print("  临时 tab 已按 target ID 关闭", flush=True)
        except Exception:
            pass

    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"=== smoke done: PASS={len(results) - n_fail} FAIL={n_fail} exit={0 if ok_all else 1} ===", flush=True)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
