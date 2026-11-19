# 收敛执行计划（P0–P5）

配套决策：ADR-0009（不变量与 canonical 路径）、ADR-0010（依赖单向化与端口化）、ADR-0011（三层测试带与真 provider 契约）。

前置约束：**功能面不删**。所有阶段对用户可见行为零破坏，唯一例外是 alias 调用时多一行 deprecation warning。

## 阶段总览

| 阶段 | 内容 | 是否改行为 | 主要风险 |
|---|---|---|---|
| P0 | 设计落盘 | 否 | 无 |
| P1 | 零风险减法 + 测试地基 | 否 | 删死代码时误删被测试引用的公共入口 |
| P2 | 解环（domain / ports / 装配） | 否 | 大范围移动类型，与 WIP 冲突 |
| P3 | turn loop 与状态单一写者 | 否 | messages SoT 切换波及回放与快照 |
| P4 | 主路径收编 + deprecation 机制 | 是（多 warning） | alias 掩盖行为差异 |
| P5 | 上帝对象拆分 + provider smoke | 否 | 大文件拆分期间合并冲突 |

## P0 设计落盘（0.5 天）

产出：

- `docs/adr/0009-canonical-paths-and-deprecation.md`
- `docs/adr/0010-dependency-direction-and-ports.md`
- `docs/adr/0011-test-bands-and-provider-contract.md`
- 本文件；并更新 `docs/adr/README.md` 索引

验收：

- 三条 ADR 状态为 `Proposed`，索引已登记；
- 每条不变量在 ADR-0011 的守护表里有对应行；
- 不改动 `src/`、`tests/`、`scripts/`。

## P1 零风险减法 + 测试地基（2–3 天）

改动面：

1. 删死代码：`ui/output.py`、`ui/input.py`、`ui/permission_dialog.py`（约 320 行，唯一引用 `tests/test_ui/test_modes.py:9`）、`channels/UPSTREAM`。`runtime/assembly.py` 不在本阶段删，P2 退役；`state/`、`bridge/` 给结论（实测均有 `ui` / `commands` 入边，按"零消费者"重新判定，不得直接删）。
2. 删 `engine/query.py:47-56` 的 10 个兼容别名（9 个零消费者；`_execute_tool_call` 被 `tests/test_engine/test_query_engine.py:29` 使用，同步改测试）。
3. 给 0 读者开关标 deprecated：`verbose`（`config/settings.py:639`）等，登记进 `deprecated_fields.py`。
4. 加 marker + `--strict-markers` + `pytest-timeout` + `make test-fast/test-all`。
5. `tests/conftest.py` 提供共享 `FakeApiClient`（记录 system / tools / messages 的完整 wire bytes）、tmp HOME、autouse 禁 socket、可注入 clock。
6. `tests/test_no_committed_secrets.py` 从 669 个 node 收成 1 个用例内循环。
7. 新增 `tests/test_engine/test_cache_stability.py`（ADR-0010 D7 / ADR-0011 I7 的四条断言），先于 P2 落地；同时让 `cache_diagnostics.summary()` 暴露最近一次 `stable_sha` / `tools_sha`，使 PR 的 cache delta 可脚本生成。

验收：

- 全量测试绿；`git diff` 只触及上面列出的文件；
- `make test-fast` 存在且 < 60s，且包含缓存闸门；
- node id 从 2364 降到 ≤ 1900；
- 每条被删内容在 PR 描述里给出"零消费者"证据（grep 结果）；
- 缓存闸门在 P1 基线上绿，并留下首份 cache delta 基线（`stable_sha` + `hot_cache_hit_rate`）。此后每个阶段的 PR 统一附一行：`cache_delta: stable_sha <before> → <after>; hot_hit <before>% → <after>%; one-shot: yes/no`。

回滚：纯删除 + 新增测试配置，逐条 revert 即可。

## P2 解环（4–5 天）

改动面（按顺序，每步全量绿再走下一步）：

1. 新建 `openharness/domain/messages.py`，把 `ConversationMessage`/`TextBlock`/`ImageBlock`/`assistant_message_from_api` 下移；`api`、`engine` 改为依赖 L0。**字段顺序与 `model_dump` 语义保持不变**；若 wire bytes 变化，在 PR 标注"一次性失效"。
2. 新建 `engine/ports.py`（`EventSink`/`MemoryPort`/`CompactPort`/`SubagentPort`/`SessionStorePort`），engine 改依赖端口；实现留在 `services/` 并由装配注入。
3. `tools/agent_tool.py` 等 4 个文件改依赖 `AgentRunPort`，后端选择上移 `services/agent_backends`。
4. `build_runtime`（`ui/runtime.py:320-629`）搬进 `services/assembly.py`，**零语义 diff**：`prompts/context.py:165-186` 的 stable/live 切分原样保留；`RuntimeBundle`/`start_runtime`/`close_runtime`/`_resolve_api_client_from_settings` 同批移动；`autopilot`/`ohmo`/`cli_commands`/`cli_preview` 去掉 `ui.*` 依赖。`runtime/assembly.py`（CapabilityAssembler）退役，不作为落点。
5. `import-linter` 契约进 CI（违规带到期日 ignore）。

验收：

- `services → engine` 29 → 0；`engine → services` 10 → 0；`api → engine` 3 → 0；`tools → coordinator/swarm` 4 → 0；
- 契约测试通过，ignore 表中每项都有到期日；
- 全量测试绿，无行为变更；
- 缓存闸门四条绿；同一会话连续两轮、cwd 与时钟不同时 `stable_sha`/`tools_sha` 相等；
- PR 附 cache delta；domain 下移导致的失效标注为"一次性，允许"。

回滚：每步独立 commit；`domain/messages.py` 若受阻可保留 re-export 双写过渡。

**进度（2026-11-18）**：

- `domain/messages.py`、`domain/usage.py`、`domain/stream_events.py`、`domain/events.py` 已落地，旧路径保留 re-export。
- 事件实现（projections / subscribers / fan-out）已移入 `services/`，`engine.events` 只保留契约 re-export。
- 端口已落在 `runtime/seams`：`EventLogPort`、`SessionAppendPort`、`MemoryPort`、`CompactPort`；`engine/ports.py` 只是 re-export，实现由 `build_runtime` 注入。
- Agent 编排已收敛到 `AgentRunPort`：`agent` / `send_message` / `team_create` / `team_delete` 不再 import `coordinator` / `swarm`。
- `build_runtime` / `RuntimeBundle` / start-close 已搬到 `services.assembly`；命令注册表由顶层组合根 `openharness.assembly` 注入，避免 `services ⇄ commands` 环；`ui.runtime` 只做 re-export。
- `runtime/seams` 已回到纯端口层：`backends.py` 删除，`LocalToolExecutor` / `CallableApprovalGate` 移入 `engine.tool_executor`；`runtime → engine` 为 0。
- 实测：`api → engine` **0**，`engine → services` **0**，`tools → coordinator/swarm` **0**，`runtime → engine` **0**，`engine → api` 6，`channels → engine` 1。
- `services → engine` 为 3，全部在 `services/assembly.py`（组合装配，L4→L3 允许）；不是实现耦合，也不构成环。`runtime → services` 只剩 `seams/agents.py` 的惰性默认端口工厂。
- 纯移动/端口注入不改变 wire bytes，缓存闸门与全量测试绿。
- P3 已提前完成两项：`QueryContext` 单点构造（`max_turns` override 有回归测试）；`_bounded_completion_tokens` / `_serialize_messages` / `_preprocess_images_in_messages` / 错误分类别名移到 `engine/step_support.py`，`query.py` 的 10 个兼容别名删除，`query ⇄ turn_executor` 的懒加载环消失。
- **收尾状态（2026-11-18）**：最终上行边为 `api → engine` 0、`engine → services` 0、`tools → services` 0、`tools → coordinator/swarm` 0、`runtime → engine` 0、`runtime → services` 0、`channels → engine` 0；`services → engine` 仅剩 `services/assembly.py` 的 3 条组合装配边（L4→L3 允许）。
- 剩余同层/启动器耦合：`ohmo/runtime.py → ui` 3 条（launcher 仍用 `ui.backend_host` / `ui.frontend_runtime` / `ui.runtime.handle_line`）、`cli_commands → ui.app` 1 条（同为 L5）。不影响解环。
- P3 已加 messages SoT 的字节级等价测试 `tests/test_engine/test_message_sot.py`：live 与 `events.jsonl` 重放的 `_message_wire_payload` hash 序列必须一致；coordinator context 只存在于当前请求；continue_pending 与普通轮可重放。dangling tool_use 的缺口已用**重放侧折叠**解决：`SessionLogState.derive_messages()` 应用与 live resume 相同的 `sanitize_conversation_messages`。
- **SoT 写者已切换**：每轮结束后 `QueryEngine._commit_messages` 从事件日志重建 `SessionManager` 缓存；默认 pipeline 读增量 `SessionLogState` 投影（不重读 JSONL），无日志引擎保留原路径。
- **快照降为回退**：`load_session_snapshot` / `load_session_by_id` 在存在 `events.jsonl` 时用其投影覆盖快照消息，并标记 `messages_source=event_log`；无日志/空日志仍回退快照。`save_session_snapshot` 保留用于旧会话与导出，P4 再决定 deprecation。
- permission mode 已收成单一写者：`PermissionSettings.set_mode`，7 处直接写全部改走它；`tests/test_config/test_permission_mode_writer.py` 静态断言源码中不再出现 `permission.mode =` 赋值。
- P5 D6 清单全部完成：`swarm/permission_sync.py`（1169 → 104 + 339/365/475）、`config/settings.py`（1190 → 82 + 587/361/45/163/76）、`channels/impl/feishu.py`（1344 → 487 + 183/431/185/128/29）、`ui/runtime.py`（981 → 358）、`ohmo/gateway/runtime.py`（1272 → 657 + 669）。`quality_baseline.json` 的 `file_size_limits` 已为空，大文件数为 0。
- P3 未做：`CompactPolicy` 实现收敛、token-limit 重试策略。

## P3 turn loop 与状态单一写者（3–4 天）

改动面：

1. `engine/query.py` 取消层级地位（并入 `query_context.py` 或仅 re-export）。先把仍被 `turn_executor.py:47-55` / `step_runner.py:74` 懒加载的辅助函数（`_bounded_completion_tokens`、`_serialize_messages`、`_preprocess_images_in_messages`、错误分类别名）移到中立模块，断开 `query.py` ⇄ `turn_executor` 的函数内循环。
2. `QueryContext` 单点构造（消除 `query_engine.py:804` / `:883` 重复）。两处字段目前只差 `max_turns`；把 `max_turns` 作为显式参数传入，保留 `continue_pending(max_turns=...)` 的 override 语义；去掉 `step_runner.py:80` 的 `del turn_count`。
3. 压缩**只统一实现、不合并触发点**：`CompactPolicy` 统一摘要构造与预算（吸收 `services/compact/auto.py:298,314` 的 legacy 路径），但保留 `on_step_pre`（主动，`turn_executor.py:103`）与 `on_request_error`（被动，`turn_executor.py:171`）两个入口。不得用单一阈值提高主动压缩频率。
4. 重试收敛到 `api/transport.py` 一个策略点。
5. permission mode 改为 `settings` 唯一写、其余只读投影（直接写者 7 → 1，含 `commands/builtin/settings.py:600,616,629`、`engine/query_engine.py:554`、`tools/enter_plan_mode_tool.py:34`、`tools/exit_plan_mode_tool.py:39`、`ui/runtime.py:554`）。
6. messages SoT：先加"事件日志重放 == live 流"等价测试（比较 `_message_wire_payload` 的 hash 序列，不是语义相等），再把 `SessionManager` 降为缓存视图，`session_storage.py` 快照标 legacy；覆盖 submit 与 `continue_pending` 两条路径（coordinator context 只在 submit 追加，`query_engine.py:840-842`）。

验收：

- `query.py` 只剩 `QueryContext` / `MaxTurnsExceeded` / `run_query`（辅助函数已搬走）；
- 压缩实现只有一处、触发点仍为 2 个（grep 断言）；重试只有一个策略点；
- 双发射等价测试与重放等价测试通过，SoT 等价性用 wire-payload hash 序列；
- permission mode 赋值点数量 = 1；
- 缓存闸门四条绿；压缩与 SoT 两条 PR 各附 cache delta，不允许出现会话中途持续 drift。

回滚：第 6 项单独一个 commit，出问题只回退 SoT 切换。

## P4 主路径收编 + deprecation 机制（4–5 天）

改动面：

1. 新增 `commands/deprecations.py`（alias 表 + warning + `/deprecated` 自省）。
2. 子代理 / 工具注册表：`services/subagent_provider.py` 扩为 `AgentBackend` 注册表；`swarm` 4 后端注册；`coordinator/TeamRegistry` → alias；Codex → experimental。注册顺序必须确定，不得引入 `set` / `dict` 迭代序；`activate_tools` 的 append-only 语义（`query_engine.py:344-350`）保持不变。
3. 记忆：引入唯一 `MemoryWriter`；其余 4 套注册为 hook 策略；`services/compact/session_memory.py` → deprecated。记忆 section 位于 **turn context**（`prompts/context.py:362`），不在 stable 前缀；写入只能影响新建的尾部消息，已追加的历史 turn-context 消息必须冻结，否则会变成 `history_rewrite`（整段前缀失效）。
4. cron / autopilot / 账本：`/autopilot`、`/usage` 为 canonical，`/ship`、`/cost`、`/stats` → alias。
5. 事件：`enable_ui` 要么接线要么删；`session-<id>.json` 降 legacy。
6. README 对账测试上线；README/CHANGELOG 与 D3 能力表对齐（工具数、channel 数、sandbox 描述、voice 标 experimental）。
7. 不变量守护测试全套上线（I2–I7）。

验收：

- 每个能力域恰好 1 个 canonical，其余全部经同一接口注册且默认关；
- 每个 alias 有逐字段等价性测试；
- README 能力表 100% 映射，对账测试通过；
- `/deprecated` 输出与 `deprecations.py` 一致；
- 缓存闸门绿；记忆写入只产生一次可归因 drift；注册表收编前后 `tools_sha` 不变。

回滚：每张表独立 commit；`.py` 中的注册表默认值可逐个开关回退。

## P5 上帝对象拆分 + provider smoke（5–6 天）

改动面按 ADR-0010 D6，逐个文件拆；同时：

- 删除 `scripts/quality_gate.py` 中 `file_size_limits` 的 feishu 豁免；
- `tests/smoke/` 三个 provider case 上线；key 缺失 skip；
- per-package 覆盖率 floor 进 `quality_gate.py`。

验收：

- `feishu.py` ≤ 600 行、`settings.py` ≤ 500 行、`permission_sync.py` ≤ 450 行、`ohmo/gateway/runtime.py` ≤ 700 行；
- `quality_baseline.json` 中无 `file_size_limits`；
- 三个 provider case 在真 key 下通过一次，结果记录在 `benchmarks/`；
- per-package floor 生效且与当前值一致；
- 缓存闸门绿；拆 `settings` 若改变 profile section 文案，在 PR 标注一次性失效。

回滚：单文件为粒度；拆出的模块先以 re-export 保留旧导入路径一个版本。

## 量化指标

| 指标 | 起点 | P5 目标 |
|---|---|---|
| 反向/横穿依赖 | 5 类（`services→engine` 29 处） | 0 |
| ruff / mypy | 89 / 533（core 0） | ≤ 50 / ≤ 400（core 恒 0） |
| 测试带 | 无 marker，E2E 0 | 3 带 + 3 个 provider case |
| `make test-fast` | 不存在 | < 45s |
| node id | 2364 | ≤ 1900（secret 扫描收编 + 死测试清理） |
| 每能力域路径数 | 2–5 | 1 |
| 按包覆盖率 | 无 floor（`autopilot` 0.09） | 有 floor 且只升 |
| test/impl | 0.57 | ≥ 0.57 |
| 最大文件 | 1344 行（带豁免） | ≤ 700 行（无豁免） |
| 缓存命中率 | 无基线（`cache_diagnostics.summary()`） | 每阶段记录 `hot_cache_hit_rate`；只允许已标注的一次性冷启动 |

## 全局风险与对策

1. **与未提交 WIP 冲突**：当前工作区有大量未提交改动。P1 之前必须先落定基线（提交或隔离到 worktree），否则 P2 的类型下移会与 WIP 大面积冲突。
2. **alias 掩盖行为差异**：等价性测试必须逐字段比较（输出、exit code、副作用），不能只断言"都成功"。
3. **契约一次性红一片**：P2 的 import-linter 违规全部登记带到期日 ignore，到期即 CI 失败，避免变成永久豁免。
4. **provider smoke 不稳定**：只断言协议级事实，不做措辞匹配；失败留档原始响应以便区分"harness 错"与"模型不同"。
5. **缓存前缀被 live 字段污染**：`build_runtime` 搬家是这类回归的高发点——cwd/日期/分支一旦进入 stable section，每轮都 miss。对策：零语义 diff + 缓存闸门第一条 + PR cache delta。
6. **压缩触发点被合并**：主动压缩变频繁等于持续重写历史。对策：只统一 `CompactPolicy` 实现，`on_step_pre` 与 `on_request_error` 两个入口保留，验收用 grep 钉住触发点数量。
7. **SoT 切换产生不同 wire bytes**：语义相等不够，必须比较 `_message_wire_payload` 的 hash 序列，并覆盖 submit / `continue_pending` 两条路径。
8. **记忆写入改写历史**：记忆在 turn context（消息尾部）。若写入器重写已追加的上下文消息，会产生 `history_rewrite` 并让整段前缀失效。对策：历史消息冻结，刷新只影响新建的尾部。
