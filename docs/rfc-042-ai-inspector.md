# RFC-042: 每日签到 AI 智能质检与自愈链路 (AI Inspector & Auto-Remediation)

## 1. 概述与背景 (Overview)
自动化签到定时批次执行完毕后，部分站点可能因 Cloudflare Turnstile 竞争、OAuth 会话过期、上游站长关闭签到或 Cloudflare Tunnel 离线等各种因素失败。
本规范定义每日签到批次完成后的 **AI 智能质检与自愈链路 (AI Inspector & Auto-Remediation)**，在批次结束后对失败任务进行深度分析、定性归因，并依据严格置信度执行安全自愈（单次定向退避重试或自动抑制）。

## 2. 核心架构与设计规范 (Architecture)

### 2.1 依赖关系分层 (Strict Layering)
- **Domain (纯领域契约)**: `checkin_core/ai_inspector_models.py` 定义分类枚举 `FailureCategory`、站点诊断结果 `SiteInspectionResult`、总报告 `InspectionReport`。
- **Domain Security (凭据清洗)**: `checkin_core/ai_sanitizer.py` 提供 `EvidenceSanitizer`，在数据流出系统前以不可逆方式抹除 Token、密码、Cookie、邮箱。
- **Infrastructure (存储与状态机)**: `checkin_core/store.py` 扩充 `ai_inspections` 表及相关重试计数原子操作。
- **Application (分析引擎与调度)**:
  - `checkin_core/ai_inspector.py`: 调用兼容 OpenAI 格式的轻量 LLM，以结构化 JSON Schema 约束输出，设 15s 硬超时熔断。
  - `checkin_core/remediation_engine.py`: 评估置信度与重试配额，非阻塞异步调度子进程靶向重试。
- **Presentation (通知与前端展现)**:
  - `checkin_core/notify.py`: 渲染 Telegram 诊断简报 Markdown。
  - `stealth_checkin_runner.py`: 在主批次完成后作为安全 Hook 触发。

### 2.2 故障分类与自愈门禁 (Taxonomy & Arbitration)
1. `transient_flake`: 偶发瞬态（Turnstile、PoW 竞争、网关 504）。置信度 $\ge 0.85$ 时触发延迟 60~120s 自动重试（单日单站上限 1 次）。
2. `auth_expired`: 鉴权失效（未登录、密码错误、401）。不重试，生成高优先级人工待办。
3. `upstream_disabled`: 上游关停（`checkin_enabled=false`、个人中心无签到）。置信度 $\ge 0.90$ 时自动标记抑制。
4. `upstream_offline`: 上游完全不可达（CF 530、SSL 握手超时、DNS 失败）。不重试，累加健康失败计数。
5. `selector_drift`: 前端改版导致选择器失效。生成选择器更新提醒。
6. `unknown`: 无法确诊。仅记录并回退。

### 2.3 核心生产红线 (P0 Invariants)
- **零凭据外泄**: 发送至 LLM 的上下文必须经过 `EvidenceSanitizer` 处理，绝对不允许出现真实账号名、密码、Cookie 或私密 Token。
- **零阻塞阻断**: AI API 异常、Key 耗尽或网络超时（<=15s）均静默降级，绝不影响主流程退出码。
- **防风暴配额**: 单批次重试目标上限 3 个，单日单站自动重试上限 1 次，严禁死循环。
- **高可用容灾**: 支持多渠道配置与级联降级（Multi-Channel Cascade Degradation），主渠道不可用时自动无感切换备用渠道。

## 3. 多渠道容灾与级联降级规范 (Multi-Channel Fallback Architecture)

### 3.1 渠道契约与优先级链 (Channel Waterfall Contract)
系统支持配置有序的 AI 质检渠道列表 `channels: list[AIChannelConfig]`：
1. **主用渠道 (Primary)**: 默认执行第一优先级的 LLM 端点（如性价比高、延迟低的 DeepSeek / SiliconFlow / NewAPI）。
2. **备用降级渠道 (Fallback / Secondary)**: 当主用渠道遭遇以下异常时自动触发级联降级：
   - HTTP 429 (Rate Limit / 并发与速率限制)
   - HTTP 402 / 400 (配额不足 / Quota Exceeded / 欠费)
   - HTTP 500 / 502 / 503 / 504 (服务端内部错误或网关离线)
   - 网络超时 (Connection / Read Timeout >= timeout_s)
   - 非法或畸形响应 (Empty choices / Malformed JSON / DNS 污染)
3. **全链路容灾安全退出 (Exhaustion Safe Fallback)**:
   - 若全部候选渠道均失败，系统优雅捕获异常并记录告警日志，不阻塞签到批次退出码。

### 3.2 质检报告容灾遥测 (Degradation Telemetry)
在质检产物 `InspectionReport` 与数据库 `ai_inspections` 表中，记录本次调用的实际生效渠道标识与降级链路：
- `channel_name`: 实际成功产出报告的渠道名称（例如 `primary` 或 `fallback-backup`）。
- `fallback_chain`: 发生降级时所跳过的失败渠道列表与对应错误摘要。
- `model`: 数据库持久化格式标记为 `渠道名称:模型标识`（例如 `Primary:deepseek-chat` 或 `Fallback:gpt-4o-mini`），供前端面板直观审计。
