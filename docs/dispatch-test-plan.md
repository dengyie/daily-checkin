# 面板直推分发（Dispatch）测试计划与全流程覆盖文档

> 适用于 2026-11-06 上线的「pxed 面板发布任务 → 直推 Mac 节点执行」Dispatch 架构。
> 运维背景与部署清单见 Obsidian《公益站签到运维手册》2026-11-06 变更条目；本文档只讲**怎么测、测什么、在哪测**。
> 运行环境：`python3.12`（playwright 装在该版本），仓库根目录执行。

---

## 1. 被测对象与架构速览

```
pxed 控制面                                Mac 执行节点
─────────────────────────────              ─────────────────────────────
checkin_core/dispatch.py                   checkin_core/dispatch_agent.py
  ├─ DispatchStore (dispatch.db)             ├─ HTTP /dispatch（token 鉴权）
  │    dispatch_nodes / dispatch_jobs        ├─ runner 子进程 --source web
  ├─ DeliveryWorker（投递线程）              ├─ complete_task_manually（manual 作业）
  └─ CLI add-node/list-nodes/jobs            ├─ result 回调（公网 HTTPS）
checkin_core/web.py（remote 模式）           ├─ 心跳（300s）
  ├─ /api/run → 入队（不本地执行）           └─ sync-db-to-pxed.sh（原子同步）
  ├─ /api/tasks/complete → 入队
  ├─ /api/dispatch/{result,heartbeat}（节点回调）
  └─ /api/state（jobs=dispatch + nodes 在线徽标）
web/app.js + index.html + style.css（toast / 手动完成 / 节点徽标条）
```

投递通道：pxed → `ssh -R` 反向隧道（agent 自持，`DAILY_CHECKIN_DISPATCH_TUNNEL_ALIAS=pxed-direct`）→ Mac `127.0.0.1:9777`。回调走 `https://check-api.mangoqwq.com`。

---

## 2. 分层测试矩阵（全流程覆盖图）

| # | 层 | 文件 | 覆盖点 |
|---|---|---|---|
| L1 | Store 单元 | `tests/test_dispatch.py::DispatchStoreTests` | 入队/CAS 投递/终态归属与状态机/投递次数自愈/stale 回收/节点令牌哈希与常量比较/心跳在线判定/kind 白名单/legacy 列形状兼容 |
| L2 | 投递线程 | `tests/test_dispatch.py::DeliveryWorkerTests` | 有作业才投（空闲零请求）/ 失败留队列重试 / 连续失败自动 `delivery_failed` / 投递前 stale 回收 |
| L3 | 投递 HTTP | `tests/test_dispatch.py::DeliverHttpTests` | `_deliver_http` 对真 HTTP 服务：202 受理、503 busy 视为重试、连接拒绝、空 base_url、X-Node-Token 与 payload 形状 |
| L4 | CLI | `tests/test_dispatch.py::DispatchCliTests` | `add-node` 令牌仅打印一次 / `list-nodes` 不泄露令牌 / 重复注册拒绝 / `jobs` 列表 / `remove-node` |
| L5 | 面板 API | `tests/test_system_store_web.py::DispatchRemoteWebTests` | remote `/api/run` 只入队不拉子进程 / `run_all` kind / manual 完成入队且副本库不被写 / 写接口 400 拦截 / 节点回调 401·400·409 / result 真实闭环 / 心跳在线 / state 的 jobs 与 dispatch 块 / **副本库不物化**（materialize=False） |
| L6 | 全链路集成 | `DispatchRemoteWebTests::test_full_dispatch_loop_with_mock_agent` | 面板入队 → **真 HTTP 投递** → mock agent → **真 HTTP 回调 result** → done，全链无 mock 断点 |
| L7 | local 模式回归 | `tests/test_system_store_web.py::LocalModeDispatchEndpointTests` + `WebTests` | 默认本地模式零回归：无 dispatch store、节点回调 401、`/api/run` 仍走 `executor.submit` |
| L8 | Agent HTTP 面 | `tests/test_dispatch_agent.py::DispatchEndpointTests` | token 缺失/错误 401 / kind·job_id 校验 400 / 互斥锁 busy 503 / **晨批运行锁 busy 503** / 未知路径 404 / health 忙闲上报 |
| L9 | Agent 执行管线 | `tests/test_dispatch_agent.py::AcceptExecuteReportFlowTests` | 受理→后台执行→result 回传→互斥锁释放→sync 触发；runner 崩溃归因 failed；manual 作业调 `complete_task_manually` 并 JSON 落 message；payload 缺失拒绝 |
| L10 | Runner 调用契约 | `tests/test_dispatch_agent.py::RunnerInvocationTests` | `--source web`、`--only` 逗号拼接、`all` 不带 `--only`、退出码映射 done/failed、超时→`runner_timeout`、message 截断 4000、site 必须非空 sites |
| L11 | 回调重试 | `tests/test_dispatch_agent.py::ResultReportingTests` | 瞬时 500 重试后成功 / 重试耗尽不抛错（面板 reaper 兜底）/ 心跳带令牌 |
| L12 | 运行锁探测 | `tests/test_dispatch_agent.py::RunnerLockProbeTests` | flock 空闲 True / 晨批持锁 False |
| L13 | Agent 启动守卫 | `tests/test_dispatch_agent.py::AgentStartupGuardTests` | 缺 token 快速失败 / 隧道模式默认绑定 loopback / 直连模式默认绑定 mesh IP |
| L13b | 晨批静默窗口 | `tests/test_dispatch_agent.py::QuietWindowTests` | `HH:MM-HH:MM` 解析与畸形输入 / 起止边界与跨午夜 / **窗口内 /dispatch 503 reason=quiet_window** / 窗口外正常受理 —— 保证面板派发作业永远抢不走 08:10 晨批的运行锁（cron 拿锁失败会 exit 4 放弃当日批次，此窗口是唯一硬保证） |
| L14b | 多节点路由 | `tests/test_dispatch.py::SiteNodeRoutingTests` + `DispatchRemoteWebTests::test_multi_node_run_fans_out_per_node_jobs` 等 | `site_nodes` 映射 CRUD / 按节点分组保序 / 未映射回落默认节点 / **跨节点混合选择 → 按节点拆分多作业**（响应 `jobs:[{job_id,node,sites}]` + 单作业 back-compat 形状）/ manual 完成按映射路由 / CLI `assign-site/unassign-site/list-sites` |
| L14c | Runner 进程树治理 | `RunnerInvocationTests`（Popen 契约） | 会话/进程组隔离参数 / **孤儿孙进程持管道自愈**（exited-but-pipes-held → 树杀后排水，归因保持 runner 原退出码而非假超时）/ 超时树杀 `runner_timeout` / spawn 失败归因 / `DAILY_CHECKIN_AGENT_CDP` 显式下传 `--cdp`（P0 严格 attach-only） |
| L14 | 脚本契约 | `tests/test_dispatch.py::SyncScriptGuardTests` | `sync-db-to-pxed.sh` 缺 DB 环境零网络安全退出 / `launch-dispatch-agent.sh` sourcing + exec 契约 |
| L15 | 前端锚定 | `tests/test_system_store_web.py::DispatchUiContractTests` | remote toast 文案（移除「正在附着 Chrome 9222」）/ 手动完成派发分支 / 队列视图节点徽标条 / CSS 类齐备且零内联样式 |
| L16 | 部署冒烟（人工） | 见 §4 | 真实 EasyTier/隧道/Cloudflare 链路、launchd、心跳节拍、晨批共存 |

运行命令：

```bash
# 全量 dispatch 相关（L1–L15）
python3.12 -m pytest tests/test_dispatch.py tests/test_dispatch_agent.py tests/test_system_store_web.py -q

# 项目全量回归（xdist 并行，~2.5min；runner 套件已并入 tests/）
python3.12 -m pytest tests/ -q -n auto
```

---

## 3. 关键流程断言（按用户故事）

### 3.1 用户在面板点「执行签到」（site 作业）
```text
POST /api/run {sites:["伏枥"]}
  → remote: 202 {"job_id","node":"mac","mode":"remote"}        [L5]
  → dispatch_jobs: queued 行, kind=site                         [L1]
  → DeliveryWorker.sweep: POST {base_url}/dispatch              [L2,L3]
      真实通道: ssh -R 隧道 → 127.0.0.1:19777 → agent 9777      [L16 冒烟]
  → agent 鉴权/校验 → 202 accepted（mutex+晨批锁忙→503 重投）    [L8]
  → runner --source web --only 伏枥（退出码→状态, 2h 超时门）    [L10]
  → POST /api/dispatch/result {status,exit_code,message≤4000}   [L9,L11]
      归属校验（仅本节点、仅 running 可终态）→ done/failed       [L1,L5]
  → sync-db-to-pxed.sh 原子同步 → 面板任务行秒级回显             [L9,L14,L16]
  → 前端队列视图：#id/kind·node/状态 pill/exit_code/message     [L15]
```

### 3.2 面板「人工手动签到」完成（manual 作业）
```text
POST /api/tasks/complete {name,day}
  → remote: 202 {"queued":true,"job_id","node"}                 [L5]
  → 副本库 daily_tasks 不被面板直接改写（断言仍 pending）        [L5]
  → agent 调 complete_task_manually（Mac 权威库+Obsidian 勾选）  [L9]
  → 前端 toast「已发布手动完成指令至节点…」                      [L15]
```

### 3.3 节点失联 / Mac 休眠 / 崩溃韧性
```text
投递失败 → 留队列, delivery_attempts+1                          [L2,L3]
连续 240 次(≈2h@30s) → 自动 failed "delivery_failed:…"          [L2]
受理后 2h 未回传 → reaper failed "node_timeout:…"               [L1,L2]
面板 result 临时 500 → agent 指数退避重试                        [L11]
晨批持运行锁 → agent 503 busy → 面板下轮重投（不失败）           [L8,L12]
07:30-08:45 静默窗口 → 一律 503 quiet_window → 08:45 后重投     [L13b]
   （全量派发批次自限时 40min，故 07:30 前受理的作业必然 ≤08:10 结束；
    窗口结束早于晨批尾部的部分由 flock 探测兜底 → 双保险）
```

### 3.4 安全边界
```text
无/错 X-Node-Token → 401（面板回调与 agent 双向）               [L5,L8]
令牌 sha256 落库、list 不回显、add-node 仅打印一次               [L4]
finish 归属+状态机双重校验（伪造 queued/跨节点 → 409/False）     [L1,L5]
面板写接口（sites/credentials/config…）remote 全量 400 拦截      [L5]
站点名 _clean_site_name 白名单 + 禁逗号（remote 同样生效）       [L5]
pxed 对 system.db 严格只读（不物化 pending 行）                  [L5]
```

### 3.5 零回归契约
```text
默认 local 模式: /api/run → executor.submit（进程内执行）        [L7]
local 无 dispatch store, /api/dispatch/* 一律 401                [L7]
既有 WebTests 25 例全部原样通过                                   [L7]
```

### 3.6 多节点（Mac + Windows）端到端
```text
POST /api/run {sites:["hiyo"]}           # hiyo 已 assign-site → win
  → 路由: node_for_site("hiyo")="win" → 单作业 {job_id,node:"win"}   [L14b]
  → 反向隧道 19778 投递 → win agent 202 受理（令牌=win 节点）        [L3,L16]
  → Windows runner --source web --cdp http://127.0.0.1:9222 严格 attach
    （CDP 掉线 → 快速 cdp_down 归因，绝不拉起第二个 Chrome）          [L14c]
  → 结果经 ssh -L 19780 正向隧道回传 /api/dispatch/result            [L16]
    （Windows 本机代理会掐断公网 TLS → 回调必须走隧道内环）
  → done/failed 落 dispatch.db，面板作业行显示 node=win              [L5]
真机实证 2026-11-06 作业 #7: hiyo ALREADY (5.3s) exit 0 全自主闭环 ✅
```

多节点运维要点（Windows 节点 = home-win 实测踩坑记录）：
- pxed 自身 sing-box TUN 劫持 mesh 出站 → 两节点投递都走节点主动外拨的 ssh -R 反向隧道（Mac:19777 / win:19778，各自独立端口，互不抢占）。
- Windows 的 clash 会掐断节点→公网 HTTPS 的 TLS（`SSL: UNEXPECTED_EOF_WHILE_READING`）→ 回调必须走 `DAILY_CHECKIN_CALLBACK_TUNNEL_ALIAS` 正向隧道（19780），agent 自动把 callback URL 改写为隧道内环。
- Windows runner 退出后孤儿孙进程持有管道 → `_run_sites` 检测 exited-but-pipes-held 并树杀排水（Windows: `taskkill /F /T`，POSIX: `killpg`）。
- Windows 反向隧道在 clash fake-ip DNS 下会周期性抖动重连（~17s），投递/回调靠隧道循环自愈 + result 重试覆盖，功能不受影响；后续可在 Windows ssh config 固定 IP 优化。
- 节点晨批静默窗口：Mac 与 win 都为 08:10 cron 设 `07:30-08:45`。
- agent 文件日志 `DAILY_CHECKIN_AGENT_LOG_FILE`（Windows 无控制台，PS 5.1 transcript 抓不到 native stdout）。

### 3.7 统一任务清单与多平台并行扇出（2026-11-06 上线）

**设计原则（用户决策）**：前端完全统一，执行平台差异只是任务上的一个渠道参数 `node`；一次触发可让所有平台并行签到。

```text
/api/state（remote 模式）
  → 主库快照 + 归属默认节点 mac（每行 t.node = "mac"）
  → DAILY_CHECKIN_NODE_REPLICAS="win=/data/.../system-win.db" 只读快照合并
    （store.read_only_tasks，immutable 连接，绝不触碰副本文件）
  → 每行带执行渠道 t.node；前端 tile 显示渠道徽标，仍是一个统一清单

POST /api/run {} 或 {"all":true}（run_all 全站扇出）
  → 按 registered_nodes() 为每个注册节点各入队一个 all 作业（并行直推）
  → 响应 {"mode":"remote","jobs":[{job_id,node}…]}（单作业响应保持兼容）
  → 每节点 agent 各自执行本节点站点（site_nodes 路由 hiyo/relayfor→win），
     各自完成后回调 + 副本同步 → 面板统一清单刷新
```

**结果回调竞态修复（2026-11-06，L5 强化）**：
- 原 `sweep()` 先 `_deliver_fn` 后 `mark_delivered`，极快节点的 result 回调会先于
  queued→running 状态落地 → `finish()` 409，作业永久卡 running（全闭环测试实测复现）。
- 修复为 **claim-then-push**：入列作业先 CAS queued→running 再推送；推送未受理则
  `revert_delivery` 回队（保留 delivery_attempts 累计，不破坏重试耗尽自动失败）；
- `finish()` 同时接受 queued/running 作为合法来源态（归属节点能拿到 job_id 即已受理
  推送的证明，归属校验 + 恰好一次终态写入不变）。
- 真机回归：run_all 扇出作业 #23(all,mac)#24(all,win) 同刻 done exit=0，无 409。

**pxed 部署盲区注记**：pxed 真实运行目录是 `/data/daily-checkin/app/`（dc-web cwd），
代码必须 scp 到 `/data/daily-checkin/app/checkin_core/` 与 `/data/daily-checkin/app/web/`；
`/data/daily-checkin/checkin_core/` 只是暂存副本，改错目录不生效。supervisor
`supervisor-daily-checkin.conf` 在 `/personal/pxed/`，env 数值行必须带 `export` 前缀。

### 3.8 权威写 dispatch 化（2026-11-07 上线）

**背景**：面板 system.db 是 Mac 权威库的严格只读副本，早前「remote 全量 400 拒绝」导致面板上
新增站点 / 改配置 / 删站点 / 加凭据 / 改余额全部报错（用户实测「删除失败」）。改为：所有落权威库
的写操作 dispatch 到持有数据的节点执行，节点完成后副本同步自然回传。

```text
面板写操作 → _dispatch_write(kind, payload, node_hint)
  ├─ kind 由 site_nodes/default_node 路由（站点 site 作业同源判定），无路由走 default_node
  ├─ enqueue → 202 {"ok":true,"queued":true,"job_id","node","mode":"remote"}
  └─ 节点 agent _run_config_write(kind, body) 本地执行 + summary 回传 + _sync_db()

作业 kind 清单（VALID_JOB_KINDS，两个模块同步维护）:
  删除站点 delete_site（级联：task 行 + site 配置 + keychain 凭据 + 墓碑）
  新增/更新站点 add_site / update_site（白名单字段 + num_keys 边界校验）
  凭据写/删 set_credential（token/cookie/password, make_ref）/ delete_credential
  系统配置/余额 update_config（与 web handler 同边界）/ update_balance（math.isfinite）
REMOTE_BLOCKED_WRITE_PATHS 精简为仅 /api/sync/obsidian（长连接投影，不适合异步作业）。
```

**前端等待回显（trackDispatchJob）**：写操作响应含 `queued` 时（而不是提示「已提交等同步」
后靠 10s 空闲轮询刷新）— 每 2s 轮询 `/api/state` 直到该 job 终态（done/failed/
delivery_failed），期间 load() 刷新卡片；20s 超时兜底提示「请稍后刷新确认」。实测删除站点
16s 闭环、手动完成 2s 内卡片翻转。触点：新增站点 / 站点配置 / 凭据增删 ×2 / config /
balance / 删除站点 / 手动完成。

**agent 执行兜底（死锁修复）**：`execute_job` finally 中 `_post_result/_sync_db` 异常不再
跳过 `_job_mutex.release()`（曾致节点永久 503 busy）— report 包 try/except 仅记日志，
release 移入嵌套 finally 且带 `if locked()` 守卫（直调测试未 acquire 时不重复释放）。
[L5 扩展, L9 扩展]（tests/test_dispatch_agent.py + test_system_store_web.py 新增 12 例）

### 3.9 日期滚动物化（2026-11-07 缺陷修复）

**现象（用户实测）**：2026-11-06→07 滚动后 check.mangoqwq.com/#/tasks 显示 0 任务，
持续到 08:10 晨批前。**根因**：面板只读副本不能 materialize_day；Mac 晨批（08:10）前也没人
物化当天 → 00:00~08:10 统一清单为空（win 副本由 runner 启动即物化 + 批后 sync，不受影响）。

```text
agent _heartbeat_loop（300s 节拍）:
  today != last_materialized_day → store_from_env().materialize_day(today)
  → _sync_db() 立即推送面板副本（不等晨批）                       [L14]
  每个自然日仅物化一次（last_materialized_day 单调推进；循环内局部
  import store_from_env，避免模块级依赖）.
  materialize/sync 异常只记日志不破坏心跳；下轮 300s 再试.
```
真机实证：agent 00:54:18 重启 → 00:59:21 `materialized daily tasks for 2026-11-07` →
00:59:29 `panel DB sync ok` → 面板 /api/state `tasks:114, day:2026-11-07`。✅

---

## 4. 部署后真机冒烟清单（L16，2026-11-06 已执行通过）

以下场景依赖真实网络/系统组件（EasyTier、ssh 隧道、Cloudflare、launchd、Chrome 9222），单测无法覆盖，按序人工复跑：

1. **面板直推首签**：`check.mangoqwq.com` 触发任一未签站点 → toast「已直推节点 mac」→ 队列视图 queued→running→done（exit 0），消息含 `SUMMARY OK=1`。✅ 2026-11-06 伏枥实测
2. **原子同步回显**：作业 done 后 ≤10s，面板当日任务行翻 done。✅
3. **节点在线徽标**：agent 启动后 300s 心跳准点，队列视图「节点 mac · 在线」。✅ 04:34:37
4. **写保护**：面板改配置 → 400「看板模式下该操作需在执行节点本地完成」。✅
5. **休眠/离线重投**：Mac 合盖触发作业 → 面板作业停留 queued + 节点离线 → 唤醒后 ≤30s 自动补投执行。（由 L2/L3 单测 + 隧道 KeepAlive 保证，可随时复演）
6. **晨批共存**：08:10 批次运行中触发作业 → agent 503 busy → 批次结束后 ≤30s 补投。（由 L8/L12 单测保证）
7. **launchd 自愈**：`launchctl kickstart -k gui/$(id -u)/com.mango.daily-checkin-agent` → agent 秒级重启、隧道重建、`/health` 200。
8. **统一清单渠道徽标**：pxed `/api/state` 返回 116 任务（mac 114 + win 2），win 行（hiyo/relayfor）随副本每日同步（`run-checkin-win.ps1` 批后 + agent 回调后 `sync-db-to-panel.ps1` 实弹验证 ok）。✅ 2026-11-06
9. **多平台并行扇出**：面板 run_all → 响应 `{"jobs":[{23,"mac"},{24,"win"}]}` → 双节点同刻并行执行 → 均 done exit=0 回调闭环。✅ 2026-11-06 实测
10. **权威写 dispatch 闭环**：面板删站点 → delete_site 作业 → 节点删 task+配置+keychain+墓碑 → 副本同步 → 卡片 16s 内消失。✅ 2026-11-07 实测
11. **config 双跳 E2E**：面板改 batch_timeout_s 1500 → 节点执行 → 副本回传 → 面板读回 1500 → 再改回 2400 还原。✅ 2026-11-07 实测
12. **日期滚动物化**：跨 00:00 后 ≤5min（首个心跳）agent 物化当日 + sync → 面板任务非空。✅ 2026-11-07 00:59:21 实测

---

## 5. 测试基建约定（为什么这样测）

> **2026-11-07 测试工程重构**：legacy `test_stealth_checkin_m1.py`（10k 行单文件、430 例）
> 已按域拆入 `tests/`（53 类 1:1 迁移、862 例全绿后删除）；pytest 成为唯一门面。

- **统一 pytest 门面**：`pyproject.toml` 定义 testpaths/timeout(120s)/markers；全量命令
  `python3.12 -m pytest tests/ -q -n auto`（xdist 并行 ~2.5min，原串行 ~13min）。
  `requirements-dev.txt` 收纳 pytest/xdist/timeout（CI 安装）。
- **共享测试基建（`tests/_support.py`）**：runner 模块加载器 `load_mod(force)`/
  `ensure_runner_loaded()`、env knob 清洗 `clean_env()`/`CleanEnvMixin`、页面替身
  `FakePage`/`FakeLoc`/`make_fake_selector_page`。新测试**必须**从这里 import，禁止再复制粘贴
  （迁移前的 5 份 load_mod / 110 处手写 env save-restore 已收敛）。根 `conftest.py` 保证
  任意 cwd 可运行；`tests/__init__.py` 使 `from tests._support import …` 全平台可导入。
- **runner 标记与 Windows 范围**：站点流/yaml/oauth 五个文件带 `pytestmark = [pytest.mark.runner]`
  （274 例）；Windows CI 只跑 `-m "not runner"`（588 例），macOS 跑全量。legacy 中 4 处依赖
  执行顺序的 `__import__("stealth_checkin_runner")` 已改为 `ensure_runner_loaded()`。
- **ops 脚本契约测试（`tests/test_ops_scripts.py`）**：晨批/同步/派发链路的 6 个脚本
  （launch-daily-batch.sh、sync-db-to-pxed.sh、sync-db-to-panel.ps1、run-checkin-win.ps1、
  两个 .bat + win 派发三件套）做源码契约断言（原子 mv、wal_checkpoint、win 副本路径、
  token 不硬编码）——这些脚本此前零覆盖，而它们正是 08:10 晨批 HA 链路。
- **无前端 JS 测试框架**：沿用仓库「源码锚定式」约定（同 `test_web_toast.py`），对 `web/` 静态资源做关键回归点断言；`style=` 内联禁令由全局守卫测试兜底（严格 CSP）。
- **绝不触真实基础设施**：agent 测试中 runner 在 `subprocess.run` 边界打桩、`_sync_db` 一律打桩（否则会真跑 scp！）；面板一律用本地 mock HTTP server，回调/投递走真实 socket，保证协议层无 mock 断点。
- **互斥锁所有权**：`execute_job` 契约是「调用方先持有 `_job_mutex`，finally 释放」。HTTP 用例走 handler（真实持锁路径）；直调用例先 `acquire(blocking=False)` 模拟 handler 所有权。
- **测试状态隔离**：所有临时 DB/server 用 `TemporaryDirectory` + 动态端口；`DAILY_CHECKIN_*` 环境变量经 `CleanEnvMixin`/`clean_env()` 严格备份还原，防止污染同 worker 其他套件（xdist 每 worker 独立进程，天然隔离 sys.modules 缓存）。
- **防抖护栏**：per-test 120s watchdog（挂死 2min 内报败，不烧 CI 15min timeout）；不引入 pytest-randomly（runner sys.modules 缓存有顺序语义，随机序是假保护）。
- **pxed 侧 CLI 调试易错点**：在 pxed 上手工跑 `python -m checkin_core.dispatch` 必须先 `. /data/daily-checkin/env`，否则落库到 `~/.hermes/checkin/dispatch.db` 游离文件（2026-11-06 部署时实际踩过）。

## 6. 已知不覆盖项（显式声明）

- 真实 EasyTier 断连/重连的端到端时序（由 ssh 隧道层与 L2 重试语义覆盖到可接受程度）。
- Cloudflare Tunnel 故障演练（回调走公网 HTTPS 或节点隧道，重试语义在 L11 覆盖，链路级故障由面板 reaper 兜底）。
- launchd KeepAlive/ThrottleInterval 系统行为（L16 冒烟项 7 覆盖）。
- Windows 侧 msvcrt.locking 的真实并发行为（POSIX flock 已实测；win 路径以源码锚定 + 节点真机冒烟覆盖）。
- ops 脚本仅做源码契约断言（`tests/test_ops_scripts.py`），不真实执行 scp/ssh/mv 网络路径——原子协议语义由「.incoming 临时副本 + 远端 mv」结构保证，实弹验证在冒烟清单 #2/#8。
- 双节点同时争抢**同一节点**作业的 CAS 竞态已由 L1 覆盖；跨节点争抢因作业按节点定向而不存在，接入第三个节点时无需新增用例（仅需新 add-node + assign-site + 独立隧道端口）。
