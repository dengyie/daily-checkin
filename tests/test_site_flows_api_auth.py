#!/usr/bin/env python3
"""Migrated from legacy test_stealth_checkin_m1.py (mechanical move).
Assertions are unchanged; shared harness comes from tests/_support.py.
"""
from pathlib import Path
from unittest import mock
import asyncio
import inspect
import json
import re
import tempfile
import pytest
import unittest
import yaml

from tests._support import (
    FakePage,
    ROOT,
    TARGET,
    load_mod,
)

pytestmark = [pytest.mark.runner]


class YunzhiCheckinTest(unittest.TestCase):
    """yunzhi (yunzhi.play.cn 云智手机 云机空间权益) 纯 RPC 签到流程单测 (2026-10-01).

    协议来自 2026-10-01 抓包逆向:
    - 头签名 HMAC-SHA256("METHOD\\n/yunzhi+path\\nparams\\nbody\\nheaders\\n", 盐 8822FF81...),
      头规范化排除 Qu 表;30/30 抓包样本离线复算逐字节一致。
    - 体签名 MD5(除 sign 外参数升序 k=v & 拼接 + 盐 7f9e2d08...),6/6 样本一致。
    - 流程: home-popups/init → {popupId}/claim → benefit/user/benefit(158)
      取 userItems + 运行中云机 → benefit/claim → 轮询 claim/status 至 status=1。
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio

    def _adapter(self):
        return self.m.SiteAdapter(
            name="yunzhi",
            url="https://yunzhi.play.cn/ai/?channel_code=00000042",
            kind="yunzhi",
            sign_selectors=['text=开心收下', 'text=立即领取'],
            already_selectors=['text=今日已领取', 'text=明天再来'],
            prefer_catalog_url=True,
            login_url="https://yunzhi.play.cn/ai/#/login",
        )

    def test_is_yunzhi_site_and_name(self):
        self.assertTrue(self.m.is_yunzhi_site("https://yunzhi.play.cn/ai/?channel_code=00000042"))
        self.assertTrue(self.m.is_yunzhi_site("https://act.new-gm.cn/hd/t/benefit_detail/?benefit_id=158"))
        self.assertTrue(self.m.is_yunzhi_site("https://yunzhi.new-gm.cn/yunzhi/api/benefit/user/list"))
        self.assertFalse(self.m.is_yunzhi_site("https://example.com"))
        self.assertTrue(self.m.is_yunzhi_name("yunzhi"))
        self.assertTrue(self.m.is_yunzhi_name("云智手机"))
        self.assertFalse(self.m.is_yunzhi_name("glados"))

    def test_kind_in_literal_and_yaml_and_builtin(self):
        self.assertIn("yunzhi", self.m.AdapterKind.__args__)
        import yaml
        yaml_path = ROOT / "sites.yaml"
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "yunzhi"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 yunzhi 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "yunzhi")
        self.assertEqual(a.url, "https://yunzhi.play.cn/ai/?channel_code=00000042")
        self.assertTrue(a.prefer_catalog_url)
        b = next((x for x in _BUILTIN_SITE_ADAPTERS if x.name == "yunzhi"), None)
        self.assertIsNotNone(b, "内置 adapter 应包含 yunzhi")
        self.assertEqual(b.kind, "yunzhi")
        # 字段等价(sites.yaml 与 _BUILTIN_SITE_ADAPTERS 必须同步)
        for k in vars(b):
            self.assertEqual(getattr(a, k), getattr(b, k), f"字段 {k} 不等价")

    def test_p0_no_clear_cookies_in_yunzhi_path(self):
        import inspect
        for fn_name in ("yunzhi_checkin", "_yunzhi_api", "_yunzhi_build_headers", "_yunzhi_load_external_token", "_yunzhi_save_external_token"):
            src = inspect.getsource(getattr(self.m, fn_name)).lower()
            self.assertNotIn("clear_cookies", src, f"{fn_name} 不得调用 clear_cookies")
            self.assertNotIn("clear_localstorage", src, f"{fn_name} 不得清 localStorage")

    def test_yunzhi_md5_sign_captured_vectors(self):
        """体签名与 2026-10-01 抓包样本逐字节一致(样本为业务 ID,非凭据)."""
        cases = [
            ({"benefitConfigId": "158", "timestamp": "1790794831629"},
             "2164c5c88db4005310250a27f3b7807e"),
            ({"userItemId": 355236345335936, "timestamp": "1790794843383",
              "resourceId": "D0026092223823038"},
             "2536063923750f79cd4677a1f64460b9"),
            ({"claimId": 2720635, "timestamp": "1790794845048"},
             "4b0d3010875a47dcda38b1f80861f79a"),
            ({"resourceType": "LOBSTER", "effectiveSeconds": "86400",
              "timestamp": 1790794812373},
             "85c85c16d74699477e9e13deabd01579"),
        ]
        for params, want in cases:
            self.assertEqual(self.m._yunzhi_md5_sign(params), want, f"向量 {params} 不匹配")

    def test_yunzhi_hmac_sign_node_vector(self):
        """头签名与 Node crypto 独立生成的向量一致(固定时间戳/request_id,假 token)."""
        headers = {
            "authorization": "TEST_TOKEN_FAKE_123",
            "device_type": "3", "client_type": "h5", "channel_code": "00000042",
            "version": "10310", "api_version": "1", "device_no": "a3eef24f4e96d698",
            "timestamp": "1790794811525", "request_id": "5d67f86522734bceb951d058e3a7efed",
            "accept": "application/json", "content-type": "application/json",
            "cache-control": "no-cache",
        }
        got = self.m._yunzhi_hmac_sign("GET", "/yunzhi/api/content/home-popups/init", None, None, headers)
        self.assertEqual(
            got,
            "2ec6f56786c4c0b1b62a517931e98a038b30ae2b6a0f10e429708c14e4695b00",
        )

    def test_yunzhi_canonical_headers(self):
        """排除表 + 键小写 + 排序;浏览器自动头(referer/sec-ch-ua)不影响签名."""
        headers = {
            "Authorization": "T", "Content-Type": "application/json",
            "Referer": "https://yunzhi.play.cn/", "sec-ch-ua-platform": '"macOS"',
            "b-key": "2", "a-key": "1",
        }
        self.assertEqual(
            self.m._yunzhi_canonical_headers(headers),
            "a-key=1&authorization=T&b-key=2&referer=https://yunzhi.play.cn/&sec-ch-ua-platform=\"macOS\"",
        )

    def _fake_page(self, responses, state):
        """FakePage: evaluate 按 creds/fetch 分发;fetch 按 method+path 路由."""
        m = self.m

        class FakePage:
            url = "https://yunzhi.play.cn/ai/?channel_code=00000042"

            async def goto(self, *a, **k):
                pass

            async def evaluate(self, js, payload=None):
                if "cloud_phone_token" in js:
                    return {"token": "TEST_TOKEN_FAKE_123", "deviceNo": "a3eef24f4e96d698"}
                # fetch 分发(注意:此处 self 是 FakePage,模块引用走闭包 m)
                method = payload["method"]
                path = payload["url"].replace(m.YUNZI_API_BASE, "")
                handler = responses.get((method, path))
                if handler is None:
                    return {"_http_status": 404, "_authorization": "", "_data": None}
                out = handler(state)
                if isinstance(out, tuple):
                    return out[0](state)
                return out

        return FakePage()

    def test_yunzhi_already_flow(self):
        """已领态: init popups=[] 且 userItems=[] → ALREADY."""
        m = self.m
        responses = {
            ("GET", "/api/content/home-popups/init"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {"channelCode": "00000042", "popups": []}},
            },
            ("POST", "/api/benefit/user/benefit"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {
                    "benefitConfigId": 158, "remainingQuota": 0, "matchType": "CLOUD_DEVICE",
                    "userItems": [], "cloudDevices": [
                        {"flavor": "4C8G128GB", "vendorResourceId": "D0026092223823038",
                         "status": 2, "expireTime": "2026-10-05 06:25:39"},
                    ],
                }},
            },
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, {}), self._adapter()))
        self.assertEqual(res.status, "ALREADY")
        self.assertIn("无待开通权益卡", res.detail)
        self.assertEqual(res.transition, "NONE")

    def test_yunzhi_fresh_flow_returns_ok(self):
        """未领态: 弹窗领取 → userItems 1 张卡 → claim → status 轮询至 1 → OK."""
        m = self.m
        state = {"claimed": False, "status_calls": 0}

        def popup_init(s):
            popups = [] if s["claimed"] else [
                {"popupId": 46, "popupName": "今日登录福利", "benefitType": 1,
                 "state": "CAN_CLAIM", "canClaim": True},
            ]
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success",
                              "data": {"channelCode": "00000042", "popups": popups}}}

        def popup_claim(s):
            s["claimed"] = True
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success", "data": {
                        "success": True, "claimed": True,
                        "claimNo": "CLM17907948158771499554", "benefitType": 1}}}

        def benefit(s):
            items = [] if not s["claimed"] else [{"userItemId": 355236345335936}]
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success", "data": {
                        "benefitConfigId": 158, "matchType": "CLOUD_DEVICE",
                        "remainingQuota": 1 if items else 0,
                        "userItems": items,
                        "cloudDevices": [
                            {"flavor": "4C8G128GB", "vendorResourceId": "D0026092223823038",
                             "status": 2, "expireTime": "2026-10-05 06:25:39"},
                        ]}}}

        def claim(s):
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success",
                              "data": {"claimId": 2720635, "status": 0, "errorMsg": None}}}

        def claim_status(s):
            s["status_calls"] += 1
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success",
                              "data": {"claimId": 2720635, "status": 1, "errorMsg": None}}}

        responses = {
            ("GET", "/api/content/home-popups/init"): popup_init,
            ("POST", "/api/content/home-popups/46/claim"): popup_claim,
            ("POST", "/api/benefit/user/benefit"): benefit,
            ("POST", "/api/benefit/claim"): claim,
            ("POST", "/api/benefit/claim/status"): claim_status,
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        async def fake_sleep(*a, **k):
            return None

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass), \
             mock.patch.object(m.asyncio, "sleep", fake_sleep):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, state), self._adapter()))
        self.assertEqual(res.status, "OK")
        self.assertIn("弹窗46福利卡领取成功", res.detail)
        self.assertIn("D0026092223823038", res.detail)
        self.assertEqual(res.transition, "PENDING_TO_DONE")
        self.assertEqual(state["status_calls"], 1)

    def test_yunzhi_async_ingestion_transient_error_retry_success(self):
        """弹窗刚领完时，服务端异步入账暂态报错「权益不存在或已过期」，重试自愈成功开通."""
        m = self.m
        state = {"claimed": False, "status_calls": 0, "benefit_calls": 0}

        def popup_init(s):
            popups = [] if s["claimed"] else [
                {"popupId": 46, "popupName": "今日登录福利", "benefitType": 1,
                 "state": "CAN_CLAIM", "canClaim": True},
            ]
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success",
                              "data": {"channelCode": "00000042", "popups": popups}}}

        def popup_claim(s):
            s["claimed"] = True
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success", "data": {
                        "success": True, "claimed": True,
                        "claimNo": "CLM17907948158771499554", "benefitType": 1}}}

        def benefit(s):
            s["benefit_calls"] += 1
            # 模拟第 1 次查询因 MQ 延迟返回「权益不存在或已过期」
            if s["benefit_calls"] == 1:
                return {"_http_status": 200, "_authorization": "",
                        "_data": {"code": 10001, "message": "权益不存在或已过期", "data": None}}
            items = [{"userItemId": 355236345335936}]
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success", "data": {
                        "benefitConfigId": 158, "matchType": "CLOUD_DEVICE",
                        "remainingQuota": 1,
                        "userItems": items,
                        "cloudDevices": [
                            {"flavor": "4C8G128GB", "vendorResourceId": "D0026092223823038",
                             "status": 2, "expireTime": "2026-10-05 06:25:39"},
                        ]}}}

        def claim(s):
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success",
                              "data": {"claimId": 2720635, "status": 0, "errorMsg": None}}}

        def claim_status(s):
            s["status_calls"] += 1
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success",
                              "data": {"claimId": 2720635, "status": 1, "errorMsg": None}}}

        responses = {
            ("GET", "/api/content/home-popups/init"): popup_init,
            ("POST", "/api/content/home-popups/46/claim"): popup_claim,
            ("POST", "/api/benefit/user/benefit"): benefit,
            ("POST", "/api/benefit/claim"): claim,
            ("POST", "/api/benefit/claim/status"): claim_status,
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        async def fake_sleep(*a, **k):
            return None

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass), \
             mock.patch.object(m.asyncio, "sleep", fake_sleep):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, state), self._adapter()))
        self.assertEqual(res.status, "OK")
        self.assertIn("弹窗46福利卡领取成功", res.detail)
        self.assertIn("D0026092223823038", res.detail)
        self.assertEqual(res.transition, "PENDING_TO_DONE")
        self.assertGreaterEqual(state["benefit_calls"], 2)

    def test_yunzhi_async_ingestion_empty_user_items_retry_success(self):
        """弹窗刚领完时，第1次查询userItems为空列表，重试后返回卡片成功开通."""
        m = self.m
        state = {"claimed": False, "status_calls": 0, "benefit_calls": 0}

        def popup_init(s):
            popups = [] if s["claimed"] else [
                {"popupId": 46, "popupName": "今日登录福利", "benefitType": 1,
                 "state": "CAN_CLAIM", "canClaim": True},
            ]
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success",
                              "data": {"channelCode": "00000042", "popups": popups}}}

        def popup_claim(s):
            s["claimed"] = True
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success", "data": {
                        "success": True, "claimed": True,
                        "claimNo": "CLM17907948158771499554", "benefitType": 1}}}

        def benefit(s):
            s["benefit_calls"] += 1
            items = [] if s["benefit_calls"] == 1 else [{"userItemId": 355236345335936}]
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success", "data": {
                        "benefitConfigId": 158, "matchType": "CLOUD_DEVICE",
                        "remainingQuota": 1,
                        "userItems": items,
                        "cloudDevices": [
                            {"flavor": "4C8G128GB", "vendorResourceId": "D0026092223823038",
                             "status": 2, "expireTime": "2026-10-05 06:25:39"},
                        ]}}}

        def claim(s):
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success",
                              "data": {"claimId": 2720635, "status": 1, "errorMsg": None}}}

        responses = {
            ("GET", "/api/content/home-popups/init"): popup_init,
            ("POST", "/api/content/home-popups/46/claim"): popup_claim,
            ("POST", "/api/benefit/user/benefit"): benefit,
            ("POST", "/api/benefit/claim"): claim,
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        async def fake_sleep(*a, **k):
            return None

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass), \
             mock.patch.object(m.asyncio, "sleep", fake_sleep):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, state), self._adapter()))
        self.assertEqual(res.status, "OK")
        self.assertIn("弹窗46福利卡领取成功", res.detail)
        self.assertIn("D0026092223823038", res.detail)
        self.assertGreaterEqual(state["benefit_calls"], 2)

    def test_yunzhi_async_ingestion_exhausted_preserves_popup_note(self):
        """4次重试均报暂态错误耗尽时，失败detail必须保留弹窗已领取的业务上下文."""
        m = self.m
        state = {"claimed": False, "benefit_calls": 0}

        def popup_init(s):
            popups = [] if s["claimed"] else [
                {"popupId": 46, "popupName": "今日登录福利", "benefitType": 1,
                 "state": "CAN_CLAIM", "canClaim": True},
            ]
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success",
                              "data": {"channelCode": "00000042", "popups": popups}}}

        def popup_claim(s):
            s["claimed"] = True
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success", "data": {
                        "success": True, "claimed": True,
                        "claimNo": "CLM17907948158771499554", "benefitType": 1}}}

        def benefit(s):
            s["benefit_calls"] += 1
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 10001, "message": "权益不存在或已过期", "data": None}}

        responses = {
            ("GET", "/api/content/home-popups/init"): popup_init,
            ("POST", "/api/content/home-popups/46/claim"): popup_claim,
            ("POST", "/api/benefit/user/benefit"): benefit,
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        async def fake_sleep(*a, **k):
            return None

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass), \
             mock.patch.object(m.asyncio, "sleep", fake_sleep):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, state), self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "error")
        self.assertIn("弹窗46福利卡领取成功", res.detail)
        self.assertIn("权益不存在或已过期", res.detail)
        self.assertEqual(state["benefit_calls"], 4)

    def test_yunzhi_postcheck_matches_target_device_expire_time(self):
        """多云机账号二次复查时，必须读取本次绑定的目标云机有效期，禁止错取第1台历史过期设备."""
        m = self.m
        state = {"claimed": True}

        def popup_init(s):
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success", "data": {"popups": []}}}

        def benefit(s):
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success", "data": {
                        "benefitConfigId": 158, "matchType": "CLOUD_DEVICE",
                        "remainingQuota": 1,
                        "userItems": [{"userItemId": 355236345335936}],
                        "cloudDevices": [
                            {"flavor": "2C4G", "vendorResourceId": "OLD_EXPIRED_DEV",
                             "status": 4, "expireTime": "2024-01-01 00:00:00"},
                            {"flavor": "4C8G128GB", "vendorResourceId": "D0026092223823038",
                             "status": 2, "expireTime": "2026-11-21 06:25:39"},
                        ]}}}

        def claim(s):
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 0, "message": "success",
                              "data": {"claimId": 2720635, "status": 1, "errorMsg": None}}}

        responses = {
            ("GET", "/api/content/home-popups/init"): popup_init,
            ("POST", "/api/benefit/user/benefit"): benefit,
            ("POST", "/api/benefit/claim"): claim,
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        async def fake_sleep(*a, **k):
            return None

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass), \
             mock.patch.object(m.asyncio, "sleep", fake_sleep):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, state), self._adapter()))
        self.assertEqual(res.status, "OK")
        self.assertIn("D0026092223823038", res.detail)
        self.assertIn("有效期至 2026-11-21 06:25:39", res.detail)
        self.assertNotIn("2024-01-01", res.detail)


    def test_yunzhi_evaluate_exception_fails_not_already(self):
        """P1 回归(2026-10-01 review):页面内 fetch 异常必须 FAIL error,禁止穿透成假 ALREADY."""
        m = self.m
        state = {"raise": True}

        class DeadPage:
            url = "https://yunzhi.play.cn/ai/?channel_code=00000042"

            async def goto(self, *a, **k):
                pass

            async def evaluate(self, js, payload=None):
                if "cloud_phone_token" in js:
                    return {"token": "TEST_TOKEN_FAKE_123", "deviceNo": "a3eef24f4e96d698"}
                if state["raise"]:
                    raise RuntimeError("Execution context was destroyed")
                return {"_http_status": 200, "_authorization": "", "_data": None}

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass):
            res = self.asyncio.run(m.yunzhi_checkin(DeadPage(), self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("页面内 fetch 异常", res.detail)
        self.assertIn("Execution context was destroyed", res.detail)

    def test_yunzhi_anomaly_shape_fails_not_already(self):
        """异常形状(userItems 空但 remainingQuota 缺失)必须 FAIL,不得假 ALREADY."""
        m = self.m
        responses = {
            ("GET", "/api/content/home-popups/init"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {"channelCode": "00000042", "popups": []}},
            },
            ("POST", "/api/benefit/user/benefit"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {"benefitConfigId": 158}},
            },
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, {}), self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("状态异常", res.detail)

    def test_yunzhi_waf_503_fails_loud(self):
        """WAF 503(HTTP 非 200)必须显式 FAIL,不得静默穿透."""
        m = self.m
        responses = {
            ("GET", "/api/content/home-popups/init"): lambda s: {
                "_http_status": 503, "_authorization": "", "_data": None,
            },
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, {}), self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("HTTP 503", res.detail)


    def test_yunzhi_popup_claim_failure_fails_not_already(self):
        """弹窗领取失败时不得判 ALREADY(claimable 非空 → 显式 FAIL)."""
        m = self.m
        responses = {
            ("GET", "/api/content/home-popups/init"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {"channelCode": "00000042", "popups": [
                    {"popupId": 46, "popupName": "今日登录福利", "benefitType": 1,
                     "state": "CAN_CLAIM", "canClaim": True},
                ]}},
            },
            ("POST", "/api/content/home-popups/46/claim"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 500, "message": "系统异常", "data": None},
            },
            ("POST", "/api/benefit/user/benefit"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {
                    "benefitConfigId": 158, "remainingQuota": 0, "matchType": "CLOUD_DEVICE",
                    "userItems": [], "cloudDevices": []}},
            },
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, {}), self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("状态异常", res.detail)
        self.assertIn("系统异常", res.detail)

    def test_yunzhi_claim_already_message_returns_ok(self):
        """claim 返回「已领取」语义时按已开通计 OK(幂等重复执行保护)."""
        m = self.m
        responses = {
            ("GET", "/api/content/home-popups/init"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {"channelCode": "00000042", "popups": []}},
            },
            ("POST", "/api/benefit/user/benefit"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {
                    "benefitConfigId": 158, "remainingQuota": 1, "matchType": "CLOUD_DEVICE",
                    "userItems": [{"userItemId": 355236345335936}],
                    "cloudDevices": [
                        {"flavor": "4C8G128GB", "vendorResourceId": "D0026092223823038",
                         "status": 2, "expireTime": "2026-10-05 06:25:39"},
                    ]}},
            },
            ("POST", "/api/benefit/claim"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 500, "message": "今日已领取过该权益", "data": None},
            },
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, {}), self._adapter()))
        self.assertEqual(res.status, "OK")
        self.assertIn("已开通", res.detail)

    def test_yunzhi_no_running_device_fails(self):
        """有待开通卡但无运行中云机时必须 FAIL,不得盲选或假成功."""
        m = self.m
        responses = {
            ("GET", "/api/content/home-popups/init"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {"channelCode": "00000042", "popups": []}},
            },
            ("POST", "/api/benefit/user/benefit"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {
                    "benefitConfigId": 158, "remainingQuota": 1, "matchType": "CLOUD_DEVICE",
                    "userItems": [{"userItemId": 355236345335936}],
                    "cloudDevices": []}},
            },
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, {}), self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("无可用云机实例", res.detail)


    def test_yunzhi_claim_poll_timeout_fails(self):
        """claim 受理后轮询 claim/status 一直不出终态时,轮询耗尽必须 FAIL."""
        m = self.m
        responses = {
            ("GET", "/api/content/home-popups/init"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {"channelCode": "00000042", "popups": []}},
            },
            ("POST", "/api/benefit/user/benefit"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {
                    "benefitConfigId": 158, "remainingQuota": 1, "matchType": "CLOUD_DEVICE",
                    "userItems": [{"userItemId": 355236345335936}],
                    "cloudDevices": [
                        {"flavor": "4C8G128GB", "vendorResourceId": "D0026092223823038",
                         "status": 2, "expireTime": "2026-10-05 06:25:39"},
                    ]}},
            },
            ("POST", "/api/benefit/claim"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success",
                          "data": {"claimId": 2720635, "status": 0, "errorMsg": None}},
            },
            ("POST", "/api/benefit/claim/status"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success",
                          "data": {"claimId": 2720635, "status": 0, "errorMsg": None}},
            },
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        async def fake_sleep(*a, **k):
            return None

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass), \
             mock.patch.object(m.asyncio, "sleep", fake_sleep):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, {}), self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertIn("轮询超时", res.detail)

    def test_yunzhi_multi_card_partial_failure_surfaced(self):
        """多卡部分成功部分失败时,OK detail 必须透出失败部分(不得静默吞掉)."""
        m = self.m
        calls = {"claim": 0}

        def claim(s):
            calls["claim"] += 1
            if calls["claim"] == 1:
                return {"_http_status": 200, "_authorization": "",
                        "_data": {"code": 0, "message": "success",
                                  "data": {"claimId": 2720635, "status": 1, "errorMsg": None}}}
            return {"_http_status": 200, "_authorization": "",
                    "_data": {"code": 500, "message": "开通排队超时", "data": None}}

        responses = {
            ("GET", "/api/content/home-popups/init"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {"channelCode": "00000042", "popups": []}},
            },
            ("POST", "/api/benefit/user/benefit"): lambda s: {
                "_http_status": 200, "_authorization": "",
                "_data": {"code": 0, "message": "success", "data": {
                    "benefitConfigId": 158, "remainingQuota": 2, "matchType": "CLOUD_DEVICE",
                    "userItems": [{"userItemId": 111}, {"userItemId": 222}],
                    "cloudDevices": [
                        {"flavor": "4C8G128GB", "vendorResourceId": "D0026092223823038",
                         "status": 2, "expireTime": "2026-10-05 06:25:39"},
                    ]}},
            },
            ("POST", "/api/benefit/claim"): claim,
        }

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        async def fake_sleep(*a, **k):
            return None

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass), \
             mock.patch.object(m.asyncio, "sleep", fake_sleep):
            res = self.asyncio.run(m.yunzhi_checkin(self._fake_page(responses, {}), self._adapter()))
        self.assertEqual(res.status, "OK")
        self.assertIn("卡111→D0026092223823038", res.detail)
        self.assertIn("部分失败", res.detail)
        self.assertIn("开通排队超时", res.detail)

    def test_yunzhi_no_token_returns_auth_required(self):
        """9222 profile 无登录态时必须 auth_required(引导用户浏览器登录)."""
        m = self.m

        class NoLoginPage:
            url = "https://yunzhi.play.cn/ai/?channel_code=00000042"

            async def goto(self, *a, **k):
                pass

            async def evaluate(self, js, payload=None):
                if "cloud_phone_token" in js:
                    return {"token": "", "deviceNo": ""}
                return {"_http_status": 200, "_authorization": "", "_data": None}

        async def fake_url(page):
            return "https://yunzhi.play.cn/ai/?channel_code=00000042"

        async def fake_bypass(page):
            return False

        async def fake_sleep(*a, **k):
            return None

        with mock.patch.object(m, "page_url", fake_url), \
             mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass), \
             mock.patch.object(m.asyncio, "sleep", fake_sleep):
            res = self.asyncio.run(m.yunzhi_checkin(NoLoginPage(), self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "auth_required")
        self.assertIn("cloud_phone_token", res.detail)

    def test_yunzhi_win_in_yaml_and_builtin(self):
        import yaml
        yaml_path = ROOT / "sites.yaml"
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "yunzhi_win"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 yunzhi_win 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "yunzhi")
        self.assertEqual(a.url, "https://yunzhi.play.cn/ai/?channel_code=00000042")
        self.assertTrue(a.prefer_catalog_url)
        b = next((x for x in _BUILTIN_SITE_ADAPTERS if x.name == "yunzhi_win"), None)
        self.assertIsNotNone(b, "内置 adapter 应包含 yunzhi_win")
        self.assertEqual(b.kind, "yunzhi")
        for k in vars(b):
            self.assertEqual(getattr(a, k), getattr(b, k), f"yunzhi_win 字段 {k} 不等价")

    def test_yunzhi_load_external_token(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # direct candidate patch
            with mock.patch.object(self.m.Path, "home", return_value=Path(tmpdir)):
                # not found
                tok, dev, p = self.m._yunzhi_load_external_token("nonexistent_site")
                self.assertEqual(tok, "")
                self.assertIsNone(p)

                # path traversal rejection
                tok, dev, p = self.m._yunzhi_load_external_token("../../etc/passwd")
                self.assertEqual(tok, "")

                # Put under ~/.config/daily-checkin/credentials/
                target_dir = Path(tmpdir) / ".config" / "daily-checkin" / "credentials"
                target_dir.mkdir(parents=True, exist_ok=True)
                target_file = target_dir / "yunzhi_win_test.json"
                target_file.write_text(json.dumps({
                    "token": "win_tok_999",
                    "deviceNo": "win_dev_888",
                }), encoding="utf-8")
                # 测试过于宽松的权限自动收紧为 0600
                target_file.chmod(0o666)
                tok, dev, p = self.m._yunzhi_load_external_token("yunzhi_win_test")
                self.assertEqual(tok, "win_tok_999")
                self.assertEqual(dev, "win_dev_888")
                self.assertEqual(p, target_file)
                self.assertEqual(oct(target_file.stat().st_mode & 0o777), "0o600")

    def test_yunzhi_save_external_token(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            target_file = Path(tmpdir) / "yunzhi_save_test.json"
            target_file.write_text(json.dumps({
                "token": "old_token",
                "deviceNo": "dev_123",
            }), encoding="utf-8")

            # rotation update (原子写盘)
            self.m._yunzhi_save_external_token(target_file, "new_rotated_token")
            data = json.loads(target_file.read_text(encoding="utf-8"))
            self.assertEqual(data["token"], "new_rotated_token")
            self.assertIn("updated_at", data)
            self.assertEqual(oct(target_file.stat().st_mode & 0o777), "0o600")

    def test_yunzhi_win_flow_with_external_token_and_auto_rotation(self):
        """外部 Token 驱动: 不查 localStorage, 响应带新 token 时触发自动回写持久化."""
        m = self.m
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            target_file = Path(tmpdir) / "yunzhi_win.json"
            target_file.write_text(json.dumps({
                "token": "initial_ext_token",
                "deviceNo": "ext_dev_001",
            }), encoding="utf-8")

            evaluated_creds_called = []

            class ExternalTokenPage:
                url = "https://yunzhi.play.cn/ai/?channel_code=00000042"

                async def goto(self, *a, **k):
                    pass

                async def evaluate(self, js, payload=None):
                    if "cloud_phone_token" in js:
                        evaluated_creds_called.append(True)
                        return {"token": "WRONG_ACCOUNT_1_TOKEN", "deviceNo": "wrong_dev"}
                    method = payload["method"]
                    path = payload["url"].replace(m.YUNZI_API_BASE, "")
                    if (method, path) == ("GET", "/api/content/home-popups/init"):
                        # 模拟服务端下发轮换的新 token
                        return {
                            "_http_status": 200,
                            "_authorization": "brand_new_rotated_token_xyz",
                            "_data": {"code": 0, "message": "success", "data": {"channelCode": "00000042", "popups": []}},
                        }
                    if (method, path) == ("POST", "/api/benefit/user/benefit"):
                        return {
                            "_http_status": 200,
                            "_authorization": "",
                            "_data": {"code": 0, "message": "success", "data": {
                                "benefitConfigId": 158, "remainingQuota": 0, "matchType": "CLOUD_DEVICE",
                                "userItems": [], "cloudDevices": [
                                    {"flavor": "4C8G128GB", "vendorResourceId": "D0026092223823038",
                                     "status": 2, "expireTime": "2026-10-05 06:25:39"},
                                ],
                            }},
                        }
                    return {"_http_status": 404, "_authorization": "", "_data": None}

            adapter = m.SiteAdapter(
                name="yunzhi_win",
                url="https://yunzhi.play.cn/ai/?channel_code=00000042",
                kind="yunzhi",
                sign_selectors=['text=开心收下', 'text=立即领取'],
                already_selectors=['text=今日已领取', 'text=明天再来'],
                prefer_catalog_url=True,
                login_url="https://yunzhi.play.cn/ai/#/login",
            )

            async def fake_url(*a, **k):
                return "https://yunzhi.play.cn/ai/?channel_code=00000042"

            async def fake_bypass(*a, **k):
                return False

            with mock.patch.object(m, "_yunzhi_load_external_token", return_value=("initial_ext_token", "ext_dev_001", target_file)), \
                 mock.patch.object(m, "page_url", fake_url), \
                 mock.patch.object(m, "bypass_chrome_interstitial_if_needed", fake_bypass):
                res = self.asyncio.run(m.yunzhi_checkin(ExternalTokenPage(), adapter))

            self.assertEqual(res.status, "ALREADY")
            # 确信未读取 9222 的 localStorage (账号隔离保障)
            self.assertEqual(len(evaluated_creds_called), 0, "使用外部 Token 时不应轮询页面 localStorage")
            # 确信自动写回了新 token (自动续期保障)
            saved_data = json.loads(target_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_data["token"], "brand_new_rotated_token_xyz")


class TestNailaoCheckin(unittest.TestCase):
    """nailao (nailao.biz) 2026-10-27 改版后的专用流程.

    会话是 SPA 内存里的 Bearer,整页跳转即丢;UI 无签到 CTA,
    真入口是同页 fetch POST /api/user/checkin。token 不得进日志或结果。
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio
        # 桩必须随用例结束还原：直接赋值会残留在共享 runner 模块对象上，
        # xdist 同 worker 后续缓存加载的用例（如 sites.yaml 注册表校验）会吃到
        # 假凭据（2026-11-07 全量并行回归实测闪失的根因）。
        stub = mock.patch.object(
            self.m, "get_account_credential",
            lambda name: ("user", "secret-token-value"),
        )
        stub.start()
        self.addCleanup(stub.stop)

    def _adapter(self):
        return self.m.SiteAdapter(
            name="nailao",
            url="https://nailao.biz/sign-in",
            kind="nailao",
            prefer_catalog_url=True,
            login_url="https://nailao.biz/sign-in",
        )

    def _page(self, api_body):
        """已登录页:无密码框。on('request') 时回放一条带 Bearer 的 /api/user 请求。"""
        seen = {"token": None}

        class FakeLocator:
            def __init__(self, n):
                self._n = n
            @property
            def first(self):
                return self
            async def count(self):
                return self._n

        class FakePage:
            url = "https://nailao.biz/dashboard/overview"

            async def goto(self, *a, **k):
                pass

            def locator(self, sel):
                return FakeLocator(0)

            def on(self, event, handler):
                # 流程用 ensure_future 挂监听,假事件循环不会在 sleep 前跑它。
                # 按闭包变量名找到 bearer_box 再写入,不依赖单元格顺序。
                fn = getattr(handler, "__wrapped__", None) or handler
                # lambda req: ensure_future(_capture_bearer(req)) —— token 在内层函数里。
                inner = None
                for cell in fn.__closure__ or ():
                    val = cell.cell_contents
                    if callable(val) and "bearer_box" in (val.__code__.co_freevars or ()):
                        inner = val
                        break
                names = inner.__code__.co_freevars
                box = inner.__closure__[names.index("bearer_box")].cell_contents
                box["token"] = "secret-token-value"

            async def evaluate(self, expr, *args):
                if args:
                    seen["token"] = args[0]
                return api_body

        return FakePage(), seen

    def test_p0_no_cookie_clear_and_token_not_logged(self):
        """只复用 9222 会话:不清 Cookie / localStorage,日志也不打 token。"""
        import inspect
        import re
        src = inspect.getsource(self.m.nailao_checkin)
        code = re.sub(r'"""[\s\S]*?"""', "", src)
        code = "\n".join(
            line for line in code.splitlines() if not line.lstrip().startswith("#")
        ).lower()
        for banned in (
            "clear_cookies(",
            "clear_localstorage",
            "clear_all(",
            "localstorage.clear",
            "document.cookie",
            "deleteallcookies",
        ):
            self.assertNotIn(banned, code)
        for line in src.splitlines():
            if "print(" in line:
                self.assertNotIn("bearer_box", line)
                self.assertNotIn("{token", line)

    def test_dispatch_routes_nailao_kind(self):
        import inspect
        src = inspect.getsource(self.m.legacy_checkin_on_page)
        self.assertIn('kind == "nailao"', src)
        self.assertIn("nailao_checkin", src)
        self.assertLess(src.find('kind == "nailao"'), src.find('kind == "xmiaom"'))

    def test_missing_credential_fails_closed(self):
        # setUp 已打桩为有效凭据；本用例覆盖「凭据缺失」分支，作用域限于本用例
        with mock.patch.object(self.m, "get_account_credential", lambda name: None):
            page, seen = self._page({"status": 200, "success": True, "message": "", "quota_awarded": 1})
            res = self.asyncio.run(self.m.nailao_checkin(page, self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "auth_required")
        self.assertIn("no registered credential", res.detail)
        self.assertIsNone(seen["token"])

    def test_success_maps_ok_without_echoing_token(self):
        page, seen = self._page(
            {"status": 200, "success": True, "message": "ok", "quota_awarded": 7}
        )
        res = self.asyncio.run(self.m.nailao_checkin(page, self._adapter()))
        self.assertEqual(res.status, "OK")
        self.assertIn("+7", res.detail)
        self.assertEqual(res.action.kind, "api_post")
        self.assertEqual(res.action.target, "/api/user/checkin")
        self.assertEqual(seen["token"], "secret-token-value")
        self.assertNotIn("secret-token-value", res.detail)
        self.assertNotIn("secret-token-value", res.msg())

    def test_already_message_maps_already(self):
        page, _seen = self._page(
            {"status": 200, "success": False, "message": "今日已签到", "quota_awarded": None}
        )
        res = self.asyncio.run(self.m.nailao_checkin(page, self._adapter()))
        self.assertEqual(res.status, "ALREADY")
        self.assertNotIn("secret-token-value", res.detail)

    def test_unauthorized_maps_auth_required(self):
        page, _seen = self._page(
            {"status": 401, "success": False, "message": "unauthorized", "quota_awarded": None}
        )
        res = self.asyncio.run(self.m.nailao_checkin(page, self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "auth_required")
        self.assertNotIn("secret-token-value", res.detail)

    def test_other_failure_maps_no_grant(self):
        page, _seen = self._page(
            {"status": 500, "success": False, "message": "upstream", "quota_awarded": None}
        )
        res = self.asyncio.run(self.m.nailao_checkin(page, self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_grant")


class TestHowtofixCheckin(unittest.TestCase):
    """howtofix (howtofix.id) 2026-11-14 接入的专用 CLI 协议流程.

    网页签到被服务端禁用(CLI only),唯一通道是浏览器上下文内
    POST api.howtofix.id/v1/cli/checkin(Bearer sk- key + hwid 设备头)。
    关键红线:sk- key 只经 evaluate 参数通道,不落日志/结果。
    """

    def setUp(self):
        self.m = load_mod(force=True)
        import asyncio as _aio
        self.asyncio = _aio
        stub = mock.patch.object(
            self.m, "get_account_credential",
            lambda name: ("ci-account-howtofix", "sk-ci-key-howtofix-000000000000"),
        )
        stub.start()
        self.addCleanup(stub.stop)

    def _adapter(self):
        return self.m.SiteAdapter(
            name="howtofix",
            url="https://howtofix.id/profile",
            kind="howtofix",
            prefer_catalog_url=True,
        )

    def _page(self, evaluate_return=None, evaluate_exc=None):
        """假页:evaluate 直接回放 JS 桥返回值(模拟网关各 stage)。"""
        got = {"args": None, "goto_urls": []}

        class FakePage:
            async def goto(self, url, *a, **k):
                got["goto_urls"].append(url)

            async def evaluate(self, expr, *args):
                got["args"] = args
                if evaluate_exc is not None:
                    raise evaluate_exc
                return evaluate_return

        return FakePage(), got

    def _device(self):
        stub = mock.patch.object(
            self.m, "_howtofix_device_id", return_value="dev-fp-32chars-ok"
        )
        stub.start()
        self.addCleanup(stub.stop)
        return "dev-fp-32chars-ok"

    def test_no_sk_key_fails_closed(self):
        """凭据缺失/非 sk- 前缀 → auth_required,不发任何网关请求。"""
        with mock.patch.object(self.m, "get_account_credential", lambda name: None):
            page, got = self._page({"stage": "claimed"})
            res = self.asyncio.run(self.m.howtofix_checkin(page, self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "auth_required")
        self.assertEqual(res.stage, "auth")
        self.assertIsNone(got["args"])

    def test_success_maps_ok_with_reward(self):
        did = self._device()
        page, got = self._page({"stage": "claimed", "quota_awarded": 2500000})
        res = self.asyncio.run(self.m.howtofix_checkin(page, self._adapter()))
        self.assertEqual(res.status, "OK")
        self.assertIn("+$5.00", res.detail)
        self.assertEqual(res.action.kind, "api_post")
        self.assertEqual(res.action.target, "/v1/cli/checkin")
        self.assertEqual(res.transition, "PENDING_TO_DONE")
        # key/device 只经 evaluate 参数通道
        self.assertEqual(got["args"][0][0], "sk-ci-key-howtofix-000000000000")
        self.assertEqual(got["args"][0][1], did)
        self.assertNotIn("sk-ci-key-howtofix", res.detail)
        self.assertNotIn("sk-ci-key-howtofix", res.msg())

    def test_already_checked_in_maps_already(self):
        self._device()
        page, _got = self._page(
            {"stage": "already", "quota_today": 2500000}
        )
        res = self.asyncio.run(self.m.howtofix_checkin(page, self._adapter()))
        self.assertEqual(res.status, "ALREADY")
        self.assertIn("+$5.00", res.detail)
        self.assertNotIn("sk-ci-key-howtofix", res.detail)

    def test_gateway_rejects_key_maps_auth_required(self):
        self._device()
        page, _got = self._page({"stage": "auth", "status": 401})
        res = self.asyncio.run(self.m.howtofix_checkin(page, self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "auth_required")
        self.assertNotIn("sk-ci-key-howtofix", res.detail)

    def test_disabled_maps_no_grant(self):
        self._device()
        page, _got = self._page({"stage": "disabled"})
        res = self.asyncio.run(self.m.howtofix_checkin(page, self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_grant")

    def test_gateway_error_message_is_not_already(self):
        """P1 根修回归:维护类报错(含「签到」字样)不得误判 ALREADY。"""
        self._device()
        page, _got = self._page(
            {"stage": "error", "status": 503, "message": "checkin temporarily under maintenance"}
        )
        res = self.asyncio.run(self.m.howtofix_checkin(page, self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "no_grant")

    def test_js_exception_maps_upstream_unavailable(self):
        self._device()
        page, _got = self._page(evaluate_exc=RuntimeError("evaluate failed"))
        res = self.asyncio.run(self.m.howtofix_checkin(page, self._adapter()))
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.reason, "upstream_unavailable")

    def test_already_regexp_is_strict(self):
        """源码守卫:POST 失败兜底正则只认实测已签语义,禁止宽中文匹配。"""
        import inspect
        src = inspect.getsource(self.m.howtofix_checkin)
        self.assertIn("once per day", src)
        # 已移除「签到|今日」宽匹配(zerocat/huaweicloud 假 ALREADY 同款坑)
        self.assertNotIn("/already|claimed|签到|今日|once per day/i", src)
        # goto 必须用 adapter.url(非硬编码,防 URL 漂移失效)
        self.assertIn("adapter.url", src)
        self.assertNotIn('goto("https://howtofix.id/profile"', src)

    def test_p0_no_cookie_clear_and_key_not_logged(self):
        import inspect
        import re
        src = inspect.getsource(self.m.howtofix_checkin)
        code = re.sub(r'"""[\s\S]*?"""', "", src)
        code = "\n".join(
            line for line in code.splitlines() if not line.lstrip().startswith("#")
        ).lower()
        for banned in (
            "clear_cookies(",
            "clear_localstorage",
            "clear_all(",
            "localstorage.clear",
            "document.cookie",
            "deleteallcookies",
        ):
            self.assertNotIn(banned, code)
        for line in src.splitlines():
            if "print(" in line:
                self.assertNotIn("api_key", line)
                self.assertNotIn("{key", line)

    def test_dispatch_routes_howtofix(self):
        import inspect
        src = inspect.getsource(self.m.legacy_checkin_on_page)
        self.assertIn('kind == "howtofix"', src)
        self.assertIn("howtofix_checkin", src)

    def test_device_id_shape_and_stability(self):
        """指纹是 32 hex 且同机两次调用一致(服务端按 device 去重)。"""
        d1 = self.m._howtofix_device_id()
        d2 = self.m._howtofix_device_id()
        self.assertEqual(d1, d2)
        self.assertEqual(len(d1), 32)
        int(d1, 16)  # 纯 hex


class TestHuaweicloud(unittest.TestCase):
    """华为云开发者成长中心 (developer.huaweicloud.com/grow) 测试集."""

    def setUp(self):
        self.m = load_mod()

    def test_yaml_registers_huaweicloud_entry(self):
        """sites.yaml 的 huaweicloud 条目解析为 kind=browser 且带立即签到/已签到回执。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "huaweicloud"), None)
        self.assertIsNotNone(entry, "sites.yaml 应有 huaweicloud 条目")
        from stealth_checkin_runner import _adapter_from_yaml_entry, _BUILTIN_SITE_ADAPTERS
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "browser")
        self.assertEqual(a.url, "https://developer.huaweicloud.com/grow")
        self.assertTrue(a.prefer_catalog_url)
        self.assertEqual(a.login_url, "https://developer.huaweicloud.com/grow")
        self.assertTrue(any("立即签到" in s for s in a.sign_selectors))
        self.assertTrue(any("已签到" in s for s in a.already_selectors))

        # 字段等价 (sites.yaml 与 _BUILTIN_SITE_ADAPTERS 严格同步)
        b = next((x for x in _BUILTIN_SITE_ADAPTERS if x.name == "huaweicloud"), None)
        self.assertIsNotNone(b, "内置 adapter 应包含 huaweicloud")
        self.assertEqual(b.kind, "browser")
        for k in vars(b):
            self.assertEqual(getattr(a, k), getattr(b, k), f"字段 {k} 不等价")

    def test_huaweicloud_predicates_and_resolve(self):
        """is_huaweicloud_site / is_huaweicloud_name 与 resolve_site 判定正确."""
        m = self.m
        self.assertTrue(m.is_huaweicloud_site("https://developer.huaweicloud.com/grow"))
        self.assertTrue(m.is_huaweicloud_site("https://huaweicloud.com/grow?sms"))
        self.assertFalse(m.is_huaweicloud_site("https://example.com/"))

        self.assertTrue(m.is_huaweicloud_name("huaweicloud"))
        self.assertTrue(m.is_huaweicloud_name("华为云"))
        self.assertFalse(m.is_huaweicloud_name("other"))

        resolved = m.resolve_site("huaweicloud", "https://developer.huaweicloud.com/grow")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.name, "huaweicloud")
        self.assertEqual(resolved.kind, "browser")
        self.assertTrue(resolved.prefer_catalog_url)

    def test_huaweicloud_unsigned_page_not_already(self):
        """未签页只渲染常驻的「已连续签到（天）」统计标签, 绝不能被判为 ALREADY.

        2026-10-15 线上事故回归: 旧 already 列表含 'text=已连续签到', 命中常驻标签
        → trusted_already 短路返回 sel:text=已连续签到 → 永不点击「立即签到」。
        """
        import asyncio
        from stealth_checkin_runner import _BUILTIN_SITE_ADAPTERS

        adapter = next(x for x in _BUILTIN_SITE_ADAPTERS if x.name == "huaweicloud")
        self.assertNotIn(
            "text=已连续签到",
            adapter.already_selectors,
            "常驻统计标签『已连续签到（天）』不得作为已签判据",
        )

        # resolve_site 兜底失败路径与 sites.yaml 条目同样不得用该常驻标签(防御纵深化)
        resolved_hw = self.m.resolve_site("huaweicloud", "https://developer.huaweicloud.com/grow")
        self.assertNotIn(
            "text=已连续签到",
            resolved_hw.already_selectors,
            "resolve_site 兜底亦不得含『已连续签到（天）』",
        )
        import yaml
        yaml_path = ROOT / "sites.yaml"
        with open(yaml_path, encoding="utf-8") as fh:
            ydata = yaml.safe_load(fh)
        yentry = next((e for e in ydata.get("sites", []) if e.get("name") == "huaweicloud"), None)
        self.assertIsNotNone(yentry, "sites.yaml 应有 huaweicloud 条目")
        self.assertNotIn(
            "text=已连续签到",
            yentry.get("already", []),
            "sites.yaml huaweicloud already 不得含『已连续签到（天）』",
        )

        class Item:
            def __init__(self, txt, visible=True, disabled=False):
                self.txt, self.visible, self.disabled = txt, visible, disabled

            async def is_visible(self, timeout=0):
                return self.visible

            async def is_disabled(self):
                return self.disabled

            async def inner_text(self, timeout=0):
                return self.txt

        class Loc:
            def __init__(self, items=None):
                self.items = items or []

            async def count(self):
                return len(self.items)

            def nth(self, i):
                return self.items[i] if i < len(self.items) else Item("", visible=False)

            @property
            def first(self):
                return self.items[0] if self.items else Item("", visible=False)

        class UnsignedPage:
            """未签: 有「立即签到」按钮 + 常驻「已连续签到（天）」, 无「已签到」badge."""

            async def evaluate(self, _js):
                return "yd_289657918\n立即签到\n我的积分\n1\n已连续签到（天）"

            def locator(self, sel):
                if sel == "text=已连续签到":
                    return Loc([Item("已连续签到（天）")])
                if sel == "button":
                    return Loc([Item("立即签到")])
                return Loc([])

        sig = asyncio.run(
            self.m.already_done(
                UnsignedPage(), adapter.already_selectors, trusted_already=True
            )
        )
        self.assertEqual(sig, "", f"未签页不得被判为已签, 实际 sig={sig!r}")

        class SignedPage:
            """已签: 「立即签到」按钮消失, 出现 .user-message-view-userValue-item-button「已签到」."""

            async def evaluate(self, _js):
                return "yd_289657918\n已签到\n我的积分\n1\n已连续签到（天）"

            def locator(self, sel):
                if sel == '.user-message-view-userValue-item-button:has-text("已签到")':
                    return Loc([Item("已签到")])
                if sel == "text=已连续签到":
                    return Loc([Item("已连续签到（天）")])
                return Loc([])

        sig_signed = asyncio.run(
            self.m.already_done(
                SignedPage(), adapter.already_selectors, trusted_already=True
            )
        )
        self.assertTrue(sig_signed, "已签页 (badge 已签到) 仍应被识别为已完成")


class TestIsAbntSite(unittest.TestCase):
    """abnt.it(Aether API)尊属适配的站点判定与接线。

    该站是标准 New API 实例,但 LinuxDO OAuth 回跳落在 /sign-in(gate page 停住),
    真实登录态却在另一个带会话的 /profile tab,通用 newapi_profile 流程因此误判
    not logged in → auth_required。登录适配器 abnt_checkin 接管 „„/sign-in → SSO 后
    定位带登录态 /profile → 再点签到。
    """

    def setUp(self):
        self.m = load_mod()

    def test_is_abnt_site_positive_negative(self):
        m = self.m
        self.assertTrue(m.is_abnt_site("https://api.abnt.it/profile"))
        self.assertTrue(m.is_abnt_site("https://api.abnt.it/sign-in"))
        self.assertTrue(m.is_abnt_site("https://abnt.it/"))
        self.assertFalse(m.is_abnt_site("https://demo.dev2.mulink.top/wallet"))
        self.assertFalse(m.is_abnt_site("https://example.com/"))

    def test_resolve_abnt_kind_is_dedicated(self):
        m = self.m
        a = m.resolve_site("abnt", "https://api.abnt.it/profile")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "abnt")

    def test_yaml_registers_abnt_dispatch_kind(self):
        m = self.m
        # sites.yaml 条目存在且 kind=abnt
        from stealth_checkin_runner import SITE_ADAPTERS
        found = [a for a in SITE_ADAPTERS if getattr(a, "name", "") == "abnt"]
        self.assertTrue(found, "abnt 条目应在 sites.yaml 解析的 SITE_ADAPTERS 中")
        self.assertEqual(found[0].kind, "abnt")

    def test_legacy_dispatch_contains_abnt_branch(self):
        src = TARGET.read_text(encoding="utf-8")
        self.assertIn('is_abnt_site(site_url)', src)
        self.assertIn('return await abnt_checkin(page, adapter, browser=browser)', src)

    def test_yaml_registers_abnt_adapter(self):
        """sites.yaml 注册的 abnt 条目可被 _adapter_from_yaml_entry 解析并透传 kind=abnt。"""
        import yaml
        yaml_path = ROOT / "sites.yaml"
        self.assertTrue(yaml_path.exists())
        with open(yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        entry = next((e for e in data.get("sites", []) if e.get("name") == "abnt"), None)
        self.assertIsNotNone(entry)
        from stealth_checkin_runner import _adapter_from_yaml_entry
        a = _adapter_from_yaml_entry(entry)
        self.assertEqual(a.kind, "abnt")

    def test_abnt_checkin_already_authed_skips_sso(self):
        """已登录态下(9222 共享 profile)直接访问 /profile 签到,不调用 try_linuxdo_sso。"""
        import asyncio
        from unittest.mock import AsyncMock, patch

        m = self.m
        fake_page = AsyncMock()
        fake_page.url = "https://api.abnt.it/profile"
        adapter = m.resolve_site("abnt", "https://api.abnt.it/profile")

        with patch.object(m, "_abnt_close_stale_tabs", new_callable=AsyncMock), \
             patch.object(m, "_abnt_goto_profile", new_callable=AsyncMock), \
             patch.object(m, "wait_out_cloudflare", new_callable=AsyncMock, return_value=None), \
             patch.object(m, "dismiss_obstructing_dialogs", new_callable=AsyncMock), \
             patch.object(m, "page_text", new_callable=AsyncMock, return_value="Aether API 用户 ID 38 @mangoqwq 当前余额 Payload 80"), \
             patch.object(m, "try_linuxdo_sso", new_callable=AsyncMock) as mock_sso, \
             patch.object(m, "_abnt_sign_current", new_callable=AsyncMock, return_value=m.confirmed_done_result("abnt check-in clicked", adapter="abnt")):
            res = asyncio.run(m.abnt_checkin(fake_page, adapter))
            self.assertTrue(res.ok)
            mock_sso.assert_not_called()

    def test_abnt_checkin_logged_out_calls_sso(self):
        """未登录态下(/sign-in 登录页)必须触发 try_linuxdo_sso。"""
        import asyncio
        from unittest.mock import AsyncMock, patch

        m = self.m
        fake_page = AsyncMock()
        fake_page.url = "https://api.abnt.it/sign-in"
        adapter = m.resolve_site("abnt", "https://api.abnt.it/profile")

        with patch.object(m, "_abnt_close_stale_tabs", new_callable=AsyncMock), \
             patch.object(m, "_abnt_goto_profile", new_callable=AsyncMock), \
             patch.object(m, "wait_out_cloudflare", new_callable=AsyncMock, return_value=None), \
             patch.object(m, "dismiss_obstructing_dialogs", new_callable=AsyncMock), \
             patch.object(m, "page_text", new_callable=AsyncMock, return_value="Sign in to your account 欢迎回来 使用 LinuxDO 继续"), \
             patch.object(m, "wait_text_ready", new_callable=AsyncMock), \
             patch.object(m, "try_linuxdo_sso", new_callable=AsyncMock, return_value="OK") as mock_sso, \
             patch.object(m, "_abnt_find_authed_profile_tab", new_callable=AsyncMock, return_value=fake_page), \
             patch.object(m, "_abnt_sign_current", new_callable=AsyncMock, return_value=m.confirmed_done_result("done", adapter="abnt")):
            res = asyncio.run(m.abnt_checkin(fake_page, adapter))
            mock_sso.assert_called_once()


if __name__ == "__main__":
    unittest.main()
