"""Web toast 可交互化回归锚点（2026-10-16「无法点击确认 toast」根修）。

线上 #message-toast 曾是 pointer-events:none 纯展示且 3.5s 自动消失：用户点击
toast 时事件穿透到底层 topbar 按钮（执行全部待办/刷新），且没有任何确认控件。
根修后可见 toast 必须可交互：点任意处（含确认并关闭按钮）即手动确认关闭、
结果类(success/error)不自动消失必须用户确认、info 进度类保留 3.5s 自动消失、
悬停暂停自动消失、点击拦截防穿透。
本测试锚定 web/ 静态资源中的关键回归点（仓库无前端 JS 测试框架，沿用源码锚定式）。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class WebToastTests(unittest.TestCase):
    def setUp(self):
        self.app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.style_css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    def test_visible_toast_is_clickable(self):
        """可见态 toast 必须 pointer-events:auto（否则确认按钮永远点不到）。"""
        block = re.search(r"\.message-toast\.is-visible\s*\{[^}]*\}", self.style_css)
        self.assertIsNotNone(block, ".message-toast.is-visible 规则缺失")
        self.assertIn("pointer-events: auto", block.group(0))

    def test_hidden_toast_stays_click_through(self):
        """隐藏态保持 pointer-events:none，不让不可见 toast 挡住页面点击。"""
        block = re.search(r"\.message-toast\s*\{[^}]*\}", self.style_css)
        self.assertIsNotNone(block, ".message-toast 基础规则缺失")
        self.assertIn("pointer-events: none", block.group(0))

    def test_show_toast_renders_confirm_close_button(self):
        """showToast 必须渲染「确认并关闭」按钮（toast-close + aria-label）。"""
        self.assertIn('class="toast-close"', self.app_js)
        self.assertIn('aria-label="确认并关闭"', self.app_js)
        # CSP 零内联：关闭图标必须走 uiIcon，而不是内联 onclick/事件属性
        self.assertIn('uiIcon("x"', self.app_js)

    def test_toast_click_delegation_guards_misfire(self):
        """toast 点击委托必须 stopPropagation 防穿透;仅 × 按钮关闭;
        显示后 800ms 内忽略点击(防止弹出处接住上一次操作的误触)。"""
        m = re.search(
            r'\$\("#message-toast"\)\?\.addEventListener\("click".*?\}\);',
            self.app_js,
            re.S,
        )
        self.assertIsNotNone(m, "toast click 委托缺失")
        body = m.group(0)
        self.assertIn("stopPropagation", body)
        self.assertIn('closest(".toast-close")', body)
        self.assertIn("hideToast()", body)
        self.assertRegex(body, r"toastShownAt")
        self.assertIn("toastShownAt = performance.now()", self.app_js)

    def test_result_toasts_do_not_autodismiss(self):
        """success/error 结果类 toast 不自动消失，必须用户点击确认才关闭。"""
        # showToast 内的自动消失定时器必须被 info 类型门控
        m = re.search(
            r"toast\.innerHTML = [^\n]*toast-close[^\n]*;\s*\n(.*?)(\n\})",
            self.app_js,
            re.S,
        )
        self.assertIsNotNone(m, "showToast 渲染段缺失")
        self.assertRegex(m.group(1), r'if\s*\(type === "info"\)')

    def test_hover_pauses_autodismiss(self):
        """悬停暂停 3.5s 自动消失（用户来得及点确认），移出后仅 info 类恢复计时。"""
        self.assertIn('addEventListener("mouseenter"', self.app_js)
        self.assertIn('addEventListener("mouseleave"', self.app_js)
        self.assertIn('toast-info', self.app_js)

    def test_no_inline_html_event_handlers_in_web(self):
        """CSP 零内联红线：web 静态资源禁止 HTML onclick= 事件属性。"""
        self.assertNotIn('onclick="', self.app_js)
        index_html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('onclick="', index_html)

    def test_confirm_dialog_overlay_in_html(self):
        """确认弹窗必须预埋在 index.html，遵循 a11y role="dialog" 与 aria-modal。"""
        index_html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="confirm-dialog-overlay"', index_html)
        self.assertIn('role="dialog"', index_html)
        self.assertIn('aria-modal="true"', index_html)
        self.assertIn('id="confirm-dialog-cancel-btn"', index_html)
        self.assertIn('id="confirm-dialog-confirm-btn"', index_html)

    def test_confirm_dialog_styles(self):
        """确认弹窗样式：不可见时 pointer-events: none，可见时 pointer-events: auto 且 z-index 高于 toast。"""
        self.assertIn(".confirm-dialog-overlay", self.style_css)
        self.assertIn(".confirm-dialog-overlay.is-visible", self.style_css)
        overlay_block = re.search(r"\.confirm-dialog-overlay\s*\{[^}]*\}", self.style_css)
        self.assertIsNotNone(overlay_block)
        self.assertIn("pointer-events: none", overlay_block.group(0))
        visible_block = re.search(r"\.confirm-dialog-overlay\.is-visible\s*\{[^}]*\}", self.style_css)
        self.assertIsNotNone(visible_block)
        self.assertIn("pointer-events: auto", visible_block.group(0))

    def test_confirm_dialog_logic_in_app_js(self):
        """showConfirm 与 initConfirmDialog：支持 Escape 键、遮罩点击、取消与确认 Promise 解析。"""
        self.assertIn("function initConfirmDialog()", self.app_js)
        self.assertIn("function showConfirm(", self.app_js)
        self.assertIn('e.key === "Escape"', self.app_js)
        self.assertIn("confirmResolver", self.app_js)

    def test_destructive_actions_use_show_confirm(self):
        """删除站点与删除凭据必须通过 showConfirm 异步等待用户显式确认，禁止未经确认直接调用 api。"""
        self.assertIn("await showConfirm", self.app_js)
        # 站点抽屉删除必须调用 showConfirm
        m_del_site = re.search(
            r'\$\("#site-drawer-delete-site-btn"\)\?\.addEventListener\("click", async \(\) => \{(.*?)\}\);',
            self.app_js,
            re.S,
        )
        self.assertIsNotNone(m_del_site)
        self.assertIn("showConfirm", m_del_site.group(1))
        # 站点抽屉凭据删除必须调用 showConfirm
        m_del_cred = re.search(
            r'\$\("#site-drawer-delete-cred-btn"\)\?\.addEventListener\("click", async \(\) => \{(.*?)\}\);',
            self.app_js,
            re.S,
        )
        self.assertIsNotNone(m_del_cred)
        self.assertIn("showConfirm", m_del_cred.group(1))


if __name__ == "__main__":
    unittest.main()
