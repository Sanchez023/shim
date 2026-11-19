# ADR-0009: 以不变量与 canonical 路径收敛功能面（不删除能力）

- 状态: Proposed
- 日期: 2026-11-18
- 决策者: OpenHarness maintainers
- 相关: ADR-0001, ADR-0002, ADR-0005, ADR-0006, ADR-0010, ADR-0011

## Context

项目对外开源，功能面只增不减；但能力域的扩张速度长期快于结构收敛。当前实测：

- 5 个 console_scripts（`shim`/`openharness`/`oh`/`openh` → `openharness.cli:app`，`ohmo` → `ohmo.cli:app`），实际只有 2 个 app；
- `oh` 42 个子命令、`ohmo` 16 个命令、斜杠命令 68 个 canonical；
- 39 个内置工具，其中 23 个在 `core` profile 下对模型不可见（`src/openharness/tools/profiles.py:20-46`）；
- `channels/impl/` 12 个实现，README 只宣传 4 个；
- 子代理 4 套执行体（`tasks/`、`swarm/`、`coordinator/`、Codex `services/subagent_provider.py`）；
- 记忆 5 套机制，`MEMORY.md` 有 3 个写者；
- cron 4 条路径、autopilot 4 条路径、令牌账本 3 个命令（`/cost` `/usage` `/stats`）；
- 事件有 2 条并行通路，另有 1 条写完未接线（`services/event_pipeline.py:80,108` 的 `enable_ui=False`）；
- 配置面约 30 个开关，其中 8 个只被读 1 处，`verbose`（`config/settings.py:639`）零读者；
- README 宣称与实际漂移：`Tools 43+` vs 39、"4 个 channel" vs 12、sandbox 文档写 Docker 而默认 `backend="srt"`（`config/settings.py:107`）、`voice/stream_stt.py:6` 是空壳。

这不是"代码脏"，而是**同一件事有多条实现路径，改一处要同时想三处**。在"不能删功能"的约束下，删除不是选项，唯一的收敛手段是：每条能力都指定唯一 canonical 路径，其余实现挂在同一接口后面、默认关、文档标档。

## Decision

### D1. 六条不变量

| 编号 | 不变量 |
|---|---|
| I1 | 每个能力域有唯一 canonical 路径；其余实现是同一接口下的可替换 strategy，默认关 |
| I2 | 依赖单向：`api` 不认识 `engine`；`tools` 不认识编排；呈现层只能被 CLI 入口使用（见 ADR-0010） |
| I3 | 每个状态面只有一个写者，其余是只读投影 |
| I4 | 事件日志是 SoT；UI 所见必须能从日志重放；第二通路必须与日志等价 |
| I5 | 宣称即可验收：README/CHANGELOG 的每条能力必须映射到 canonical 路径或标 `experimental` |
| I6 | 每个配置项必须有读者和一个测试，否则标 `deprecated` |

不变量本身由测试守护，见 ADR-0011。

### D2. 五档处置（替代"删除"）

- **canonical**：主路径，文档与测试只认它。
- **alias**：等价转发 + 调用时 warning，保留语义，收掉实现。
- **experimental**：默认关、README 明确标注、不承诺兼容。
- **deprecated**：代码保留、文档不再承诺、登记带到期日的 ignore，下一轮才有资格删。
- **死代码**：零消费者，可直接删除。当前可直接删的有 `ui/output.py` + `ui/input.py` + `ui/permission_dialog.py`（约 320 行，唯一引用是 `tests/test_ui/test_modes.py:9`）、`runtime/assembly.py`（自述 "Experimental: not exported"）、`channels/UPSTREAM`（空目录）、`state/`、`tasks/bridge`（无入边）。

### D3. 能力地图

| 能力域 | 现状 | canonical | 其余处置 |
|---|---|---|---|
| 子代理 | 4 套 | `services/subagent_provider.py` 扩为 `AgentBackend` 注册表，默认后端 `tasks/` | `swarm` 4 后端注册为 strategy；`coordinator/TeamRegistry` → alias；Codex → experimental |
| 记忆 | 5 套 | `memory/` 单一 `MemoryWriter` 端口 | 其余注册为 hook 策略；`services/compact/session_memory.py` → deprecated；`auto_extract_enabled` 保持默认关 |
| 事件 | 2 并行 + 1 未接线 | `*.events.jsonl` 为 SoT；`AgentEvent` 为内部事实，`StreamEvent` 为 wire 派生 | UI subscriber 接线或删；`session-<id>.json` 降为 legacy 只读 |
| 持久化 | 2 套格式 | `*.events.jsonl` | `session_storage.py` 快照保留为回退，文档不承诺 |
| 权限模式 | 5 份副本 | `settings.permission.mode` 唯一写者 | 其余改只读投影 |
| 定时任务 | 4 条 | 单一 scheduler | 触发面保留两档：slash 与 tool |
| autopilot | 4 条 | `/autopilot` | `/ship` → alias；`autopilot-dashboard/` → experimental |
| 令牌账本 | 3 个命令 | `/usage` | `/cost`、`/stats` → alias |
| 前端 | React 主路径 + Python TUI 残骸 | `frontend/terminal` + `ui/backend_host.py` | 残骸按 D2 删除 |
| channel | 12 个实现 | README 名单中的 4 个 | 其余标 experimental/community |
| 工具面 | 39 个，23 个 deferred | `core` 集需文档解释取舍 | 其余在 `/tools` 中可解释 |
| 入口 | 5 个 console_scripts | `openharness.cli:app` 与 `ohmo.cli:app` | 其余别名保留但文档只写 2 个 |

### D4. deprecation 是机制，不是承诺

新增 `commands/deprecations.py`：

- 一张表登记 `alias → canonical` 与移除版本；
- 调用 alias 时打印一行 `→ /usage（/cost 将于 0.4 移除）`；
- `/deprecated` 可自省；
- **别名等价性测试**：alias 与 canonical 的 handler 输出必须逐字段相等，否则 CI 失败。

### D5. README 对账

新增解析 README 能力表的测试：每一项必须映射到本文档 D3 的 canonical 或标 `experimental`，否则失败。

## Consequences

### Positive

- 功能面零损失，但"改一处要想三处"降为"改一处想一处"。
- 用户可见行为不变，除 alias 会多一行 warning。
- 新增能力必须先回答"挂到哪个域、谁是 canonical"，从源头阻止第二条路径。

### Negative / Trade-offs

- alias/deprecated 档会让代码里长期存在"活着但没人用"的实现，短期内行数不会下降。
- 等价性测试需要为每个 alias 写逐字段断言，成本高于"删掉"。
- 23 个 deferred 工具的可解释性需要额外文档投入。

## Migration

见 `docs/refactor-plan.md` 的 P1（死代码与别名）与 P4（主路径收编）。

## Alternatives

- **直接删除重复实现**：被排除——项目对外开源，功能面不能删。
- **全仓冻结、只做新功能**：被排除——`quality_gate.py` 的棘轮已经在防新债，冻结会让已有债固化。
- **只写文档、不加测试**：被排除——没有等价性测试的 alias 会在三次改动后变成第二套实现，正是当前问题的成因。

## References

- `docs/adr/0005-context-and-subagent-seams.md`（子 Agent 后端可插拔的既有方向）
- `docs/adr/0002-event-log-and-projections.md`（事件日志作为基础）
- `src/openharness/services/agent_backend.py`、`src/openharness/tools/agent_tool.py:155-210`
- `src/openharness/commands/builtin/session.py:617-690`、`src/openharness/cli_app.py:34-46`
