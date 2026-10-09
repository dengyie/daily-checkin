# RFC-043: 签到平台账户余额与额度自动采集追踪设计文档 (Account Balance Tracking)

## 1. 概述与可行性结论 (Executive Summary & Feasibility)

### 1.1 核心结论：改动是否方便？
**答：非常方便，而且当前项目的架构极其契合该功能的接入。**

当前项目已具备实现账户余额/额度自动采集的三大核心优势：
1. **已有原生浏览器上下文 (No Extra Cost)**: 签到执行时，Playwright 已经附着在 headed Chrome 9222 上，并且绝大多数站点（特别是 45+ 个 New API 站）签到的落脚点本身就是个人中心（`/profile`、`/console` 等）。页面已经处于渲染完成状态，获取余额无需额外的登录、握手或导航开销。
2. **结构化契约与证据链完备 (Seamless Evidence Model)**: 项目目前拥有高度规范的 `CheckinResult`、`ActionEvidence`、`ConfirmationEvidence` 及 SQLite `run_items.evidence_json`，余额数据可以作为 `BalanceEvidence` 无缝挂载，不需要重构既有状态机。
3. **站点类型高度集中收敛 (High ROI)**:
   - 全网 106 个站点中，**45 个站点为标准 New API / One API 派系**，接口协议统一（`GET /api/user/self`），一次适配即可覆盖全量近 50% 的站点。
   - GLaDOS、云智等特殊站点已内置了专用的 RPC / fetch 逻辑，扩展只需几行代码。

---

## 2. 站点类型与余额采集途径调研 (Data Extraction Topology)

通过对 `sites.yaml` 和适配器运行机制的深入调研，目前站点的余额获取可划分为四大途径：

| 站点类别 | 站点数量 | 获取途径 | 技术实现细节 |
| :--- | :--- | :--- | :--- |
| **New API / One API 派系** (`newapi_profile`) | 45 站 | **页面内同源 fetch** 或 **DOM 提取** | 1. 调同源 `GET /api/user/self`，返回 `{"data": {"quota": 500000}}`（500,000 = $1.00，部分站点直接返回金额或点数）。<br>2. DOM 兜底：抓取 `.profile-card` 或包含 `当前余额` 的节点。 |
| **GLaDOS 控制台** (`glados`) | 1 站 | **专属 API JSON** | runner 中已有 `_glados_fetch_points(page)`，当前已能获取打卡积分历史，可进一步从接口返回中提取用户总点数与 VIP 剩余天数。 |
| **云智手机** (`yunzhi` / `yunzhi_win`) | 2 站 | **RPC 双签名接口** | 现有 `_yunzhi_api` 调 `/api/benefit/user/benefit`，已能拿到 `cloudDevices[].expireTime`（云机到期时间）与剩余空间卡数量。 |
| **通用/其他 Web 签到** (`browser` / 各种自定义) | 58 站 | **声明式选择器 / 文本正则** | 签到成功后，通过 `sites.yaml` 可选配置 `balance_selector`（如 `.balance`、`.credit`）或正则 `(?:余额|可用额度|积分|Points)\s*[:：]?\s*([¥$€]?\s*[\d,]+(?:\.\d+)?|\d+)` 提取。 |

---

## 3. 架构设计与改动方案 (Architecture & Impact Analysis)

### 3.1 核心原则与生产红线 (P0 Invariants)
1. **绝对只读，零业务副作用**: 余额抽取操作必须是纯 `GET` 或纯 DOM 读取，严禁点击任何可能消耗额度、兑换或重置的按钮。
2. **非阻塞与解耦 (Non-blocking Degradation)**: 余额抽取属于“观测增强”功能。若因网络波动、接口变更导致余额采集超时（建议硬限制 3s），**绝对不能使原本成功的签到判定变为 FAIL**。
3. **性能零损耗**: 绝不在签到主流程外新增额外的冷启动浏览器 Tab 或全页面重载；全部复用签到完成后的存量页面上下文。

---

### 3.2 领域契约扩展 (`stealth_checkin_runner.py`)

#### 1. 定义余额证据结构 (`BalanceEvidence`)
```python
@dataclass
class BalanceEvidence:
    """账户余额与额度观测证据."""
    amount: float | None = None          # 标准化数值，如 12.50
    raw_text: str = ""                   # 原始文本，如 "$12.50" 或 "1250000 quota"
    currency: str = "USD"                # 货币/单位: "USD", "CNY", "POINTS", "DAYS"
    quota_raw: int | None = None         # New API 原始配额值 (500000 = $1)
    captured_at: str = ""                # 采集时间 (ISO8601)
    source: str = "api"                  # "api" | "dom" | "none"
```

#### 2. `CheckinResult` 扩充
```python
@dataclass
class CheckinResult:
    ...
    balance: BalanceEvidence | None = None  # 新增余额观测字段
```

#### 3. 抽取钩子 (`extract_balance_safely`)
在 `legacy_checkin_on_page` 流程的尾部，当站点判定为 `OK` 或 `ALREADY` 时，执行安全采集：
```python
async def extract_balance_safely(page, adapter: SiteAdapter) -> BalanceEvidence | None:
    try:
        # 硬超时 3 秒熔断，超时直接返回 None，不影响签到主状态
        return await asyncio.wait_for(_do_extract_balance(page, adapter), timeout=3.0)
    except Exception as e:
        return None
```

针对 New API 的提取实现极其简单轻量：
```python
async def _extract_newapi_balance(page) -> BalanceEvidence | None:
    res = await page.evaluate("""async () => {
        try {
            const r = await fetch('/api/user/self', { credentials: 'include' });
            if (!r.ok) return null;
            const j = await r.json();
            return j && j.data ? j.data.quota : null;
        } catch (e) { return null; }
    }""")
    if res is not None:
        quota = int(res)
        dollars = round(quota / 500000.0, 4)
        return BalanceEvidence(
            amount=dollars,
            raw_text=f"${dollars:.2f}",
            currency="USD",
            quota_raw=quota,
            captured_at=datetime.now().isoformat(timespec="seconds"),
            source="api",
        )
    return None
```

---

### 3.3 数据持久化与存储设计 (`checkin_core/store.py` & SQLite)

为了兼顾历史走势分析与高性能大盘查询，推荐采用两级持久化：

#### 1. `sites` 实体表扩展（最新快照）
在 `sites` 表增加 3 个轻量字段，提供大盘列表极速读取：
- `last_balance` (REAL): 最近一次采集到的数值
- `last_balance_text` (TEXT): 格式化展示文本（如 `"$15.20"`、`"340 积分"`）
- `balance_updated_at` (TEXT): 最近更新时间戳

#### 2. `account_balances` 时序流水表（历史走势）
用于绘制资产变化趋势折线图、计算今日签到收益增量：
```sql
CREATE TABLE IF NOT EXISTS account_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    amount REAL,
    raw_text TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'USD',
    source TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_balances_site_day ON account_balances(site_id, day);
```

#### 3. 证据链回填
在 `add_run_item` 时，将 `result.balance` 同步写入 `evidence_json["balance"]`，保留完整的司法级审计轨迹。

---

### 3.4 API 控制面设计 (`checkin_core/web.py`)

1. **`/api/state` 扩展**:
   - `tasks` 列表中返回每个站点的最新余额信息 (`task.last_balance_text`, `task.last_balance`)。
2. **新增 `/api/balances/summary` (可选聚合接口)**:
   - 返回全网资产概览：美元总计折算、人民币总计折算、今日签到新增收益汇总。
3. **新增 `/api/balances/history?site_id=...`**:
   - 查询特定站点的历史额度变化曲线（过去 30 天）。

---

### 3.5 Web 控制台交互设计 (`web/app.js` & `style.css`)

1. **Overview 概览看板**:
   - 增加 KPI 卡片：`[全网账户总估值]` 与 `[今日打卡收益]`。
2. **任务卡片 / 列表视图 (Task Card / Table)**:
   - 站点卡片右下角或操作栏增加余额徽章，例如：`[ 💰 $24.50 ]`；
   - 支持快捷排序：按“余额从低到高”（方便标记快见底的站点）或“按余额从高到低”。
3. **站点详情抽屉 (Site Drawer)**:
   - 新增“资产与余额” Tab，展示当前余额明细、计费比例换算以及近期打卡收益明细。

---

## 4. 分阶段落地路线图 (Implementation Plan)

- [ ] **Phase 1: 基础契约与存储层建设 (底层无感扩展)**
  - 实现 `BalanceEvidence` 数据类定义及序列化。
  - SQLite 数据库升版：补充 `sites` 余额字段与 `account_balances` 表。
  - 编写单测验证存储读写与边界情况。
- [ ] **Phase 2: 核心主力站点采集器接入 (覆盖 80% 资产)**
  - 接入 `newapi_profile` 通用 New API `/api/user/self` 提取器（直接覆盖 45+ 站）。
  - 接入 `glados` 总积分采集。
  - 接入 `yunzhi` 云机有效期与剩余卡采集。
  - 跑批验证采集成功率与耗时（确保耗时增量 < 5%）。
- [ ] **Phase 3: 泛站点正则与声明式 YAML 支持**
  - 在 `sites.yaml` 中支持声明 `balance_selector` 与 `balance_regex`。
  - 针对通用 `browser` 适配器做兜底 DOM 文本提取。
- [ ] **Phase 4: Web 控制台与趋势大盘可视化**
  - 前端任务卡片挂载余额徽章。
  - 抽屉展示历史走势与资产看板。
