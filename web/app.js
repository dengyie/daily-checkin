/**
 * Daily Check-in Master Application (CheckinHub)
 * True gethomepage/homepage Visual Language + New-API Console Architecture
 * Awesome UI Kit Native Components + Standard UiIcon (Tabler Visuals)
 * 100% Strict CSP Safe (Zero Inline Styles)
 */

const esc = (value) => String(value ?? "").replace(
  /[&<>"']/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
);

/* ==========================================================================
   Awesome UI Kit · Standard UiIcon Engine (Tabler Geometry, MIT License)
   ========================================================================== */

const ICONS = {
  'activity': '<path d="M3 12h4l3 8 4-16 3 8h4"/>',
  'alert-triangle': '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  'arrow-down': '<path d="m6 9 6 6 6-6"/>',
  'arrow-left': '<path d="m15 6-6 6 6 6"/>',
  'arrow-right': '<path d="m9 6 6 6-6 6"/>',
  'arrow-up': '<path d="m6 15 6-6 6 6"/>',
  'arrow-left-right': '<path d="m8 7 4-4 4 4m0 10-4 4-4-4m4-14v14"/>',
  'arrows-sort': '<path d="m3 9 4-4 4 4m-4 10V5m14 4-4 4-4-4m4-4v14"/>',
  'bot': '<rect x="4" y="8" width="16" height="12" rx="2"/><path d="M2 14h2m16 0h2M9 13v2m6-2v2M9 4h6m-3-2v2"/>',
  'calendar': '<rect x="4" y="5" width="16" height="16" rx="2"/><path d="M16 3v4M8 3v4M4 11h16"/>',
  'chart-bar': '<path d="M3 3v18h18M7 16v-3m4 3v-6m4 6V8m4 8v-9"/>',
  'check': '<path d="m5 12 4 4L19 6"/>',
  'check-circle': '<circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/>',
  'chevron-down': '<path d="m6 9 6 6 6-6"/>',
  'chevron-left': '<path d="m14 6-6 6 6 6"/>',
  'chevron-right': '<path d="m10 6 6 6-6 6"/>',
  'circle-x': '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/>',
  'clipboard-list': '<path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="M9 12h6m-6 4h6"/>',
  'clock': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
  'code': '<path d="m8 9-3 3 3 3m8-6 3 3-3 3M14 5l-4 14"/>',
  'copy': '<rect x="8" y="8" width="11" height="11" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  'eye': '<path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/>',
  'external-link': '<path d="M14 5h5v5M19 5l-8 8M19 14v3a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h3"/>',
  'file-text': '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6m-4 5H8m8 4H8m2-8H8"/>',
  'git-branch': '<path d="M6 3v12a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3V9"/><circle cx="6" cy="3" r="2"/><circle cx="18" cy="7" r="2"/><circle cx="12" cy="18" r="2"/>',
  'globe': '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  'history': '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8m0-5v5h5m4-1v5l3 2"/>',
  'image': '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m21 15-4-4L5 20"/>',
  'key': '<circle cx="8" cy="15" r="4"/><path d="m10.85 12.15 7.15-7.15m0 0h4v4h-2v2h-2v-2"/>',
  'layers': '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5m-18 4 9 5 9-5"/>',
  'loader': '<path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1m0-12.8-2.1 2.1m-8.6 8.6-2.1 2.1"/>',
  'lock': '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  'moon': '<path d="M20 15.5A8 8 0 0 1 8.5 4 8 8 0 0 0 20 15.5Z"/>',
  'monitor': '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
  'paperclip': '<path d="m20 11-7.5 7.5a5 5 0 0 1-7-7L13 4a3.5 3.5 0 0 1 5 5l-7.5 7.5a2 2 0 0 1-3-3L14 7"/>',
  'play': '<polygon points="6 3 20 12 6 21 6 3"/>',
  'plus': '<path d="M12 5v14M5 12h14"/>',
  'refresh': '<path d="M20 11a8 8 0 0 0-14.5-4L3 10m0-5v5h5M4 13a8 8 0 0 0 14.5 4L21 14m0 5v-5h-5"/>',
  'search': '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>',
  'send': '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  'database': '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
  'info-circle': '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4m0-4h.01"/>',
  'settings': '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  'shield': '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  'shield-check': '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
  'shield-alert': '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M12 8v4m0 4h.01"/>',
  'sparkles': '<path d="m12 3-1.2 4.8L6 9l4.8 1.2L12 15l1.2-4.8L18 9l-4.8-1.2L12 3Z"/><path d="m19 15-.6 2.4L16 18l2.4.6L19 21l.6-2.4L22 18l-2.4-.6L19 15Z"/>',
  'square': '<rect x="6" y="6" width="12" height="12" rx="1"/>',
  'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4m0-14.2-1.4 1.4M6.3 17.7l-1.4 1.4"/>',
  'target': '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
  'thumbs-down': '<path d="M7 10v10H5a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2h2Zm0 0 3-7a2 2 0 0 1 2 2v3h6a2 2 0 0 1 2 2l-1 7a2 2 0 0 1-2 2h-7l-3-3"/>',
  'thumbs-up': '<path d="M7 14V4a2 2 0 0 1 2-2h2l1 6h6a2 2 0 0 1 2 2l-1 7a2 2 0 0 1-2 2H9l-2-3H5a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2h2"/>',
  'tool': '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.2 2.2-2.7-.7-.7-2.7 2.2-2.2Z"/>',
  'trash': '<path d="M3 6h18m-2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m-6 5v6m4-6v6"/>',
  'trending-up': '<path d="m22 7-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
  'user': '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  'user-check': '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><polyline points="16 11 18 13 22 9"/>',
  'undo': '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/>',
  'x': '<path d="m6 6 12 12M18 6 6 18"/>',
  'zap': '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>'
};

const VALID_ICON_NAMES = new Set(Object.keys(ICONS));

function uiIcon(name, { size = 18, strokeWidth = 1.8, label = '', className = '' } = {}) {
  if (!VALID_ICON_NAMES.has(name)) return '';
  const aria = label ? `aria-label="${esc(label)}"` : 'aria-hidden="true"';
  const cls = className ? `ui-icon ${esc(className)}` : 'ui-icon';
  return `<svg width="${esc(size)}" height="${esc(size)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${esc(strokeWidth)}" stroke-linecap="round" stroke-linejoin="round" class="${cls}" ${aria}>${ICONS[name] || ''}</svg>`;
}

class UiIconElement extends HTMLElement {
  static get observedAttributes() { return ["name", "size", "stroke-width", "class", "label"]; }
  connectedCallback() { this.render(); }
  attributeChangedCallback() { this.render(); }
  render() {
    const name = this.getAttribute("name") || "";
    const size = this.getAttribute("size") || 18;
    const strokeWidth = this.getAttribute("stroke-width") || 1.8;
    const label = this.getAttribute("label") || "";
    const className = this.getAttribute("class") || "";
    this.innerHTML = uiIcon(name, { size, strokeWidth, label, className });
  }
}
if (!customElements.get("ui-icon")) {
  customElements.define("ui-icon", UiIconElement);
}

/* ==========================================================================
   Dedicated High-Fidelity Vector SVG Icon Library (gethomepage style)
   ========================================================================== */

function getServiceSvgIcon(name, provider) {
  const n = (name || "").toLowerCase();
  const p = (provider || "").toLowerCase();

  if (p.includes("wisart") || n.includes("wisart") || n.includes("图片")) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 4V2m0 20v-2M8 9l-2-2m12 12-2-2m0-12 2-2M6 19l2-2"/><path d="m14 10-8.5 8.5a2.12 2.12 0 1 1-3-3L11 7"/><circle cx="16" cy="8" r="2" fill="#3b82f6" fill-opacity="0.4"/></svg>`;
  }
  if (n.includes("linux") || n.includes("linuxdo")) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a5 5 0 0 0-5 5v3a5 5 0 0 0 10 0V7a5 5 0 0 0-5-5z" fill="#f59e0b" fill-opacity="0.2"/><path d="M4 17a4 4 0 0 0 4 4h8a4 4 0 0 0 4-4v-3a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v3z"/><circle cx="9" cy="7" r="1" fill="#ffffff"/><circle cx="15" cy="7" r="1" fill="#ffffff"/></svg>`;
  }
  if (n.includes("v2ex")) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="#e2e8f0" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m4 6 8 12 8-12" fill="#334155" fill-opacity="0.5"/><path d="m9 6 3 5 3-5"/></svg>`;
  }
  if (n.includes("openai") || n.includes("gpt") || n.includes("chat") || n.includes("ai")) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v20M2 12h20M4.93 4.93l14.14 14.14M19.07 4.93 4.93 19.07" stroke="#10b981" stroke-opacity="0.6"/><circle cx="12" cy="12" r="4" fill="#10b981" fill-opacity="0.3"/></svg>`;
  }
  if (n.includes("github") || n.includes("git")) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="#cbd5e1" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22" fill="#64748b" fill-opacity="0.3"/></svg>`;
  }
  if (n.includes("cloudflare") || n.includes("cf")) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="#f97316" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" fill="#f97316" fill-opacity="0.2"/><path d="m9 12 2 2 4-4"/></svg>`;
  }
  if (n.includes("nodeseek") || n.includes("node") || n.includes("host")) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="#06b6d4" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="8" x="2" y="2" rx="2" ry="2" fill="#06b6d4" fill-opacity="0.2"/><rect width="20" height="8" x="2" y="14" rx="2" ry="2" fill="#06b6d4" fill-opacity="0.2"/><line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/></svg>`;
  }
  if (n.includes("juejin") || n.includes("gold")) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2" fill="#3b82f6" fill-opacity="0.3"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>`;
  }
  if (p.includes("api") || n.includes("api")) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="#a855f7" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z" fill="#a855f7" fill-opacity="0.3"/></svg>`;
  }
  if (p.includes("browser") || p.includes("stealth") || p.includes("cdp")) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="#06b6d4" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10" fill="#06b6d4" fill-opacity="0.15"/><line x1="2" x2="22" y1="12" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`;
  }
  return `<svg viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3" fill="#3b82f6" fill-opacity="0.3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`;
}

function categorizeSite(task) {
  const n = (task.name || "").toLowerCase();
  const p = (task.provider || "").toLowerCase();

  if (task.site_health_status === "suppressed" || task.status === "failed" || task.status === "FAIL") {
    return "熔断与异常待办 (Circuit Broken & Diagnostics)";
  }
  if (n.includes("openai") || n.includes("gpt") || n.includes("claude") || n.includes("ai") || p.includes("wisart") || n.includes("wisart")) {
    return "AI 助手与公益服务 (AI & Non-Profit Services)";
  }
  if (n.includes("linux") || n.includes("v2ex") || n.includes("node") || n.includes("host") || n.includes("juejin") || n.includes("git")) {
    return "开发者社区与站点 (Developer Communities)";
  }
  return "自动化引擎与协议 (Automated Engines & Protocols)";
}

function getCategoryIconName(category) {
  if (category.includes("熔断")) return "shield-alert";
  if (category.includes("AI 助手")) return "bot";
  if (category.includes("开发者")) return "globe";
  return "zap";
}

/* ==========================================================================
   Awesome UI Web Components Definitions (Strict CSP Safe)
   ========================================================================== */

class StatusIndicator extends HTMLElement {
  static get observedAttributes() { return ["status", "label", "ping-ms", "show-dot"]; }
  connectedCallback() { this.render(); }
  attributeChangedCallback() { this.render(); }
  render() {
    const status = this.getAttribute("status") || "online";
    const label = esc(this.getAttribute("label") || (status === "busy" ? "Busy" : status === "error" ? "Error" : status === "offline" ? "Offline" : "Online"));
    const pingMs = this.getAttribute("ping-ms");
    const showDot = this.getAttribute("show-dot") !== "false";

    this.innerHTML = `
      <div class="ui-status-indicator ${esc(status)}">
        ${showDot ? `<span class="ui-status-dot ${esc(status)}"></span>` : ""}
        <span>${label}</span>
        ${pingMs !== null && pingMs !== undefined ? `<span class="ui-status-ping">(${esc(pingMs)}ms)</span>` : ""}
      </div>
    `;
  }
}
if (!customElements.get("status-indicator")) customElements.define("status-indicator", StatusIndicator);

class ThemeToggle extends HTMLElement {
  static get observedAttributes() { return ["storage-key"]; }
  connectedCallback() {
    this.storageKey = this.getAttribute("storage-key") || "daily-checkin-theme";
    this.mode = localStorage.getItem(this.storageKey) || "auto";
    this.render();
    this.applyTheme(this.mode);
    this.bindEvents();
  }
  resolveTheme(m) {
    if (m === "light" || m === "dark") return m;
    const hour = new Date().getHours();
    const isNight = window.matchMedia("(prefers-color-scheme: dark)").matches || hour >= 19 || hour < 7;
    return isNight ? "dark" : "light";
  }
  applyTheme(nextMode) {
    this.mode = nextMode;
    const resolved = this.resolveTheme(nextMode);
    document.documentElement.dataset.theme = nextMode;
    document.documentElement.dataset.resolvedTheme = resolved;
    document.documentElement.classList.toggle("dark", resolved === "dark");
    localStorage.setItem(this.storageKey, nextMode);
    this.updateActiveButtons();
  }
  updateActiveButtons() {
    this.querySelectorAll("[data-mode]").forEach((btn) => {
      if (btn.dataset.mode === this.mode) btn.classList.add("active");
      else btn.classList.remove("active");
    });
  }
  render() {
    this.innerHTML = `
      <div class="ui-theme-toggle">
        <button type="button" class="ui-theme-btn" data-mode="auto" title="跟随系统主题">${uiIcon("sparkles", { size: 13 })}<span>自动</span></button>
        <button type="button" class="ui-theme-btn" data-mode="light" title="浅色模式">${uiIcon("sun", { size: 13 })}<span>浅色</span></button>
        <button type="button" class="ui-theme-btn" data-mode="dark" title="深色模式">${uiIcon("moon", { size: 13 })}<span>深色</span></button>
      </div>
    `;
  }
  bindEvents() {
    this.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-mode]");
      if (!btn) return;
      this.applyTheme(btn.dataset.mode);
    });
  }
}
if (!customElements.get("theme-toggle")) customElements.define("theme-toggle", ThemeToggle);

class PromptChips extends HTMLElement {
  static get observedAttributes() { return ["suggestions", "active"]; }
  connectedCallback() { this.render(); this.bindEvents(); }
  attributeChangedCallback() { this.render(); }
  render() {
    let items = [];
    try { items = JSON.parse(this.getAttribute("suggestions") || "[]"); } catch { items = []; }
    const active = this.getAttribute("active") || "";
    this.innerHTML = `
      <div class="ui-prompt-chips">
        ${items.map((item) => `
          <button type="button" class="ui-chip-btn ${item === active ? "active" : ""}" data-chip="${esc(item)}">
            ${esc(item)}
          </button>
        `).join("")}
      </div>
    `;
  }
  bindEvents() {
    this.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-chip]");
      if (!btn) return;
      const chipValue = btn.dataset.chip;
      this.setAttribute("active", chipValue);
      this.dispatchEvent(new CustomEvent("select", { detail: chipValue, bubbles: true }));
    });
  }
}
if (!customElements.get("prompt-chips")) customElements.define("prompt-chips", PromptChips);

/* ==========================================================================
   State & App Controller
   ========================================================================== */

const defaultApiBase = (typeof window !== "undefined" && window.location && (window.location.hostname === "check.example.com" || window.location.hostname === "check-api.example.com"))
  ? (window.location.port ? `${window.location.protocol}//${window.location.hostname}:8765` : `https://check-api.example.com`)
  : "http://127.0.0.1:8765";
const apiBase = window.API_BASE || defaultApiBase;
const SESSION_KEY = "daily-checkin-password";

let state = {
  day: "",
  updated_at: "",
  tasks: [],
  credentials: [],
  runs: [],
  jobs: [],
  history: [],
};

let currentFilter = "全部";
let searchQuery = "";
let manualSearchQuery = "";
let manualStatusFilter = "all"; // "all", "pending", "done"
let currentSort = "import_desc"; // import_desc, import_asc, alpha_asc, alpha_desc, status, health
let activeView = "overview";
let serverOnline = true;

const VIEW_TITLES = {
  overview: "控制台总览",
  tasks: "自动签到站点",
  "batch-config": "批次调度与全局配置",
  manual: "人工手动签到专区",
  "site-manage": "新增签到任务站",
  history: "历史趋势与证据",
  queue: "异步作业队列",
  health: "熔断与健康诊断",
  credentials: "Keychain 凭据箱",
  settings: "系统环境与运行状态",
};

const $ = (selector) => document.querySelector(selector);

function apiUrl(path) {
  return `${apiBase.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
}

function updateServerStatus(st, label, ping) {
  const el = $("#server-status");
  if (!el) return;
  el.setAttribute("status", st);
  if (label) el.setAttribute("label", label);
  if (ping !== undefined) el.setAttribute("ping-ms", String(ping));
}

let toastTimer = null;
function showToast(msg, type = "info") {
  const toast = $("#message-toast");
  if (!toast) return;

  if (toastTimer) {
    clearTimeout(toastTimer);
    toastTimer = null;
  }

  // 根据 type 选择合适的 icon
  let iconName = "info-circle";
  if (type === "success" || (!type && /成功|完成|已解锁|已保存|已记录|已入队/.test(msg))) {
    iconName = "check-circle";
    type = "success";
  } else if (type === "error" || (!type && /失败|错误|请提供|未配置|HTTP/.test(msg))) {
    iconName = "alert-triangle";
    type = "error";
  }

  toast.className = `message-toast is-visible toast-${type}`;
  toast.innerHTML = `<ui-icon name="${iconName}" size="16"></ui-icon><span>${esc(msg)}</span>`;

  toastTimer = setTimeout(() => {
    toast.classList.remove("is-visible");
    toastTimer = null;
  }, 3500);
}

async function api(path, payload, method = "POST", isRetry = false) {
  const headers = { "Content-Type": "application/json" };
  const storedPassword = localStorage.getItem(SESSION_KEY);
  if (storedPassword) headers["X-DailyCheckin-Password"] = storedPassword;
  if (state.csrf) headers["X-CSRF-Token"] = state.csrf;

  const url = apiUrl(path);
  const resp = await fetch(url, {
    method,
    headers,
    body: payload ? JSON.stringify(payload) : undefined,
    cache: "no-store",
  });

  if (resp.status === 401) {
    showTokenGate();
    throw new Error("请提供有效的控制台访问密码");
  }

  // Auto-heal on CSRF mismatch: transparently fetch new state and retry once
  if (resp.status === 403 && !isRetry) {
    try {
      const errJson = await resp.clone().json();
      if (errJson.error === "csrf") {
        const freshState = await api("/api/state", null, "GET", true);
        if (freshState && freshState.csrf) {
          state = freshState;
          return await api(path, payload, method, true);
        }
      }
    } catch {}
  }

  if (!resp.ok) {
    let errMessage = `HTTP ${resp.status}`;
    try {
      const errJson = await resp.json();
      if (errJson.error) errMessage = errJson.error;
    } catch {}
    throw new Error(errMessage);
  }

  return await resp.json();
}

let isLoadingState = false;
async function load() {
  if (isLoadingState) return;
  isLoadingState = true;
  const t0 = performance.now();
  try {
    const data = await api("/api/state", null, "GET");
    const ping = Math.round(performance.now() - t0);
    state = data;
    serverOnline = true;
    updateServerStatus("online", "API 正常", ping);
    render();
  } catch (error) {
    serverOnline = false;
    updateServerStatus("offline", "API 离线");
    console.error("Failed to load state:", error);
  } finally {
    isLoadingState = false;
  }
}

/* ==========================================================================
   Navigation & Sidebar
   ========================================================================== */

function switchView(viewName) {
  if (!VIEW_TITLES[viewName]) return;
  activeView = viewName;
  window.location.hash = `#/${viewName}`;

  document.querySelectorAll(".nav-item").forEach((btn) => {
    if (btn.dataset.view === viewName) btn.classList.add("is-active");
    else btn.classList.remove("is-active");
  });

  document.querySelectorAll(".view-pane").forEach((pane) => {
    if (pane.id === `pane-${viewName}`) pane.classList.add("is-active");
    else pane.classList.remove("is-active");
  });

  const pageTitle = $("#page-title");
  if (pageTitle) pageTitle.textContent = VIEW_TITLES[viewName];

  if (viewName === "site-manage") {
    initNewSiteTagsPicker();
  }
  if (viewName === "batch-config") {
    loadBatchConfig();
  }

  closeEvidenceDrawer();
  closeSiteDrawer();
}

document.querySelectorAll("[data-view]").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

$("#sidebar-toggle-btn")?.addEventListener("click", () => {
  $("#app-sidebar")?.classList.toggle("is-collapsed");
});

$("#jump-to-tasks-btn")?.addEventListener("click", () => switchView("tasks"));
$("#jump-to-trend-btn")?.addEventListener("click", () => switchView("history"));
$("#jump-to-history-btn")?.addEventListener("click", () => switchView("history"));
$("#overview-jump-health-btn")?.addEventListener("click", () => switchView("health"));

/* ==========================================================================
   Site Details Slide-over Drawer (右侧浮层详情页)
   ========================================================================== */

function openSiteDetailDrawer(siteName) {
  const task = (state.tasks || []).find((t) => t.name === siteName);
  if (!task) return;

  const cred = (state.credentials || []).find((c) => c.site === siteName);
  const isDone = task.status === "done" || task.status === "OK" || task.status === "ALREADY";
  const isFail = task.status === "failed" || task.status === "FAIL";
  const isSupp = task.site_health_status === "suppressed";
  const pillClass = isSupp ? "is-suppressed" : isDone ? "is-ok" : isFail ? "is-fail" : "is-pending";
  const pillText = isSupp ? "熔断" : isDone ? "已打卡" : isFail ? "失败" : "待办";
  const category = categorizeSite(task);

  // Find recent run executions for this specific site
  const siteHistory = [];
  (state.runs || []).forEach((run) => {
    (run.items || []).forEach((item, itemIdx) => {
      const matchName = item.site_name || item.site;
      if (matchName === siteName) {
        siteHistory.push({
          run_id: run.id,
          run_day: run.day,
          started_at: run.started_at,
          ...item,
          uid: `${run.id}-${itemIdx}`,
        });
      }
    });
  });

  // Header Elements
  const avatarBox = $("#site-drawer-avatar");
  if (avatarBox) avatarBox.innerHTML = getServiceSvgIcon(task.name, task.provider);
  const titleEl = $("#site-drawer-title");
  if (titleEl) titleEl.textContent = task.name;
  const catEl = $("#site-drawer-category");
  if (catEl) catEl.textContent = category;
  const statusPill = $("#site-drawer-status-pill");
  if (statusPill) {
    statusPill.className = `tile-status-pill ${pillClass}`;
    $("#site-drawer-status-text").textContent = pillText;
  }
  const safeUrl = (task.url && (task.url.startsWith("http://") || task.url.startsWith("https://"))) ? task.url : null;
  const urlEl = $("#site-drawer-url");
  if (urlEl) urlEl.textContent = safeUrl || "无直接 URL";

  // 1. Config Pane
  const paneConfig = $("#site-pane-config");
  if (paneConfig) {
    const isManualMode = task.checkin_mode === "manual" || (task.tags || "").toLowerCase().includes("manual");
    paneConfig.innerHTML = `
      <div class="tile-info-grid tile-info-col1 mb-md">
        <div>
          <div class="tile-prop-label">站点名称 (NAME)</div>
          <div class="tile-prop-val text-lg"><strong>${esc(task.name)}</strong></div>
        </div>
        <div>
          <div class="tile-prop-label">签到模式与隔离策略 (CHECK-IN MODE)</div>
          <div class="tile-prop-val">
            <span class="tile-category-tag ${isManualMode ? "manual" : ""}">${isManualMode ? `${uiIcon("user-check", { size: 12 })} 手工签到 (每日定时批次自动跳过)` : `${uiIcon("zap", { size: 12 })} 自动签到 (08:10 自动化中枢执行)`}</span>
          </div>
        </div>
        <div>
          <div class="tile-prop-label">分类 (CATEGORY)</div>
          <div class="tile-prop-val"><span class="tile-category-tag">${esc(category)}</span></div>
        </div>
        <div>
          <div class="tile-prop-label">自动化引擎 (PROVIDER)</div>
          <div class="tile-prop-val font-mono"><code>${esc(task.provider || "auto")}</code></div>
        </div>
        <div>
          <div class="tile-prop-label">数据库关联路径</div>
          <div class="tile-prop-val font-mono">${esc(task.obsidian_path || `system.db (Site ID: ${task.site_id || "-"})`)}</div>
        </div>
        <div>
          <div class="tile-prop-label">任务来源 (SOURCE)</div>
          <div class="tile-prop-val">${esc(task.source || "system")}</div>
        </div>
      </div>

      <!-- Config & Mode Editor Form -->
      <div class="content-card">
        <div class="card-title mb-sm">${uiIcon("settings", { size: 15 })} 修改站点执行属性与签到链路</div>
        <form id="site-drawer-config-form">
          <input type="hidden" name="name" value="${esc(task.name)}">
          <div class="form-group-item">
            <label for="site-drawer-checkin-mode">签到模式 (Execution Mode)</label>
            <select name="checkin_mode" id="site-drawer-checkin-mode">
              <option value="auto" ${!isManualMode ? "selected" : ""}>自动签到 (每日 08:10 批次无头自动执行)</option>
              <option value="manual" ${isManualMode ? "selected" : ""}>手动签到 (专属人工签到池 · 每日批次自动跳过)</option>
            </select>
            <small class="tile-prop-label mt-xs d-block">
              提示：含有复杂人机验证码 (Turnstile/Geetest)、2FA 或独立风控检测的站点请切换为「手动签到」。
            </small>
          </div>
          <div class="form-group-item">
            <label>标签 (Tags，多选已存在或新增)</label>
            <div class="tags-picker-group">
              <div class="tags-chips-cloud" id="drawer-site-tags-checkboxes"></div>
              <div class="tags-custom-add-row">
                <input id="drawer-site-custom-tag-input" placeholder="输入自定义新标签...">
                <button type="button" class="btn sm" id="drawer-site-add-tag-btn">${uiIcon("plus", { size: 12 })} 添加标签</button>
              </div>
            </div>
            <input type="hidden" name="tags" id="site-drawer-tags" value="${esc(task.tags || (isManualMode ? "manual" : ""))}">
          </div>
          <div class="form-group-item">
            <label for="site-drawer-url">入口网址 (Target URL)</label>
            <input type="text" name="url" id="site-drawer-url-input" value="${esc(task.url || "")}" placeholder="https://...">
          </div>
          <button type="submit" class="btn primary sm" id="site-drawer-save-config-btn">${uiIcon("check", { size: 13 })} 保存站点配置</button>
        </form>
      </div>

      <!-- Danger Zone Card -->
      <div class="content-card danger-zone-card mt-lg">
        <div class="card-title text-danger">${uiIcon("alert-triangle", { size: 15 })} 危险区域 (Danger Zone)</div>
        <p class="tile-prop-label text-xs mt-xs">
          彻底删除此站点及其在今日待办任务、Keychain 凭据箱与健康熔断库中的所有关联记录。历史批次证据将保留站点名称以供审计。
        </p>
        <div class="action-bar-row mt-sm">
          <button type="button" class="btn danger sm" id="site-drawer-delete-site-btn">${uiIcon("trash", { size: 13 })} 删除该签到站</button>
        </div>
      </div>
    `;

    initTagsPicker(
      "#drawer-site-tags-checkboxes",
      "#site-drawer-tags",
      "#drawer-site-custom-tag-input",
      "#drawer-site-add-tag-btn",
      task.tags || (isManualMode ? "manual" : "")
    );

    $("#site-drawer-delete-site-btn")?.addEventListener("click", async () => {
      const confirmText = `确定彻底删除站点「${task.name}」吗？\n\n此操作将同时清理：\n1. 今日签到待办任务\n2. 绑定的 Keychain 凭据密钥（如有）\n3. 健康状态与熔断抑制记录\n\n操作不可逆，是否继续？`;
      if (!confirm(confirmText)) return;

      const delBtn = $("#site-drawer-delete-site-btn");
      const origHtml = delBtn ? delBtn.innerHTML : "";
      if (delBtn) {
        delBtn.disabled = true;
        delBtn.innerHTML = `${uiIcon("loader", { size: 13, class: "spin" })} 正在删除...`;
      }
      try {
        const res = await api("/api/sites/delete", { name: task.name });
        showToast(`站点「${task.name}」已成功删除`, "success");
        closeSiteDrawer();
        await load();
      } catch (err) {
        showToast(`删除站点失败: ${err.message}`, "error");
        if (delBtn) {
          delBtn.disabled = false;
          delBtn.innerHTML = origHtml;
        }
      }
    });

    $("#site-drawer-config-form")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.target.querySelector('button[type="submit"]');
      const origHtml = btn ? btn.innerHTML : "";
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<ui-icon name="loader" size="13" class="spin"></ui-icon> 正在保存...`;
      }
      try {
        const formData = new FormData(e.target);
        const formObj = Object.fromEntries(formData);
        const targetSiteName = formObj.name || task.name;
        const res = await api("/api/sites/update", formObj);
        showToast(`站点「${targetSiteName}」配置已更新为 ${formObj.checkin_mode === "manual" ? "手动签到" : "自动签到"}`, "success");
        await load();
        openSiteDetailDrawer(targetSiteName);
      } catch (err) {
        showToast(`更新配置失败: ${err.message}`, "error");
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = origHtml;
        }
      }
    });
  }

  // 2. Credentials Pane
  const paneCreds = $("#site-pane-credentials");
  if (paneCreds) {
    paneCreds.innerHTML = cred ? `
      <div class="tile-info-grid tile-info-col1 mb-md">
        <div>
          <div class="tile-prop-label">KEYCHAIN 引用标识 (REF)</div>
          <div class="tile-prop-val font-mono"><code>${esc(cred.ref)}</code></div>
        </div>
        <div>
          <div class="tile-prop-label">凭据类型 (KIND)</div>
          <div class="tile-prop-val"><span class="tile-status-pill is-pending"><span class="tile-pulse-dot"></span>${esc(cred.kind)}</span></div>
        </div>
        <div>
          <div class="tile-prop-label">备注标签 (LABEL)</div>
          <div class="tile-prop-val">${esc(cred.label || "默认主账号")}</div>
        </div>
        <div>
          <div class="tile-prop-label">最后更新时间 (UPDATED)</div>
          <div class="tile-prop-val">${esc(cred.updated_at || "-")}</div>
        </div>
      </div>
      <button class="btn danger sm" id="site-drawer-delete-cred-btn">${uiIcon("trash", { size: 13 })} 删除该凭据</button>
    ` : `
      <div class="empty-cell mb-md">未绑定 Keychain 独立凭据（使用浏览器 Profile 登录态或免密签到）</div>
      <div class="content-card">
        <div class="card-title mb-sm">${uiIcon("key", { size: 15 })} 绑定新凭据到 macOS Keychain</div>
        <form id="site-drawer-cred-form">
          <input type="hidden" name="site" value="${esc(task.name)}">
          <div class="form-group-item">
            <label for="site-drawer-cred-kind">凭据类型</label>
            <select name="kind" id="site-drawer-cred-kind">
              <option value="token">API Token / Bearer</option>
              <option value="cookie">Cookie 字符串</option>
              <option value="password">账号密码组合 (Password)</option>
            </select>
          </div>
          <div class="form-group-item is-hidden" id="site-drawer-cred-account-row">
            <label for="site-drawer-cred-account">登录账号 / 用户名 / 邮箱 *</label>
            <input type="text" name="account" id="site-drawer-cred-account" placeholder="输入站点登录账号/用户名/邮箱...">
          </div>
          <div class="form-group-item">
            <label for="site-drawer-cred-secret" id="site-drawer-cred-secret-label">API Token / Bearer 令牌 * (保存在系统 Keychain)</label>
            <input type="password" name="secret" id="site-drawer-cred-secret" placeholder="输入 API Token / 密钥..." required>
          </div>
          <div class="form-group-item">
            <label for="site-drawer-cred-label">备注标签 (可选)</label>
            <input type="text" name="label" id="site-drawer-cred-label" placeholder="如: 主账号 / session_id">
          </div>
          <button type="submit" class="btn primary sm block">保存并绑定凭据</button>
        </form>
      </div>
    `;

    const kindSelect = $("#site-drawer-cred-kind");
    const accountRow = $("#site-drawer-cred-account-row");
    const accountInput = $("#site-drawer-cred-account");
    const secretLabel = $("#site-drawer-cred-secret-label");
    const secretInput = $("#site-drawer-cred-secret");

    const updateCredKindUi = () => {
      if (!kindSelect) return;
      const k = kindSelect.value;
      if (k === "password") {
        if (accountRow) accountRow.classList.remove("is-hidden");
        if (accountInput) accountInput.required = true;
        if (secretLabel) secretLabel.textContent = "登录密码 * (保存在系统 Keychain)";
        if (secretInput) secretInput.placeholder = "输入站点登录密码...";
      } else {
        if (accountRow) accountRow.classList.add("is-hidden");
        if (accountInput) {
          accountInput.required = false;
          accountInput.value = "";
        }
        if (secretLabel) {
          secretLabel.textContent = k === "cookie"
            ? "Cookie 字符串 * (保存在系统 Keychain)"
            : "API Token / Bearer 令牌 * (保存在系统 Keychain)";
        }
        if (secretInput) {
          secretInput.placeholder = k === "cookie"
            ? "输入完整 Cookie 字符串 (如 session=...; uid=...)..."
            : "输入 API Token / 密钥...";
        }
      }
    };
    if (kindSelect) {
      kindSelect.addEventListener("change", updateCredKindUi);
      updateCredKindUi();
    }

    $("#site-drawer-delete-cred-btn")?.addEventListener("click", async () => {
      if (!confirm(`确定删除站点 ${task.name} 的 Keychain 凭据？`)) return;
      try {
        await api("/api/credentials/delete", { ref: cred.ref });
        await load();
        openSiteDetailDrawer(siteName);
        showToast("凭据已安全删除");
      } catch (err) { showToast(err.message); }
    });

    $("#site-drawer-cred-form")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const formData = new FormData(e.target);
        const formObj = Object.fromEntries(formData);
        if (formObj.kind === "password") {
          const account = (formObj.account || "").trim();
          const secret = (formObj.secret || "").trim();
          if (!account) {
            showToast("请输入账号/用户名");
            return;
          }
          formObj.secret = JSON.stringify({ account, password: secret });
          if (!formObj.label) formObj.label = account;
        }
        delete formObj.account;
        await api("/api/credentials", formObj);
        await load();
        openSiteDetailDrawer(siteName);
        showToast("凭据已安全保存到 Keychain");
      } catch (err) { showToast(err.message); }
    });
  }

  // 3. Health & Diagnostics Pane
  const paneHealth = $("#site-pane-health");
  if (paneHealth) {
    paneHealth.innerHTML = `
      <div class="tile-info-grid tile-info-col1 mb-md">
        <div>
          <div class="tile-prop-label">今日签到状态</div>
          <div class="tile-prop-val">
            <span class="tile-status-pill ${pillClass}">
              <span class="tile-pulse-dot"></span>
              ${pillText}
            </span>
          </div>
        </div>
        <div>
          <div class="tile-prop-label">熔断健康状态 (CIRCUIT BREAKER)</div>
          <div class="tile-prop-val">${isSupp ? `<span class="text-danger font-bold">${uiIcon("shield-alert", { size: 14, className: "text-danger" })} 确定性连续失败已熔断抑制</span>` : `<span class="text-success">${uiIcon("shield-check", { size: 14, className: "text-success" })} 健康良好 (Active)</span>`}</div>
        </div>
        <div>
          <div class="tile-prop-label">连续失败计数 (FAILURES)</div>
          <div class="tile-prop-val font-mono">${task.site_health_failures || 0} 次</div>
        </div>
        <div>
          <div class="tile-prop-label">最新执行/失败原因归因</div>
          <div class="tile-prop-val font-mono">${task.last_reason ? `<code>${esc(task.last_reason)}</code>` : `<span class="text-muted">无异常记录</span>`}</div>
        </div>
      </div>
      ${task.last_reason && !isDone ? `
        <div class="tile-reason-alert mb-md">
          <strong>诊断分析：</strong>${task.last_reason.includes("dead_url") ? "站点域名可能已失效或被 DNS 污染，请检查站点最新可达网址。" : task.last_reason.includes("cloudflare") ? "触发 Cloudflare 强质询验证盾，需在 Chrome 9222 实例中手动完成一次人机验证。" : task.last_reason.includes("auth") ? "登录态已过期，请在签到 Profile 中重新登录该站点。" : "站点可能改版或签到按钮选择器有变动。"}
        </div>
      ` : task.last_reason === "manual_confirmed" ? `
        <div class="tile-reason-alert is-success mb-md">
          ${uiIcon("check-circle", { size: 14, className: "text-success" })} <strong>人工打卡确认：</strong>今日已由用户手动完成签到并成功标记。
        </div>
      ` : ""}
    `;
  }

  // 4. History Logs Pane
  const paneHistory = $("#site-pane-history");
  if (paneHistory) {
    paneHistory.innerHTML = siteHistory.length ? `
      <div class="table-responsive">
        <table class="modern-table">
          <thead>
            <tr>
              <th>批次 ID</th>
              <th>日期</th>
              <th>状态</th>
              <th>耗时</th>
              <th>原因归因</th>
              <th>证据</th>
            </tr>
          </thead>
          <tbody>
            ${siteHistory.map((h) => `
              <tr>
                <td><strong>#${h.run_id}</strong></td>
                <td>${esc(h.run_day || "-")}</td>
                <td>
                  <span class="tile-status-pill ${h.status === "OK" || h.status === "ALREADY" ? "is-ok" : "is-fail"}">
                    <span class="tile-pulse-dot"></span>
                    ${esc(h.status)}
                  </span>
                </td>
                <td>${h.latency_ms ? `${(h.latency_ms / 1000).toFixed(1)}s` : h.duration_ms ? `${(h.duration_ms / 1000).toFixed(1)}s` : "-"}</td>
                <td><code>${esc(h.reason || "-")}</code></td>
                <td><button class="btn sm" data-view-site-evidence="${esc(h.uid)}">查看证据 ${uiIcon("arrow-right", { size: 12 })}</button></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    ` : `<div class="empty-cell">暂无该站点的单独执行流水记录</div>`;

    paneHistory.querySelectorAll("[data-view-site-evidence]").forEach((btn) => {
      btn.onclick = () => {
        const uid = btn.dataset.viewSiteEvidence;
        const item = siteHistory.find((h) => h.uid === uid);
        if (item) {
          openEvidenceDrawer(`批次 #${item.run_id} · ${siteName}`, `执行状态: ${item.status} · 耗时: ${item.latency_ms || item.duration_ms || 0}ms`, item);
        }
      };
    });
  }

  // Footer Actions
  const footerBar = $("#site-drawer-footer");
  if (footerBar) {
    footerBar.innerHTML = `
      <div class="action-bar-row">
        <button class="btn primary" id="site-drawer-run-btn">${uiIcon("play", { size: 13 })} 自动化执行</button>
        ${safeUrl ? `<button class="btn" id="site-drawer-manual-checkin-btn">${uiIcon("globe", { size: 13 })} 手动签到 (打开网页) ${uiIcon("external-link", { size: 12 })}</button>` : ""}
        <button class="btn success" id="site-drawer-mark-complete-btn">${isDone ? uiIcon("undo", { size: 13 }) + " 取消打卡 (恢复未完成)" : uiIcon("check", { size: 13 }) + " 标记为已完成"}</button>
      </div>
    `;
    $("#site-drawer-run-btn")?.addEventListener("click", () => {
      triggerRun([task.name]);
      showToast(`已触发站点 ${task.name} 的自动化签到作业`);
    });
    $("#site-drawer-manual-checkin-btn")?.addEventListener("click", () => {
      handleManualCheckin(task.name, safeUrl);
    });
    $("#site-drawer-mark-complete-btn")?.addEventListener("click", (e) => {
      markTaskManuallyComplete(task.name, e.currentTarget);
    });
  }

  switchSiteDrawerTab("config");
  closeEvidenceDrawer();
  const drawer = $("#site-drawer-panel");
  const backdrop = $("#drawer-backdrop");
  if (backdrop) backdrop.classList.add("is-visible");
  if (drawer) drawer.classList.add("is-open");
}

function switchSiteDrawerTab(tabKey) {
  document.querySelectorAll("#site-drawer-panel [data-site-tab]").forEach((btn) => {
    if (btn.dataset.siteTab === tabKey) btn.classList.add("is-active");
    else btn.classList.remove("is-active");
  });
  document.querySelectorAll("#site-drawer-panel .drawer-tab-pane").forEach((pane) => {
    if (pane.id === `site-pane-${tabKey}`) pane.classList.add("is-active");
    else pane.classList.remove("is-active");
  });
}

document.querySelectorAll("#site-drawer-panel [data-site-tab]").forEach((btn) => {
  btn.addEventListener("click", () => switchSiteDrawerTab(btn.dataset.siteTab));
});

function closeSiteDrawer() {
  const drawer = $("#site-drawer-panel");
  const backdrop = $("#drawer-backdrop");
  if (drawer) drawer.classList.remove("is-open");
  if (backdrop && !$("#drawer-panel")?.classList.contains("is-open")) {
    backdrop.classList.remove("is-visible");
  }
}

$("#site-drawer-close-btn")?.addEventListener("click", closeSiteDrawer);

/* ==========================================================================
   4-Tab Evidence Drawer (KnowledgeDrawer Engine)
   ========================================================================== */

function openEvidenceDrawer(title, subtitle, item) {
  const drawer = $("#drawer-panel");
  const backdrop = $("#drawer-backdrop");
  if (!drawer || !backdrop) return;

  $("#drawer-title").textContent = title || "执行证据详情";
  $("#drawer-subtitle").textContent = subtitle || "CDP 自动化执行上下文与验证快照";

  let parsedEvidence = null;
  if (item && item.evidence_json) {
    try { parsedEvidence = typeof item.evidence_json === "string" ? JSON.parse(item.evidence_json) : item.evidence_json; }
    catch { parsedEvidence = null; }
  }

  // 1. Tab Actions Pane
  const paneActions = $("#pane-tab-actions");
  if (paneActions) {
    const trace = (parsedEvidence && parsedEvidence.trace) || [];
    paneActions.innerHTML = trace.length ? `
      <div class="trace-steps-wrap">
        ${trace.map((step, idx) => {
          const status = step.status || (step.error ? "error" : "success");
          return `
            <div class="step-card">
              <div class="step-num ${esc(status)}">${status === "success" ? uiIcon("check", { size: 13 }) : status === "error" ? uiIcon("circle-x", { size: 13 }) : uiIcon("loader", { size: 13 })}</div>
              <div class="step-body">
                <div class="step-title">${esc(step.name || step.action || `Step ${idx + 1}`)}</div>
                ${step.detail ? `<p class="tile-prop-label">${esc(step.detail)}</p>` : ""}
                ${step.duration_ms ? `<span class="tile-category-tag">${step.duration_ms}ms</span>` : ""}
              </div>
            </div>
          `;
        }).join("")}
      </div>
    ` : `
      <div class="empty-cell">未记录动作流水跟踪或由底层原生 Runner 快速完成</div>
    `;
  }

  // 2. Tab Confirmation Assertions Pane
  const paneConfirmation = $("#pane-tab-confirmation");
  if (paneConfirmation) {
    const confObj = parsedEvidence && (parsedEvidence.confirmation || parsedEvidence.post || parsedEvidence.attribution);
    paneConfirmation.innerHTML = `
      <div class="drawer-head-box">
        <div>
          <strong class="drawer-title-main">打卡状态判定 (Confirmation Assertions)</strong>
          <div class="drawer-subtitle-sub">双重校验与 DOM 文本匹对结果</div>
        </div>
        <span class="tile-status-pill ${item.status === "OK" || item.status === "ALREADY" ? "is-ok" : "is-fail"}">
          <span class="tile-pulse-dot"></span>
          ${esc(item.status)}
        </span>
      </div>

      ${item.reason ? `
        <div>
          <label class="drawer-section-label error">异常 / 失败原因</label>
          <pre class="evidence-box-viewer error font-mono">${esc(item.reason)}</pre>
        </div>
      ` : `
        <div class="step-card">
          <div class="step-num success">${uiIcon("check", { size: 14 })}</div>
          <div class="step-body">
            <div class="step-title">签到断言成功</div>
            <p class="tile-prop-label">页面返回成功判定文本，任务标记为已完成。</p>
          </div>
        </div>
      `}

      ${confObj ? `
        <div>
          <label class="drawer-section-label">确认依据负载 (Confirmation Data)</label>
          <pre class="evidence-box-viewer font-mono">${esc(JSON.stringify(confObj, null, 2))}</pre>
        </div>
      ` : ""}
    `;
  }

  // 3. Tab Attribution Pane
  const paneAttribution = $("#pane-tab-attribution");
  if (paneAttribution) {
    paneAttribution.innerHTML = `
      <div class="drawer-kpi-grid">
        <div class="kpi-card">
          <div>
            <div class="kpi-info-label">总执行耗时</div>
            <div class="kpi-info-val">${item.latency_ms || 0} ms</div>
          </div>
        </div>
        <div class="kpi-card">
          <div>
            <div class="kpi-info-label">状态归因</div>
            <div class="kpi-info-val val-md">${esc(parsedEvidence?.attribution || item.provider || "runner")}</div>
          </div>
        </div>
      </div>
      <div>
        <label class="drawer-section-label">执行环境参数</label>
        <div class="tile-info-grid tile-info-col1">
          <div>
            <div class="tile-prop-label">执行引擎 (Provider)</div>
            <div class="tile-prop-val">${esc(item.provider || "auto")}</div>
          </div>
          <div>
            <div class="tile-prop-label">调度阶段 (Stage)</div>
            <div class="tile-prop-val">${esc(item.stage || "-")}</div>
          </div>
          <div>
            <div class="tile-prop-label">记录创建时间</div>
            <div class="tile-prop-val">${esc(item.created_at || "-")}</div>
          </div>
        </div>
      </div>
    `;
  }

  // 4. Tab JSON Pane
  const paneJson = $("#pane-tab-json");
  if (paneJson) {
    const rawJson = item.evidence_json || "{}";
    let formattedJson = rawJson;
    try { formattedJson = JSON.stringify(JSON.parse(rawJson), null, 2); } catch {}
    paneJson.innerHTML = `
      <div class="json-action-bar">
        <span class="tile-prop-label">RAW EVIDENCE JSON</span>
        <button class="btn sm" id="copy-evidence-json-btn">${uiIcon("copy", { size: 13 })} 复制 JSON</button>
      </div>
      <pre class="evidence-box-viewer font-mono">${esc(formattedJson)}</pre>
    `;
    $("#copy-evidence-json-btn")?.addEventListener("click", () => {
      navigator.clipboard.writeText(formattedJson).then(() => showToast("已复制证据 JSON 到剪贴板"));
    });
  }

  switchDrawerTab("actions");
  backdrop.classList.add("is-visible");
  drawer.classList.add("is-open");
}

function switchDrawerTab(tabKey) {
  document.querySelectorAll("#drawer-panel [data-drawer-tab]").forEach((btn) => {
    if (btn.dataset.drawerTab === tabKey) btn.classList.add("is-active");
    else btn.classList.remove("is-active");
  });
  document.querySelectorAll("#drawer-panel .drawer-tab-pane").forEach((pane) => {
    if (pane.id === `pane-tab-${tabKey}`) pane.classList.add("is-active");
    else pane.classList.remove("is-active");
  });
}

document.querySelectorAll("#drawer-panel [data-drawer-tab]").forEach((btn) => {
  btn.addEventListener("click", () => switchDrawerTab(btn.dataset.drawerTab));
});

function getAllKnownTags() {
  const tagSet = new Set(["manual", "captcha", "cloudflare", "turnstile", "2fa", "vip", "cookie", "token", "ai", "forum", "proxy"]);
  (state.tasks || []).forEach((t) => {
    if (t.tags) {
      t.tags.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean).forEach((x) => tagSet.add(x));
    }
  });
  return Array.from(tagSet);
}

function initTagsPicker(containerSelector, hiddenInputSelector, customInputSelector, addBtnSelector, initialTags = "") {
  const container = $(containerSelector);
  const hiddenInput = $(hiddenInputSelector);
  const customInput = $(customInputSelector);
  const addBtn = $(addBtnSelector);
  if (!container || !hiddenInput) return;

  const currentTags = new Set(
    (initialTags || "")
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean)
  );

  const syncStateAndClasses = () => {
    hiddenInput.value = Array.from(currentTags).join(",");
    container.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
      const isChecked = currentTags.has(cb.dataset.tag);
      cb.checked = isChecked;
      const pill = cb.closest(".tag-checkbox-pill");
      if (pill) {
        if (isChecked) pill.classList.add("is-checked");
        else pill.classList.remove("is-checked");
      }
    });
  };

  const renderTagsCloud = () => {
    const allTags = Array.from(new Set([...getAllKnownTags(), ...Array.from(currentTags)]));
    container.innerHTML = allTags.map((tag) => {
      const isChecked = currentTags.has(tag);
      return `
        <label class="tag-checkbox-pill ${isChecked ? "is-checked" : ""}">
          <input type="checkbox" data-tag="${esc(tag)}" ${isChecked ? "checked" : ""}>
          <span>${esc(tag)}</span>
        </label>
      `;
    }).join("");

    container.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
      cb.addEventListener("change", (e) => {
        const val = e.target.dataset.tag;
        if (e.target.checked) {
          currentTags.add(val);
        } else {
          currentTags.delete(val);
        }
        hiddenInput.value = Array.from(currentTags).join(",");
        const pill = e.target.closest(".tag-checkbox-pill");
        if (pill) {
          if (e.target.checked) pill.classList.add("is-checked");
          else pill.classList.remove("is-checked");
        }
      });
    });

    syncStateAndClasses();
  };

  const addCustomTag = () => {
    if (!customInput) return;
    const val = (customInput.value || "").trim().toLowerCase().replace(/[,，]/g, "");
    if (val) {
      // 保证把当前界面上所有已勾选的 tag 都保留在 currentTags 中
      container.querySelectorAll('input[type="checkbox"]:checked').forEach((cb) => {
        if (cb.dataset.tag) currentTags.add(cb.dataset.tag);
      });
      currentTags.add(val);
      customInput.value = "";
      renderTagsCloud();
    }
  };

  addBtn?.addEventListener("click", addCustomTag);
  customInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addCustomTag();
    }
  });

  renderTagsCloud();
}

function closeEvidenceDrawer() {
  const drawer = $("#drawer-panel");
  const backdrop = $("#drawer-backdrop");
  if (drawer) drawer.classList.remove("is-open");
  if (backdrop && !$("#site-drawer-panel")?.classList.contains("is-open")) {
    backdrop.classList.remove("is-visible");
  }
}

$("#drawer-close-btn")?.addEventListener("click", closeEvidenceDrawer);

$("#drawer-backdrop")?.addEventListener("click", () => {
  closeEvidenceDrawer();
  closeSiteDrawer();
});

/* ==========================================================================
   Filter & Rendering (Spread Responsive Magnetic Service Tiles)
   ========================================================================== */

function getManualTasks() {
  const manualList = (state.tasks || []).filter((t) =>
    t.checkin_mode === "manual" || (t.tags && t.tags.toLowerCase().includes("manual"))
  );
  let list = [...manualList];
  if (manualStatusFilter === "pending") {
    list = list.filter((t) => t.status !== "done" && t.status !== "OK" && t.status !== "ALREADY");
  } else if (manualStatusFilter === "done") {
    list = list.filter((t) => t.status === "done" || t.status === "OK" || t.status === "ALREADY");
  }
  if (manualSearchQuery) {
    const q = manualSearchQuery.toLowerCase();
    list = list.filter((t) =>
      (t.name && t.name.toLowerCase().includes(q)) ||
      (t.url && t.url.toLowerCase().includes(q)) ||
      (t.tags && t.tags.toLowerCase().includes(q)) ||
      (t.last_reason && t.last_reason.toLowerCase().includes(q))
    );
  }
  return { all: manualList, filtered: list };
}

function getFilteredTasks() {
  let list = (state.tasks || []).filter((t) =>
    t.checkin_mode !== "manual" && (!t.tags || !t.tags.toLowerCase().includes("manual"))
  );
  if (currentFilter === "待办") {
    list = list.filter((t) => t.status !== "done" && t.status !== "OK" && t.status !== "ALREADY" && t.site_health_status !== "suppressed");
  } else if (currentFilter === "已完成") {
    list = list.filter((t) => t.status === "done" || t.status === "OK" || t.status === "ALREADY");
  } else if (currentFilter === "失败") {
    list = list.filter((t) => (t.status === "failed" || t.status === "FAIL") && t.site_health_status !== "suppressed");
  } else if (currentFilter === "确定性失败抑制" || currentFilter === "熔断") {
    list = list.filter((t) => t.site_health_status === "suppressed");
  } else if (currentFilter !== "全部") {
    // Check if filtering by category tag
    list = list.filter((t) => categorizeSite(t) === currentFilter);
  }

  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    list = list.filter((t) =>
      (t.name && t.name.toLowerCase().includes(q)) ||
      (t.url && t.url.toLowerCase().includes(q)) ||
      (t.provider && t.provider.toLowerCase().includes(q)) ||
      (categorizeSite(t).toLowerCase().includes(q)) ||
      (t.last_reason && t.last_reason.toLowerCase().includes(q))
    );
  }

  // Sorting logic
  list.sort((a, b) => {
    if (currentSort === "import_desc") {
      // Newest import first: by site_created_at descending, fallback to site_id desc
      const timeA = a.site_created_at || "";
      const timeB = b.site_created_at || "";
      if (timeA && timeB && timeA !== timeB) return timeB.localeCompare(timeA);
      return (b.site_id || 0) - (a.site_id || 0);
    }
    if (currentSort === "import_asc") {
      // Oldest import first: by site_created_at ascending, fallback to site_id asc
      const timeA = a.site_created_at || "";
      const timeB = b.site_created_at || "";
      if (timeA && timeB && timeA !== timeB) return timeA.localeCompare(timeB);
      return (a.site_id || 0) - (b.site_id || 0);
    }
    if (currentSort === "alpha_asc") {
      // Alphabetical ascending (A → Z, Chinese Pinyin)
      return (a.name || "").localeCompare(b.name || "", "zh-CN", { numeric: true, sensitivity: "base" });
    }
    if (currentSort === "alpha_desc") {
      // Alphabetical descending (Z → A)
      return (b.name || "").localeCompare(a.name || "", "zh-CN", { numeric: true, sensitivity: "base" });
    }
    if (currentSort === "status") {
      // Status priority: pending (0) -> failed (1) -> suppressed (2) -> done (3)
      const rank = (t) => {
        if (t.status === "pending") return 0;
        if (t.status === "failed" || t.status === "FAIL") return 1;
        if (t.site_health_status === "suppressed") return 2;
        return 3;
      };
      return rank(a) - rank(b);
    }
    if (currentSort === "health") {
      // Health priority: suppressed first, then by failure count desc
      const isSuppA = a.site_health_status === "suppressed" ? 1 : 0;
      const isSuppB = b.site_health_status === "suppressed" ? 1 : 0;
      if (isSuppA !== isSuppB) return isSuppB - isSuppA;
      return (b.site_health_failures || 0) - (a.site_health_failures || 0);
    }
    return 0;
  });

  return list;
}

function renderTile(task, showAction = true) {
  const isDone = task.status === "done" || task.status === "OK" || task.status === "ALREADY";
  const isFail = task.status === "failed" || task.status === "FAIL";
  const isSupp = task.site_health_status === "suppressed";
  const isManual = task.checkin_mode === "manual" || (task.tags && task.tags.toLowerCase().includes("manual"));
  const tileClass = isSupp ? "is-suppressed" : isDone ? "is-ok" : isFail ? "is-fail" : "is-pending";
  const pillClass = isSupp ? "is-suppressed" : isDone ? "is-ok" : isFail ? "is-fail" : "is-pending";
  const pillText = isSupp ? "熔断" : isDone ? "已打卡" : isFail ? "失败" : "待办";
  const safeUrl = (task.url && (task.url.startsWith("http://") || task.url.startsWith("https://"))) ? task.url : null;
  const svgIcon = getServiceSvgIcon(task.name, task.provider);
  const category = categorizeSite(task);

  return `
    <article class="homepage-tile ${tileClass} is-clickable" data-site-tile="${esc(task.name)}">
      <span class="tile-status-bar"></span>
      <div class="tile-head">
        <div class="tile-brand-box">
          <div class="tile-avatar-svg-box">${svgIcon}</div>
          <div class="tile-name-wrap">
            <div class="tile-title-row">
              <span class="tile-title">${esc(task.name)}</span>
              <span class="tile-category-tag">${esc(category)}</span>
              ${isManual ? `<span class="tile-category-tag manual">${uiIcon("user-check", { size: 11 })} 人工签到</span>` : ""}
            </div>
            ${safeUrl ? `<a href="${esc(safeUrl)}" target="_blank" rel="noopener noreferrer" class="tile-url-link" data-stop-prop="true">${esc(safeUrl)} ${uiIcon("external-link", { size: 11 })}</a>` : `<span class="tile-url-link">无直接 URL</span>`}
          </div>
        </div>
        <span class="tile-status-pill ${pillClass}">
          <span class="tile-pulse-dot"></span>
          ${pillText}
        </span>
      </div>

      <div class="tile-info-grid">
        <div>
          <div class="tile-prop-label">PROVIDER</div>
          <div class="tile-prop-val">${esc(task.provider || "auto")}</div>
        </div>
        <div>
          <div class="tile-prop-label">KEYCHAIN 凭据</div>
          <div class="tile-prop-val">${task.credential_ref ? `<code>${esc(task.credential_ref)}</code>` : `<span class="tile-prop-label">未绑定</span>`}</div>
        </div>
      </div>

      ${task.last_reason ? (
        isDone ? (
          task.last_reason === "manual_confirmed" ? `
            <div class="tile-reason-alert is-success" title="人工手动签到已确认完成">
              ${uiIcon("check-circle", { size: 12, className: "text-success" })} 人工打卡已确认
            </div>
          ` : ""
        ) : `
          <div class="tile-reason-alert" title="${esc(task.last_reason)}">
            ${uiIcon("alert-triangle", { size: 12, className: "text-warning" })} ${esc(task.last_reason)}
          </div>
        `
      ) : ""}

      <div class="tile-footer-bar">
        <span class="tile-prop-label">来源: ${esc(task.source || "system")}</span>
        ${showAction ? `
          <div class="tile-action-btns">
            ${isManual ? `
              ${safeUrl ? `<button class="btn sm" data-manual-checkin="${esc(task.name)}" data-url="${esc(safeUrl)}" data-stop-prop="true" title="在新标签页打开站点网页">${uiIcon("globe", { size: 12 })} 打开站点</button>` : ""}
              <button class="btn sm success" data-mark-complete="${esc(task.name)}" data-stop-prop="true" title="再点击一次可取消打卡，恢复为未打卡状态">${!isDone ? uiIcon("check", { size: 12 }) + " 标记完成" : uiIcon("undo", { size: 12 }) + " 已打卡"}</button>
            ` : `
              ${!isDone ? `
                <button class="btn sm success" data-mark-complete="${esc(task.name)}" data-stop-prop="true" title="手动签到完成后点击标记为已完成">${uiIcon("check", { size: 12 })} 标记完成</button>
              ` : `
                <button class="btn sm success" data-mark-complete="${esc(task.name)}" data-stop-prop="true" title="再点击一次可取消打卡，恢复为未打卡状态">${uiIcon("undo", { size: 12 })} 已打卡</button>
              `}
              <button class="btn sm primary" data-run-site="${esc(task.name)}" data-stop-prop="true" title="触发自动化打卡脚本">${uiIcon("play", { size: 12 })} 执行</button>
            `}
          </div>
        ` : `<span>-</span>`}
      </div>
    </article>
  `;
}

function renderTilesGrid(tasksList) {
  if (!tasksList || !tasksList.length) {
    return `<div class="empty-cell">暂无匹配站点数据</div>`;
  }
  return `
    <div class="homepage-tiles-grid">
      ${tasksList.map((task) => renderTile(task, true)).join("")}
    </div>
  `;
}

/* ==========================================================================
   SVG Historical Trend Charts Generator (100% Strict CSP Safe)
   ========================================================================== */

function generateTrendSvg(historyList, isMini = false) {
  if (!historyList || !historyList.length) {
    return `<div class="empty-cell">暂无历史走势数据</div>`;
  }

  const list = [...historyList].reverse(); // Oldest to newest (left to right)
  const n = list.length;
  const width = isMini ? 360 : 780;
  const height = isMini ? 120 : 200;
  const padLeft = isMini ? 25 : 45;
  const padRight = isMini ? 25 : 45;
  const padTop = isMini ? 15 : 25;
  const padBottom = isMini ? 25 : 35;

  const chartW = width - padLeft - padRight;
  const chartH = height - padTop - padBottom;

  const points = list.map((item, idx) => {
    const total = item.total || 1;
    const rate = Math.round(((item.done_count || 0) / total) * 100);
    const x = n === 1 ? padLeft + chartW / 2 : padLeft + (idx / (n - 1)) * chartW;
    const y = padTop + chartH - (rate / 100) * chartH;
    return { x, y, rate, day: item.day ? item.day.substring(5) : `${idx + 1}` };
  });

  const lineD = points.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
  const areaD = `${lineD} L ${points[points.length - 1].x.toFixed(1)} ${(padTop + chartH).toFixed(1)} L ${points[0].x.toFixed(1)} ${(padTop + chartH).toFixed(1)} Z`;

  return `
    <svg class="chart-svg" viewBox="0 0 ${width} ${height}">
      <defs>
        <linearGradient id="trendAreaGrad" x1="0%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" stop-color="#3b82f6" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="#3b82f6" stop-opacity="0.0"/>
        </linearGradient>
      </defs>

      <!-- Grid Lines & Y-Axis Scale -->
      <line x1="${padLeft}" y1="${padTop}" x2="${width - padRight}" y2="${padTop}" class="chart-grid-line"/>
      <line x1="${padLeft}" y1="${padTop + chartH / 2}" x2="${width - padRight}" y2="${padTop + chartH / 2}" class="chart-grid-line"/>
      <line x1="${padLeft}" y1="${padTop + chartH}" x2="${width - padRight}" y2="${padTop + chartH}" class="chart-grid-line"/>

      ${!isMini ? `
        <text x="${padLeft - 8}" y="${padTop + 4}" class="chart-scale-text" text-anchor="end">100%</text>
        <text x="${padLeft - 8}" y="${(padTop + chartH / 2 + 4).toFixed(1)}" class="chart-scale-text" text-anchor="end">50%</text>
        <text x="${padLeft - 8}" y="${(padTop + chartH + 4).toFixed(1)}" class="chart-scale-text" text-anchor="end">0%</text>
      ` : ""}

      <!-- Area and Line -->
      <path d="${areaD}" class="chart-area"/>
      <path d="${lineD}" class="chart-line"/>

      <!-- Points & Labels -->
      ${points.map((p) => `
        <circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="4.5" class="chart-point">
          <title>${p.day}: 成功率 ${p.rate}%</title>
        </circle>
        <text x="${p.x.toFixed(1)}" y="${(padTop + chartH + 18).toFixed(1)}" class="chart-axis-text">${p.day}</text>
        ${!isMini ? `<text x="${p.x.toFixed(1)}" y="${(p.y - 8).toFixed(1)}" class="chart-val-text">${p.rate}%</text>` : ""}
      `).join("")}
    </svg>
  `;
}

/* ==========================================================================
   Master Render Pipeline
   ========================================================================== */

function render() {
  const autoTasks = (state.tasks || []).filter((t) =>
    t.checkin_mode !== "manual" && (!t.tags || !t.tags.toLowerCase().includes("manual"))
  );
  const totalTasks = autoTasks.length;
  const counts = { pending: 0, done: 0, failed: 0, suppressed: 0 };
  autoTasks.forEach((task) => {
    if (task.site_health_status === "suppressed") counts.suppressed++;
    if (task.status === "done" || task.status === "OK" || task.status === "ALREADY") counts.done++;
    else if (task.status === "failed" || task.status === "FAIL") counts.failed++;
    else counts.pending++;
  });

  const successRate = totalTasks ? Math.round((counts.done / totalTasks) * 100) : 0;

  // 1. Sidebar Badges
  $("#sidebar-pending-badge").textContent = String(totalTasks);
  $("#sidebar-runs-badge").textContent = String((state.runs || []).length);
  $("#sidebar-jobs-badge").textContent = String((state.jobs || []).length);
  $("#sidebar-health-badge").textContent = String(counts.suppressed);

  // 2. View 1: Overview KPIs & Summary
  $("#kpi-done").textContent = String(counts.done);
  $("#kpi-pending").textContent = String(counts.pending);
  $("#kpi-failed").textContent = String(counts.failed);
  $("#kpi-rate").textContent = `${successRate}%`;

  $("#overview-total-tasks").textContent = String(totalTasks);
  $("#overview-creds-count").textContent = String((state.credentials || []).length);
  $("#overview-jobs-count").textContent = String((state.jobs || []).filter((j) => j.status === "queued" || j.status === "running").length);
  $("#overview-health-count").textContent = String(counts.suppressed);

  // Overview Mini Trend Chart
  const overviewTrendChart = $("#overview-trend-chart");
  if (overviewTrendChart) {
    overviewTrendChart.innerHTML = generateTrendSvg(state.history || [], true);
  }

  // Overview Recent Runs Table
  const overviewRunsTbody = $("#overview-runs-tbody");
  if (overviewRunsTbody) {
    const recent = (state.runs || []).slice(0, 5);
    overviewRunsTbody.innerHTML = recent.length ? recent.map((r) => {
      const items = r.items || [];
      const okCount = items.filter((i) => i.status === "OK" || i.status === "ALREADY").length;
      return `
        <tr>
          <td><strong>#${r.id}</strong></td>
          <td><code>${esc(r.source)}</code></td>
          <td>${esc(r.started_at)}</td>
          <td>
            <span class="tile-status-pill ${r.exit_code === 0 ? "is-ok" : "is-fail"}">
              <span class="tile-pulse-dot"></span>
              exit ${r.exit_code ?? "-"}
            </span>
          </td>
          <td>${okCount} / ${items.length} 成功</td>
          <td><button class="btn sm" data-jump-run-id="${r.id}">查看明细 ${uiIcon("arrow-right", { size: 12 })}</button></td>
        </tr>
      `;
    }).join("") : `<tr><td colspan="6" class="empty-cell">暂无运行历史记录</td></tr>`;

    overviewRunsTbody.querySelectorAll("[data-jump-run-id]").forEach((btn) => {
      btn.addEventListener("click", () => {
        switchView("history");
        const runId = btn.getAttribute("data-jump-run-id");
        const el = document.getElementById(`run-batch-${runId}`);
        if (el) el.scrollIntoView({ behavior: "smooth" });
      });
    });
  }

  // 3. View 2: Tasks Matrix (平铺全展开，无外部包裹容器)
  const filteredTasks = getFilteredTasks();
  $("#tasks-header-count").textContent = `${filteredTasks.length} / ${totalTasks}`;

  const chips = $("#tasks-filter-chips");
  if (chips) {
    const suggestions = [
      `全部 (${totalTasks})`,
      `待办 (${counts.pending})`,
      `已完成 (${counts.done})`,
      `失败 (${counts.failed})`,
    ];
    if (counts.suppressed > 0) suggestions.push(`熔断 (${counts.suppressed})`);
    chips.setAttribute("suggestions", JSON.stringify(suggestions));
    chips.setAttribute("active", currentFilter.includes("(") ? currentFilter : `${currentFilter} (${currentFilter === "全部" ? totalTasks : currentFilter === "待办" ? counts.pending : currentFilter === "已完成" ? counts.done : currentFilter === "失败" ? counts.failed : counts.suppressed})`);
  }

  const tasksTilesContainer = $("#tasks-tiles-container");
  if (tasksTilesContainer) {
    tasksTilesContainer.innerHTML = renderTilesGrid(filteredTasks);
  }

  // 3.5. View 2.5: Manual Check-in Workspace (专区渲染)
  const { all: manualAll, filtered: manualFiltered } = getManualTasks();
  const manualPendingCount = manualAll.filter((t) => t.status === "pending" || t.status === "failed" || t.status === "FAIL").length;
  const manualDoneCount = manualAll.filter((t) => t.status === "done" || t.status === "OK" || t.status === "ALREADY").length;
  const manualRate = manualAll.length ? Math.round((manualDoneCount / manualAll.length) * 100) : 0;

  $("#sidebar-manual-badge").textContent = String(manualAll.length);
  const manualHeaderCount = $("#manual-header-count");
  if (manualHeaderCount) manualHeaderCount.textContent = `${manualFiltered.length} / ${manualAll.length}`;

  const manualKpiTotal = $("#manual-kpi-total");
  if (manualKpiTotal) manualKpiTotal.textContent = String(manualAll.length);
  const manualKpiPending = $("#manual-kpi-pending");
  if (manualKpiPending) manualKpiPending.textContent = String(manualPendingCount);
  const manualKpiDone = $("#manual-kpi-done");
  if (manualKpiDone) manualKpiDone.textContent = String(manualDoneCount);
  const manualKpiRate = $("#manual-kpi-rate");
  if (manualKpiRate) manualKpiRate.textContent = `${manualRate}%`;

  const manualChips = $("#manual-filter-chips");
  if (manualChips) {
    const suggestions = [
      `全部 (${manualAll.length})`,
      `待办 (${manualPendingCount})`,
      `已完成 (${manualDoneCount})`,
    ];
    manualChips.setAttribute("suggestions", JSON.stringify(suggestions));
    const activeLabel = manualStatusFilter === "pending"
      ? `待办 (${manualPendingCount})`
      : manualStatusFilter === "done"
        ? `已完成 (${manualDoneCount})`
        : `全部 (${manualAll.length})`;
    manualChips.setAttribute("active", activeLabel);
  }

  const manualTilesContainer = $("#manual-tiles-container");
  if (manualTilesContainer) {
    if (!manualAll.length) {
      manualTilesContainer.innerHTML = `
        <div class="empty-cell manual-empty">
          <div class="empty-cell-icon">${uiIcon("user-check", { size: 32 })}</div>
          <div class="empty-cell-title">暂无被标记为「人工签到」的站点</div>
          <div class="tile-prop-label">
            如某个站点需要过人工验证码 (Turnstile/Geetest) 或 2FA 登录，可点击站点卡片进入详情页，在「站点资料与配置」中将签到模式切换为「手动签到」。该站点将自动从此专区集中管理，并在每日 08:10 批次中自动跳过。
          </div>
        </div>
      `;
    } else if (!manualFiltered.length) {
      manualTilesContainer.innerHTML = `<div class="empty-cell">当前筛选条件下暂无人工站点数据</div>`;
    } else {
      manualTilesContainer.innerHTML = renderTilesGrid(manualFiltered);
    }
  }

  // 4. View 3: History & Trend Analytics
  const historyList = state.history || [];
  let totalSuccessEver = 0;
  let totalTasksSum = 0;
  let successSum = 0;

  historyList.forEach((h) => {
    totalSuccessEver += (h.done_count || 0);
    totalTasksSum += (h.total || 0);
    successSum += (h.done_count || 0);
  });

  const avgSuccessRate = totalTasksSum > 0 ? Math.round((successSum / totalTasksSum) * 100) : successRate;

  $("#history-kpi-avg-rate").textContent = `${avgSuccessRate}%`;
  $("#history-kpi-total-runs").textContent = String(state.total_runs_count !== undefined ? state.total_runs_count : (state.runs || []).length);
  $("#history-kpi-days-count").textContent = `${historyList.length || 1} 天`;
  $("#history-kpi-total-success").textContent = String(totalSuccessEver);

  // Large Trend Curve
  const historyCurveChart = $("#history-curve-chart");
  if (historyCurveChart) {
    historyCurveChart.innerHTML = generateTrendSvg(historyList, false);
  }

  // Daily History Table
  const historyDailyTbody = $("#history-daily-tbody");
  if (historyDailyTbody) {
    historyDailyTbody.innerHTML = historyList.length ? historyList.map((h) => {
      const rate = h.total ? Math.round(((h.done_count || 0) / h.total) * 100) : 0;
      return `
        <tr>
          <td><strong>${esc(h.day)}</strong></td>
          <td>${h.total}</td>
          <td><span class="text-success font-mono font-bold">${h.done_count || 0}</span></td>
          <td><span class="text-danger font-mono">${h.fail_count || 0}</span></td>
          <td><span class="text-warning font-mono">${h.pending_count || 0}</span></td>
          <td>
            <span class="tile-status-pill ${rate >= 80 ? "is-ok" : rate >= 50 ? "is-pending" : "is-fail"}">
              <span class="tile-pulse-dot"></span>
              ${rate}%
            </span>
          </td>
        </tr>
      `;
    }).join("") : `<tr><td colspan="6" class="empty-cell">暂无历史按日统计数据</td></tr>`;
  }

  // History Batches & Evidence
  const historyContainer = $("#history-batches-container");
  if (historyContainer) {
    const runsList = state.runs || [];
    historyContainer.innerHTML = runsList.length ? runsList.map((run) => {
      const items = run.items || [];
      const okCount = items.filter((i) => i.status === "OK" || i.status === "ALREADY").length;
      const failCount = items.length - okCount;

      return `
        <div class="content-card mb-sm" id="run-batch-${run.id}">
          <div class="content-card-head">
            <div>
              <strong>批次 #${run.id}</strong>
              <span class="tile-status-pill ${run.exit_code === 0 ? "is-ok" : "is-fail"} ml-xs">
                <span class="tile-pulse-dot"></span>
                Exit ${run.exit_code ?? "-"}
              </span>
              <span class="tile-prop-label ml-xs">来源: <code>${esc(run.source)}</code> · 启动于: ${esc(run.started_at)}</span>
            </div>
            <div class="tile-prop-val">
              <span class="text-success">${okCount} 成功</span> / <span class="text-danger">${failCount} 失败</span>
            </div>
          </div>
          <div class="table-responsive">
            <table class="modern-table">
              <thead>
                <tr>
                  <th>站点名称</th>
                  <th>状态</th>
                  <th>Provider</th>
                  <th>耗时</th>
                  <th>结果摘要</th>
                  <th>证据</th>
                </tr>
              </thead>
              <tbody>
                ${items.map((item, idx) => {
                  const isOk = item.status === "OK" || item.status === "ALREADY";
                  return `
                    <tr>
                      <td><strong>${esc(item.site_name)}</strong></td>
                      <td>
                        <span class="tile-status-pill ${isOk ? "is-ok" : "is-fail"}">
                          <span class="tile-pulse-dot"></span>
                          ${esc(item.status)}
                        </span>
                      </td>
                      <td><code>${esc(item.provider || "-")}</code></td>
                      <td>${item.latency_ms || 0} ms</td>
                      <td>${item.reason ? `<span class="text-danger">${esc(item.reason)}</span>` : `<span class="text-success">打卡成功</span>`}</td>
                      <td><button class="btn sm" data-open-drawer-idx="${run.id}-${idx}">查看证据 ${uiIcon("arrow-right", { size: 12 })}</button></td>
                    </tr>
                  `;
                }).join("")}
              </tbody>
            </table>
          </div>
        </div>
      `;
    }).join("") : `<div class="empty-cell">暂无历史批次数据</div>`;

    runsList.forEach((run) => {
      (run.items || []).forEach((item, idx) => {
        const btn = document.querySelector(`[data-open-drawer-idx="${run.id}-${idx}"]`);
        if (btn) {
          btn.onclick = () => openEvidenceDrawer(`批次 #${run.id} · ${item.site_name}`, `启动于: ${run.started_at}`, item);
        }
      });
    });
  }

  // 5. View 4: Job Queue Table
  const jobsTbody = $("#jobs-tbody");
  if (jobsTbody) {
    const jobsList = state.jobs || [];
    jobsTbody.innerHTML = jobsList.length ? jobsList.map((j) => `
      <tr>
        <td><strong>#${j.id}</strong></td>
        <td><code>${esc(j.kind)}</code></td>
        <td>${esc(j.created_at)}</td>
        <td>
          <span class="tile-status-pill ${j.status === "done" ? "is-ok" : j.status === "failed" ? "is-fail" : "is-pending"}">
            <span class="tile-pulse-dot"></span>
            ${esc(j.status)}
          </span>
        </td>
        <td>${j.exit_code !== null ? `<code>${j.exit_code}</code>` : `<span class="text-warning">排队中...</span>`}</td>
        <td>${j.message ? `<pre class="evidence-box-viewer job-queue font-mono">${esc(j.message)}</pre>` : "-"}</td>
      </tr>
    `).join("") : `<tr><td colspan="6" class="empty-cell">暂无作业队列记录</td></tr>`;
  }

  // 6. View 5: Health & Circuit Breakers (平铺)
  const healthTilesContainer = $("#health-tiles-container");
  if (healthTilesContainer) {
    const suppressedTasks = (state.tasks || []).filter((t) => t.site_health_status === "suppressed" || t.status === "failed" || t.status === "FAIL");
    healthTilesContainer.innerHTML = suppressedTasks.length ? renderTilesGrid(suppressedTasks) : `<div class="empty-cell">当前所有站点健康状态良好，无熔断或严重异常站点。</div>`;
  }

  // 7. View 6: Settings / Credentials Table
  const credsTbody = $("#credentials-tbody");
  if (credsTbody) {
    const credsList = state.credentials || [];
    credsTbody.innerHTML = credsList.length ? credsList.map((c) => `
      <tr>
        <td><strong>${esc(c.site || "未绑定站点")}</strong></td>
        <td><span class="tile-status-pill is-pending"><span class="tile-pulse-dot"></span>${esc(c.kind)}</span></td>
        <td>${esc(c.label || "-")}</td>
        <td><code>${esc(c.ref)}</code></td>
        <td>${esc(c.updated_at)}</td>
        <td><button class="btn sm danger" data-del-cred="${esc(c.ref)}">${uiIcon("trash", { size: 12 })} 删除凭据</button></td>
      </tr>
    `).join("") : `<tr><td colspan="6" class="empty-cell">暂无保存的凭据引用</td></tr>`;

    credsTbody.querySelectorAll("[data-del-cred]").forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm("确认删除该凭据？")) return;
        try {
          await api("/api/credentials/delete", { ref: btn.dataset.delCred });
          await load();
          showToast("凭据已安全移除");
        } catch (err) { showToast(err.message); }
      };
    });
  }

  // Bind Tile Click to Open Right Detail Drawer
  document.querySelectorAll("[data-site-tile]").forEach((tile) => {
    tile.onclick = (e) => {
      if (e.target.closest("[data-stop-prop]")) return;
      openSiteDetailDrawer(tile.dataset.siteTile);
    };
  });
  document.querySelectorAll("[data-manual-checkin]").forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      handleManualCheckin(btn.dataset.manualCheckin, btn.dataset.url);
    };
  });
  document.querySelectorAll("[data-mark-complete]").forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      markTaskManuallyComplete(btn.dataset.markComplete, btn);
    };
  });
  document.querySelectorAll("[data-open-site-detail]").forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      openSiteDetailDrawer(btn.dataset.openSiteDetail);
    };
  });
  document.querySelectorAll("[data-run-site]").forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      triggerRun([btn.dataset.runSite]);
    };
  });
  document.querySelectorAll("[data-delete-credential]").forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("确认删除该站点保存的凭据？")) return;
      try {
        await api("/api/credentials/delete", { ref: btn.dataset.deleteCredential });
        await load();
        showToast("凭据已安全移除");
      } catch (err) { showToast(err.message); }
    };
  });
}

/* ==========================================================================
   Actions & Forms
   ========================================================================== */

function handleManualCheckin(siteName, url) {
  if (url && (url.startsWith("http://") || url.startsWith("https://"))) {
    window.open(url, "_blank", "noopener,noreferrer");
    showToast(`已在新标签页打开「${siteName}」，完成后请点击「标记完成」`, "info");
  } else {
    showToast(`站点「${siteName}」未配置入口 URL`, "error");
  }
}

function getTodayString() {
  const d = new Date();
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

async function markTaskManuallyComplete(siteName, clickedBtn = null) {
  let originalHtml = "";
  if (clickedBtn) {
    originalHtml = clickedBtn.innerHTML;
    clickedBtn.disabled = true;
    clickedBtn.innerHTML = `${uiIcon("loader", { size: 12, className: "spin" })} 标记中...`;
  }
  try {
    showToast(`正在确认并提交「${siteName}」打卡状态...`, "info");
    const today = getTodayString();
    const res = await api("/api/tasks/complete", { name: siteName, day: today });
    if (res.ok) {
      const reverted = res.status === "pending" || res.action === "revert";
      showToast(reverted
        ? `站点「${siteName}」已取消打卡，恢复为未打卡状态。`
        : `站点「${siteName}」已成功标记为完成！`, reverted ? "info" : "success");
      await load();
      if ($("#site-drawer-panel")?.classList.contains("is-open") && $("#site-drawer-title")?.textContent === siteName) {
        openSiteDetailDrawer(siteName);
      }
    }
  } catch (err) {
    showToast(`标记完成失败: ${err.message}`, "error");
  } finally {
    if (clickedBtn) {
      clickedBtn.disabled = false;
      clickedBtn.innerHTML = originalHtml;
    }
  }
}

async function triggerRun(sites) {
  try {
    updateServerStatus("busy");
    const data = await api("/api/run", { sites });
    showToast(`作业 #${data.job_id} 已入队执行，后台无头浏览器正在签到...`, "success");
    setTimeout(load, 800);
  } catch (error) {
    showToast(`触发失败：${error.message}`, "error");
  }
}

$("#global-refresh-btn")?.addEventListener("click", () => {
  showToast("正在刷新数据...");
  load();
});

$("#global-run-all-btn")?.addEventListener("click", () => {
  if (confirm("确定立即触发全量待办站点的自动签到批次？")) triggerRun([]);
});

/* ==========================================================================
   Batch Config Form & Operations
   ========================================================================== */

async function loadBatchConfig() {
  try {
    const res = await api("/api/config", null, "GET");
    if (!res || !res.configs) return;
    const cfgs = res.configs;
    const form = $("#batch-config-form");
    if (!form) return;

    if (form.elements["schedule_cron_time"]) form.elements["schedule_cron_time"].value = cfgs.schedule_cron_time || "08:10";
    if (form.elements["batch_timeout_s"]) form.elements["batch_timeout_s"].value = cfgs.batch_timeout_s || "2400";
    if (form.elements["site_timeout_s"]) form.elements["site_timeout_s"].value = cfgs.site_timeout_s || "60";
    if (form.elements["sso_timeout_s"]) form.elements["sso_timeout_s"].value = cfgs.sso_timeout_s || "30";
    if (form.elements["cf_wait_s"]) form.elements["cf_wait_s"].value = cfgs.cf_wait_s || "30";
    if (form.elements["connect_retries"]) form.elements["connect_retries"].value = cfgs.connect_retries || "2";

    if (form.elements["tg_notify_enabled"]) form.elements["tg_notify_enabled"].value = String(cfgs.tg_notify_enabled) === "false" ? "false" : "true";
    if (form.elements["tg_notify_policy"]) form.elements["tg_notify_policy"].value = cfgs.tg_notify_policy || "all";
    if (form.elements["tg_bot_token"]) form.elements["tg_bot_token"].value = cfgs.tg_bot_token || "";
    if (form.elements["tg_chat_id"]) form.elements["tg_chat_id"].value = cfgs.tg_chat_id || "";
  } catch (err) {
    console.error("loadBatchConfig failed:", err);
  }
}

$("#batch-config-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const saveBtn = $("#cfg-save-all-btn");
  const origHtml = saveBtn ? saveBtn.innerHTML : "";
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.innerHTML = `${uiIcon("loader", { size: 14, className: "spin" })} 正在保存并应用...`;
  }

  const formData = new FormData(form);
  const payload = {};
  for (const [k, v] of formData.entries()) {
    payload[k] = v;
  }

  try {
    const res = await api("/api/config/update", { configs: payload });
    if (res.ok) {
      showToast("批次调度与全局配置已成功保存！", "success");
      await loadBatchConfig();
    }
  } catch (err) {
    showToast(`保存配置失败: ${err.message}`, "error");
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.innerHTML = origHtml;
    }
  }
});

$("#cfg-reset-btn")?.addEventListener("click", async () => {
  showToast("正在重新拉取最新系统配置...", "info");
  await loadBatchConfig();
  showToast("已刷新为数据库最新配置！", "success");
});

$("#cfg-test-tg-btn")?.addEventListener("click", async () => {
  const form = $("#batch-config-form");
  const btn = $("#cfg-test-tg-btn");
  const token = form?.elements["tg_bot_token"]?.value?.trim() || "";
  const chatId = form?.elements["tg_chat_id"]?.value?.trim() || "";

  if (!token || !chatId) {
    showToast("请先填写 TG Bot Token 与 Chat ID", "error");
    return;
  }

  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `${uiIcon("loader", { size: 13, className: "spin" })} 正在向 Telegram 推送测试...`;

  try {
    const res = await api("/api/notify/test", { tg_bot_token: token, tg_chat_id: chatId });
    if (res.ok) {
      showToast("🎉 Telegram 测试通知已成功送达！请前往 TG 查收", "success");
    }
  } catch (err) {
    showToast(`测试推送失败: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }
});


$("#tasks-search-input")?.addEventListener("input", (e) => {
  searchQuery = e.target.value.trim();
  render();
});

$("#tasks-sort-select")?.addEventListener("change", (e) => {
  currentSort = e.target.value;
  render();
});

$("#manual-search-input")?.addEventListener("input", (e) => {
  manualSearchQuery = e.target.value.trim();
  render();
});

$("#manual-filter-select")?.addEventListener("change", (e) => {
  manualStatusFilter = e.target.value;
  render();
});

$("#manual-open-all-pending-btn")?.addEventListener("click", () => {
  const { all } = getManualTasks();
  const pendingManuals = all.filter((t) => t.status === "pending" && t.url && (t.url.startsWith("http://") || t.url.startsWith("https://")));
  if (!pendingManuals.length) {
    showToast("当前暂无待办的人工签到站点（或站点未配置有效 URL）");
    return;
  }
  if (!confirm(`确定在浏览器新标签页中一键打开全部 ${pendingManuals.length} 个待办人工签到站点？`)) return;
  pendingManuals.forEach((t) => {
    window.open(t.url, "_blank", "noopener,noreferrer");
  });
  showToast(`已在新标签页打开 ${pendingManuals.length} 个待办站点，签到完成后请点击「标记完成」`);
});

const filterChips = $("#tasks-filter-chips");
if (filterChips) {
  filterChips.addEventListener("select", (e) => {
    const raw = e.detail || "全部";
    currentFilter = raw.replace(/\s*\(\d+\)$/, "");
    render();
  });
}

const manualFilterChips = $("#manual-filter-chips");
if (manualFilterChips) {
  manualFilterChips.addEventListener("select", (e) => {
    const raw = e.detail || "全部";
    const clean = raw.replace(/\s*\(\d+\)$/, "");
    if (clean === "待办") {
      manualStatusFilter = "pending";
    } else if (clean === "已完成") {
      manualStatusFilter = "done";
    } else {
      manualStatusFilter = "all";
    }
    const select = $("#manual-filter-select");
    if (select) select.value = manualStatusFilter;
    render();
  });
}

$("#tasks-add-site-quick-btn")?.addEventListener("click", () => {
  switchView("site-manage");
});

// Init new site form tag picker when switching to site-manage or on reset
function initNewSiteTagsPicker() {
  initTagsPicker(
    "#new-site-tags-checkboxes",
    "#new-site-tags",
    "#new-site-custom-tag-input",
    "#new-site-add-tag-btn",
    ""
  );
}

$("#new-site-manage-form")?.addEventListener("reset", () => {
  setTimeout(initNewSiteTagsPicker, 10);
});

$("#new-site-manage-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitBtn = event.target.querySelector('button[type="submit"]');
  const originalHtml = submitBtn ? submitBtn.innerHTML : "";
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.innerHTML = `<ui-icon name="loader" size="13" class="spin"></ui-icon> 正在保存并注册...`;
  }
  try {
    const formData = new FormData(event.target);
    const formObj = Object.fromEntries(formData);
    await api("/api/tasks", formObj);
    event.target.reset();
    initNewSiteTagsPicker();
    await load();
    showToast(`站点「${formObj.name}」已成功配置并保存到今日待办！`, "success");
    if (formObj.checkin_mode === "manual") {
      switchView("manual");
    } else {
      switchView("tasks");
    }
  } catch (error) {
    showToast(`保存失败: ${error.message}`, "error");
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.innerHTML = originalHtml;
    }
  }
});

const credKindSelect = $("#cred-kind");
const credAccountRow = $("#cred-account-row");
const credAccountInput = $("#cred-account");
const credSecretLabel = $("#cred-secret-label");
const credSecretInput = $("#cred-secret");

const updateMainCredKindUi = () => {
  if (!credKindSelect) return;
  const k = credKindSelect.value;
  if (k === "password") {
    if (credAccountRow) credAccountRow.classList.remove("is-hidden");
    if (credAccountInput) credAccountInput.required = true;
    if (credSecretLabel) credSecretLabel.textContent = "登录密码 * (macOS Keychain 加密存储)";
    if (credSecretInput) credSecretInput.placeholder = "输入站点登录密码...";
  } else {
    if (credAccountRow) credAccountRow.classList.add("is-hidden");
    if (credAccountInput) {
      credAccountInput.required = false;
      credAccountInput.value = "";
    }
    if (credSecretLabel) {
      credSecretLabel.textContent = k === "cookie"
        ? "Cookie 字符串 * (macOS Keychain 加密存储)"
        : "API Token / Bearer 令牌 * (macOS Keychain 加密存储)";
    }
    if (credSecretInput) {
      credSecretInput.placeholder = k === "cookie"
        ? "输入完整 Cookie 字符串 (如 session=...; uid=...)..."
        : "输入 API Token / 密钥...";
    }
  }
};
if (credKindSelect) {
  credKindSelect.addEventListener("change", updateMainCredKindUi);
  updateMainCredKindUi();
}

$("#credential-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const formData = new FormData(event.target);
    const formObj = Object.fromEntries(formData);
    if (formObj.kind === "password") {
      const account = (formObj.account || "").trim();
      const secret = (formObj.secret || "").trim();
      if (!account) {
        showToast("请输入账号/用户名");
        return;
      }
      formObj.secret = JSON.stringify({ account, password: secret });
      if (!formObj.label) formObj.label = account;
    }
    delete formObj.account;
    await api("/api/credentials", formObj);
    event.target.reset();
    updateMainCredKindUi();
    await load();
    showToast("凭据已安全保存到 Keychain");
  } catch (error) { showToast(error.message); }
});

/* ==========================================================================
   Token Gate Auth
   ========================================================================== */

function showTokenGate() {
  const overlay = $("#token-gate-overlay");
  if (overlay) overlay.classList.add("is-visible");
  const input = $("#token-input");
  if (input) { input.value = ""; input.focus(); }
  const err = $("#token-error");
  if (err) err.classList.remove("is-visible");
}

function hideTokenGate() {
  const overlay = $("#token-gate-overlay");
  if (overlay) overlay.classList.remove("is-visible");
}

$("#token-submit-btn")?.addEventListener("click", async () => {
  const password = $("#token-input").value.trim();
  if (!password) return;
  try {
    const resp = await fetch(apiUrl("/api/state"), {
      cache: "no-store",
      headers: { "X-DailyCheckin-Password": password },
    });
    if (resp.ok) {
      localStorage.setItem(SESSION_KEY, password);
      hideTokenGate();
      await load();
      showToast("控制台已解锁");
    } else {
      const err = $("#token-error");
      err.classList.add("is-visible");
      err.textContent = "访问密码错误";
    }
  } catch {
    const err = $("#token-error");
    err.classList.add("is-visible");
    err.textContent = "无法连接后端 API，请确认 8765 端口已启动";
  }
});

$("#token-input")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#token-submit-btn").click();
});

// Listen to Hash Changes dynamically
window.addEventListener("hashchange", () => {
  const hash = window.location.hash.replace(/^#\/?/, "");
  if (hash && VIEW_TITLES[hash] && activeView !== hash) {
    switchView(hash);
  }
});

// Initialize Route from Hash on Page Load
const initHash = window.location.hash.replace(/^#\/?/, "");
if (initHash && VIEW_TITLES[initHash]) switchView(initHash);

// Boot Application Directly (Password gate is bypassed for personal local use)
load().then(() => {
  if (activeView === "site-manage") {
    initNewSiteTagsPicker();
  }
}).catch((error) => {
  console.log("Initial state load:", error.message);
});

setInterval(load, 10000);
