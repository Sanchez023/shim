# ADR-0010: 依赖单向化与端口化（含 turn loop 收敛的边界）

- 状态: Accepted（D1/D2/D3/D5/D6 已落地；D4 的压缩收敛与重试策略待办）
- 日期: 2026-11-18
- 决策者: OpenHarness maintainers
- 相关: ADR-0002, ADR-0005, ADR-0006, ADR-0008, ADR-0009, ADR-0011

## Context

ADR-0001/0005/0006 已经把 seam、事件日志、子 Agent 后端、turn 拆分做完，但**实际依赖方向**仍是双向的。实测（import 行数）：

- `services → engine` **29 处 / 20 个文件**；`engine → services` 10 处 —— 二者互指；
- `api → engine.messages`：`api/client.py:31`、`api/openai_client.py:32`、`api/codex_client.py:32`，导致 `api` 无法独立测试与独立安装；
- `tools → coordinator/swarm`：`tools/agent_tool.py:9-13`、`send_message_tool.py:9-10`、`team_create_tool.py:7`、`team_delete_tool.py:7` —— 叶子工具里塞了编排知识；
- 呈现层被下层反向使用：`cli_commands/main.py:268 → ui.app`、`autopilot/execution.py:651 → ui.runtime.build_runtime`、`cli_preview.py:396 → ui.runtime._resolve_api_client_from_settings`（跨包私有名）、`ohmo → ui` 4 处；
- 同层横穿：`plugins ⇄ mcp`；`config → permissions/hooks/mcp`；
- 疑似死代码：`state/`、`bridge/` —— 实测均有入边（`state` 被 `ui` / `commands` 使用，`bridge` 被 `ui` / `commands` 使用），需在 P1 按"零消费者"重新判定，不能直接删。

turn loop 侧的现状（ADR-0006 落地后的残留）：

- `engine/query.py`(240) 已无循环，主体是 `QueryContext`、`MaxTurnsExceeded`、`run_query`，另有一组仍被 `turn_executor.py:47-55` / `step_runner.py:74` 懒加载的辅助函数，以及 `query.py:47-56` 的 **10 个兼容别名（9 个零消费者）**（注释称 swarm workers 仍在用，实测无消费者）；
- `QueryContext` 构造写了两遍：`query_engine.py:804` 与 `:883`（字段只差 `max_turns`）；
- `step_runner.py:80` 收了 `turn_count` 又 `del turn_count`，`max_turns` 同时存在于 `QueryEngine._max_turns`（默认 8）与 `QueryContext.max_turns`（默认 200）；
- 压缩有三套（`turn_executor` auto-compact、`context_governor.on_request_error`、`services/compact`），重试有两套（`turn_executor.py:152-178` 的 token-limit 降级、`api/transport.py` 的流式重试）；
- 同一事实双发射：`step_runner.py:137-144` 既 `emit_event` 又 `yield StreamEvent`。

### 缓存侧的现状（重构的硬约束）

当前命中率依赖五条字节稳定机制：

| 机制 | 位置 | 作用 |
|---|---|---|
| stable / live 切分 | `prompts/context.py:165-186` | cwd、日期、分支走 turn_context，不进缓存前缀 |
| section 全序 | `prompts/composer.py:45-48` | 按 `(order, name)` 排序，改装配代码不影响字节 |
| 工具 append-only | `query_engine.py:344-350` | 新工具只能追加；重新排序会改每个后续请求的 tool 前缀 |
| 指纹归因 | `engine/cache_diagnostics.py` | `stable_sha` / `system_sha` / `tools_sha` / 每 section 与 message hash，可定位"第一个变了的 section" |
| 结果预算 | `engine/cache_budget.py` | 目标 95% 命中，超预算的 tool result 截成 1200 字符预览 |

Provider 语义不同：Anthropic 走 `cache_control` 断点（`api/client.py:216-285`，前缀 ≥2000 字符才挂、最多 4 个）；DeepSeek / OpenAI 是自动前缀缓存、没有断点（`api/capabilities.py:17`）。对后者，"字节稳定"是唯一变量，没有断点可以补救。

按相位预估：

| 相位 | 缓存影响 | 说明 |
|---|---|---|
| P1 死代码 / 别名 / marker | 零 | 没有一行进 prompt |
| P2 `domain/messages.py` 下移 | 一次性失效 | 字段顺序与 `model_dump` 语义不变则字节不变；变了则每个会话冷启动一次 |
| P2 `build_runtime` 搬家 | **风险 A** | stable/live 切分就住在被搬的 310 行里 |
| P2 端口化 | 零 | 只改调用方式，不改装配结果 |
| P3 `QueryContext` 单点构造 | 低风险，有一处坑 | `query_engine.py:804` 与 `:883` 的字段只差 `max_turns`；合并必须显式传参，否则静默丢覆盖 |
| P3 压缩策略收敛 | **风险 B（最大）** | 压缩 = 主动重写历史 = 必然 miss |
| P3 messages SoT 切换 | **风险 C** | 恢复路径若产出不同字节的 message 列表，续接轮全 miss |
| P4 记忆收敛单写者 | 中性偏正，有条件 | 见 D7 |
| P4 工具注册表收编 | 一次性或持续 | 取决于顺序是否仍确定 |
| P4 事件 / UI / channel / alias | 零 | 都不进 prompt |
| P5 拆 settings | 一次性 | 仅当 profile section 文案变化才失效 |
| P5 拆 feishu / ohmo | 零 | 不进 prompt |

## Decision

### D1. 目标分层与允许边

```
L5 呈现    ui / cli_commands / ohmo / autopilot      （只被 CLI 入口使用）
L4 组合    services（接入、装配、子代理后端、存储）
L3 循环    engine（唯一状态真源）
L2 能力    tools / hooks / plugins / mcp / channels  （只依赖 L1 端口与 L0 类型）
L1 底座    api / permissions / config / auth / sandbox / utils
L0 域类型  domain（messages / usage / errors，纯数据、无行为、无 IO）
```

允许的边只有 `Ln → Lm (m < n)`。跨层调用一律走端口注入，不允许直接 import 上层实现。

### D2. 三处解环

1. **`api → engine.messages`**：把 `ConversationMessage` / `TextBlock` / `ImageBlock` / `assistant_message_from_api` 下移到 `openharness/domain/messages.py`，`api` 与 `engine` 都只依赖 L0。这是收益最大的一刀：解完 `api` 才可独立测试。
2. **`engine ⇄ services`**：新增 `engine/ports.py`，定义 `EventSink` / `MemoryPort` / `CompactPort` / `SubagentPort` / `SessionStorePort`。engine 只依赖端口，实现留在 `services/`，由 L4 装配注入。验收指标：`services → engine` 从 29 → 0，`engine → services` 从 10 → 0。
3. **`tools → coordinator/swarm`**：`agent_tool` 只依赖 `AgentRunPort`，后端选择上移到 `services/agent_backends` 注册表（ADR-0009 D3 的落点）。

另外两处横穿按同一模式收：`plugins ⇄ mcp` 抽 `McpPort`；`config → permissions/hooks/mcp` 把子 schema 拆到各自包，`config` 只保留通用 schema 与 IO（见 D4）。

> **进度（2026-11-18）**：最终上行边——`api → engine` 0，`engine → services` 0，`tools → services` 0，`tools → coordinator/swarm` 0，`runtime → engine` 0，`runtime → services` 0，`channels → engine` 0。`services → engine` 仅剩 3 条，全部在 `services/assembly.py`（L4→L3 组合装配，D1 允许）。端口（`EventLogPort` / `SessionAppendPort` / `MemoryPort` / `CompactPort` / `AgentRunPort` / `CronPort` / `LspPort` / `CodexSubagentPort`）定义在 `runtime/seams`，实现留在 `services` 并由 `build_runtime` 注入；`runtime/seams` 不再 import `services`，默认端口工厂集中在 `openharness.tool_ports`。

### D3. 呈现层去反向依赖

`ui/runtime.py:320-629` 的 `build_runtime`（310 行、依赖 14 个包）搬进 `services/assembly.py`；`RuntimeBundle`、`start_runtime` / `close_runtime` 与 `_resolve_api_client_from_settings` 同批移动，`ui/runtime.py` 退化为薄适配器。之后 `autopilot` / `ohmo` / `cli_commands` / `cli_preview` 一律不得 import `ui.*`。

搬家必须是**零语义 diff**：`prompts/context.py:165-186` 的 stable/live 切分（cwd、日期、分支走 turn context，不进缓存前缀）必须原样保留，不得把 live 字段顺手并入 stable section。`runtime/assembly.py` 是 `CapabilityAssembler`（按依赖安装/卸载的另一抽象，无生产消费者），本决策下退役，不复用为 `build_runtime` 的落点。

> **进度（2026-11-18）**：assembly 已搬到 `services/assembly.py`，命令注册表由顶层组合根 `openharness/assembly.py` 注入（避免 `services ⇄ commands` 环）；`ui/runtime.py` 退化为 re-export。`autopilot`、`ohmo/gateway`、`cli_preview` 已不再 import `ui.*`。`ohmo/runtime.py` 仍用 `ui.backend_host` / `ui.frontend_runtime` / `ui.runtime.handle_line`（启动器耦合，3 条），`cli_commands → ui.app` 1 条（同为 L5），留给 P4/P5。

### D4. turn loop：保留三层，删掉第四层

**不合并 `TurnExecutor` 与 `StepRunner`** —— 那是 ADR-0006 的既有决定（turn 级临时状态 vs 单次请求），合并属于回退。实际做法：

- `engine/query.py` 取消"层"的地位，只作为 re-export 视图（或整体并入 `engine/query_context.py`）；`query.py:47-56` 的 10 个兼容别名删除（其中 9 个零消费者；`_execute_tool_call` 被 `tests/test_engine/test_query_engine.py:29` 使用，删除时同步改测试）；
- `QueryContext` 构造收敛为单点（消除 `query_engine.py:804` 与 `:883` 的重复）。两处字段目前只差 `max_turns`（`self._max_turns` vs `continue_pending` 的 override），收敛时必须把 `max_turns` 作为显式参数传入并保留 override 语义，不得静默回落；`step_runner` 不再接收 `turn_count`；
- 压缩：**只统一实现，不合并触发点**。`CompactPolicy` 统一"怎么压"（摘要构造、预算、`services/compact/auto.py:298,314` 标 legacy 的两条路径），但两个入口保留：`on_step_pre`（主动，按阈值，`turn_executor.py:103`）与 `on_request_error`（被动，仅在 prompt-too-long 之后，`turn_executor.py:171`）。合并成单一阈值会让主动压缩变频繁，而每次压缩都是历史重写，缓存命中率会持续下降；
- 重试：token-limit 降级改为 `api/transport.py` 上的一个策略点，engine 侧不再自持重试循环；
- 双发射：`emit_event` 是事实，`yield StreamEvent` 是派生，用一条等价性测试钉住（ADR-0011）。

> **进度（2026-11-18）**：`QueryContext` 已单点构造，`continue_pending(max_turns=...)` 的 override 有回归测试；`query.py` 的 10 个兼容别名已删，共享辅助函数移到 `engine/step_support.py`。压缩实现收敛、token-limit 重试策略、messages SoT 切换尚未开始。

### D5. 状态单一写者

| 状态面 | 唯一写者 | 其余 |
|---|---|---|
| messages | `events.jsonl` 为 SoT（ADR-0002），`SessionManager` 为缓存视图 | `session_storage.py` 快照降为 legacy 回退；`BackendConfig.restore_messages` 只读 |
| usage | `SessionManager.cost_tracker` | 事件日志派生、快照、UI 传参均只读 |
| permission mode | `settings.permission.mode` | 7 处直接写者改只读投影（含 plan-mode 工具、`QueryEngine.set_permission_mode`、`ui/runtime.py:554`） |
| todo | 新增内存 owner（现在只有文件） | `/todos` 读投影 |

messages 的 SoT 切换波及 `ui/backend_host.py` 回放与 `ohmo` 快照，必须"先加等价性测试、再切"，单独一步、可回滚。等价性必须是**字节级**：比较 `cache_diagnostics._message_wire_payload`（`cache_diagnostics.py:131`）的 hash 序列，而不是语义相等；并覆盖 submit 与 `continue_pending` 两条路径的已知差异——`sanitize_conversation_messages` 在两处各调用一次（`query_engine.py:755`、`:876`），但 coordinator context 只在 submit 路径追加（`:840-842`）。

> **进度（2026-11-18）**：messages SoT 已切换——每轮结束后 `QueryEngine._commit_messages` 从事件日志重建 `SessionManager` 缓存；默认 pipeline 直接读增量 `SessionLogState` 投影，不重读整个 JSONL，无日志的引擎保留原 working-list 提交。`tests/test_engine/test_message_sot.py` 覆盖 live vs 重放 wire hash、coordinator context 请求级、continue_pending 一致；dangling tool_use 由重放侧折叠解决。`session_storage` 快照已降为回退：`load_session_snapshot` / `load_session_by_id` 在存在 `events.jsonl` 时用其投影覆盖快照消息并标记 `messages_source=event_log`；保存路径保留给旧会话与导出。permission mode 已完成单写者：`PermissionSettings.set_mode`，并由 `tests/test_config/test_permission_mode_writer.py` 静态守卫。

### D6. 上帝对象拆分

| 文件 | 现状职责数 | 拆为 |
|---|---|---|
| `config/settings.py`(1186) | 8 | `settings_schema.py`（模型+Settings，避开已存在的 channel `schema.py`）/ `providers.py`（目录+别名+resolve）/ `overrides.py`（env+CLI）/ `io.py`（锁+原子写 0600） |
| `swarm/permission_sync.py`(1169) | 8 | `protocol.py` / `store.py`（布局+GC）/ `transport.py`（轮询+mailbox） |
| `channels/impl/feishu.py`(1344) | 8 | `feishu_policy.py` / `feishu_render.py` / `feishu_media.py` / `feishu_transport.py` + `feishu_base.py`；原文件 487 行，`quality_baseline.json` 的 54KB `file_size_limits` 豁免已删除 |
| `ui/runtime.py`(981) | 7 | `services/assembly.py` + 适配器 |
| `ohmo/gateway/runtime.py`(1254) | 8 | `runtime.py`（池/路由/快照，657 行）+ `runtime_helpers.py`（流事件转换与群/媒体辅助，669 行） |

> **进度（2026-11-18）**：`swarm/permission_sync.py` 已拆为 `permission_protocol.py`(339) / `permission_store.py`(365) / `permission_transport.py`(475)，`permission_sync.py` 只保留 104 行 re-export。`config/settings.py` 已拆为 `settings_schema.py`(587) / `providers.py`(361) / `provider_types.py`(45) / `overrides.py`(163) / `io.py`(76)，`settings.py` 只保留 82 行 re-export。`channels/impl/feishu.py` 已拆为 `feishu_policy.py`(183) / `feishu_render.py`(431) / `feishu_media.py`(185) / `feishu_transport.py`(128) + `feishu_base.py`(29)，原文件 487 行；`file_size_limits` 豁免已删除。`ui/runtime.py` 已随 D3 降到 358 行。`ohmo/gateway/runtime.py` 已拆为 `runtime.py`(657) + `runtime_helpers.py`(669)。D6 列出的五个文件全部拆完；`file_size_limits` 为空。

### D7. 缓存字节稳定是重构的硬约束

上面所有相位都必须满足：**不改装配结果、不改 wire bytes**。允许的变化只有两类：

1. 一次性失效（每会话冷启动一次），PR 中显式标注"允许，勿当回归"；
2. 零影响。

持续失效（会话中途反复 miss）一律视为回归。具体规则：

- `build_runtime` 搬家零语义 diff；live 字段（cwd、日期、分支）不得进入 stable section（见 D3）。
- 压缩只统一实现、保留两个触发点（见 D4）；不得用单一阈值提高主动压缩频率。
- messages 的 SoT 切换按 wire-payload hash 序列验收（见 D5）。
- 记忆注入位于 **turn context**（`_turn_context_sections`，`prompts/context.py:362`），不在 stable section。因此记忆写入不得改写已追加的历史 turn-context 消息；刷新只影响新建的尾部消息。历史消息一旦写入即冻结，否则会变成 `history_rewrite`（整段前缀失效）。
- 工具注册顺序确定；`activate_tools` 保持 append-only（`query_engine.py:344-350`），收编注册表时不得引入 `set` / `dict` 迭代序。

**闸门**：`tests/test_engine/test_cache_stability.py`，P1 先落地，P2 起每个阶段必须绿。四条断言（不需要真 provider，`FakeApiClient` 记录 wire bytes 即可）：

1. 连续两轮、cwd 与时钟不同 → `stable_sha` 与 `tools_sha` 相等，第二轮 `message_hashes` 以第一轮为前缀；
2. `activate_tools(["x"])` 之后 → 旧前缀原样保留，只有尾部变化；
3. 一次记忆写入 → `stable_sha` / `tools_sha` 不变，`system_drifts` 与 `history_rewrites` 均为 0，变化只出现在新追尾的 turn-context 消息；
4. `continue_pending` 与普通轮 → `stable_sha` 相同，message 前缀兼容。

**流程**：每个阶段的 PR 附一行 cache delta——取最近一次 `CacheFingerprint` 的 `stable_sha` 与 `cache_diagnostics.summary()` 的 `hot_cache_hit_rate`（P1 让 `summary()` 也暴露最近 fingerprint，便于脚本生成）；一次性失效显式标注"允许"。

## Consequences

### Positive

- `api` 可独立测试；`engine` 不再知道 `services`，替换存储/记忆/压缩不需要改核心循环。
- turn loop 的层数与 ADR-0006 一致，但不再有第四层壳和重复构造。
- import 契约可机器校验（ADR-0011），防止回潮。
- 缓存命中率除"压缩实现收敛"与"messages SoT 切换"外均无影响；这两处各有一条字节级闸门，一次性失效必须在 PR 中显式标注。

### Negative / Trade-offs

- 端口化会让 `engine` 侧调用变长（`ports` 间接层），调试需要额外跳转。
- 一次性大范围移动类型（`domain/messages.py`）会让未提交的工作区产生大 diff，必须与现有 WIP 隔离。
- `services/assembly.py` 接管 `build_runtime` 后，`ui` 侧的测试需要重写入口。
- 每个阶段的 PR 多两项固定成本：运行一次缓存闸门 + 附一行 cache delta。一次性失效需要人工判断，不能纯自动判绿。

## Migration

见 `docs/refactor-plan.md`：P1（先落 `tests/test_engine/test_cache_stability.py`，建立缓存基线）、P2（解环）、P3（turn loop 与状态）、P5（上帝对象）。每步保持全量测试绿、零功能变更，并在 PR 附 cache delta。

## Alternatives

- **合并 `TurnExecutor` 与 `StepRunner` 回三层**：被排除——会回退 ADR-0006，且 turn 级临时状态（压缩标记、token 降级）与 step 级状态混在一处后更难测。
- **只加 import-linter 契约、不移动类型**：被排除——契约会一次性红几十条、只能大面积 ignore，等于没有契约。
- **把 `services` 整体并进 `engine`**：被排除——`services` 承担接入与装配，与循环无关，合并会把客户端/存储依赖拖进核心。
- **把 auto-compact 与 reactive compact 合并为一个触发阈值**：被排除——两个触发点语义不同（阈值预判 vs prompt-too-long 后补救），合并会提高主动压缩频率；每次压缩都重写历史，对 DeepSeek/OpenAI 这类无断点的隐式前缀缓存是持续性命中率损失。
- **messages SoT 等价性用"语义相等"比较**：被排除——语义相等不保证 wire bytes 相同，少一条 `sanitize_conversation_messages` 或多一条 coordinator context 都会让后续前缀 miss。

## References

- `docs/adr/0001-capability-seams.md`、`0002-event-log-and-projections.md`、`0006-turn-executor-decomposition.md`、`0008-prompt-cache-stability.md`、`0011-test-bands-and-provider-contract.md`
- `src/openharness/engine/query_engine.py:704,804,840-842,873,883`
- `src/openharness/engine/query.py:47-56`
- `src/openharness/engine/step_runner.py:80,137-144`
- `src/openharness/engine/turn_executor.py:103,171`
- `src/openharness/prompts/context.py:165-186,362`、`src/openharness/prompts/composer.py:45-48`
- `src/openharness/engine/cache_diagnostics.py:131`、`src/openharness/engine/cache_budget.py`
- `src/openharness/api/client.py:216-285`、`src/openharness/api/capabilities.py:17`
- `src/openharness/services/compact/auto.py:298,314`
- `scripts/quality_gate.py`（`file_size_limits`）
- `tests/test_engine/test_cache_stability.py`（D7 闸门，P1 落地）
