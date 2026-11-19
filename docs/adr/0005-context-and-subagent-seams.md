# ADR-0005: 上下文治理与子 Agent 后端可插拔

- 状态: Implemented
- 日期: 2026-11-15
- 决策者: OpenHarness maintainers
- 相关: ADR-0001, ADR-0002

## Context

上下文压缩目前写死在 `run_query` 的循环里：每轮开始调用 `auto_compact_if_needed()`，出错时触发 reactive compact，之后还有 session memory 与 autodream（`src/openharness/engine/query.py:645-717`、`src/openharness/services/compact/__init__.py`）。

子 Agent 有多套实现：`swarm/in_process.py`、`swarm/subprocess_backend.py`、`tasks/local_agent_task.py`，但没有统一契约。调用方需要知道具体后端，生命周期、容量、结果归一化各自实现。

DSH 的做法：

- 压缩是 `CompactionEngine` 服务，订阅 `agent/pre-step` 与 `agent/request-error`，而不是被 loop 直接调用（`docs/architecture.zh.md:112`、`packages/compaction/compaction-basic/src/index.ts:172-243`）。
- 压缩是持久事务：`compaction/start|summary|end|prune` 事件 + surface range 替换（`packages/compaction/compaction/src/types.ts:18-90`）。
- Subagent provider 暴露 `name`、`capabilities`、`inheritsParentContext`，以及二选一：`prepareContinuable()`（本地可续）或 `start()`（外部一次性）；单一 manager 负责生命周期（`packages/subagent/subagent/src/types.ts:321-364`、`packages/subagent/subagent/README.zh.md:94-148`）。

## Decision

### 上下文治理

- 新增 `ContextGovernor` 协议：
  - `on_step_pre(messages, budget) -> ContextPlan`
  - `on_request_error(messages, error) -> ContextPlan`
  - `after_turn(messages, usage) -> None`
  - `estimate(messages) -> int`
- 默认 `ExistingCompactGovernor` 包装 `auto_compact_if_needed`、microcompact、`try_context_collapse`，保持现有行为。
- 提供 `NoCompactGovernor` 与 `SessionMemoryGovernor`。
- 压缩结果发射 `context.compaction.start/summary/end` 持久事件，可从日志重放。
- `run_query` 只执行 `ContextPlan`，不再内含压缩策略分支。

### 子 Agent 后端

- 新增 `SubagentProvider` 协议，字段与 DSH 对齐：
  - `name`、`capabilities`、`inherits_parent_context`、`agent_route_defaults`
  - `prepare_continuable()` 或 `start()` 二选一
- 新增 `SubagentManager`，统一负责身份、容量、冷恢复、目录、通知与销毁；provider 不拥有这些。
- 结果归一化为 `(output, structured, diagnostic, stop_reason)`；消费者不解析传输错误。
- 现有 `swarm/` 与 `tasks/` 适配为 provider，默认行为不变。
- 职责边界：`SubagentManager` 只管理委派子任务；根会话生命周期归 `AgentBackend`，避免两个 manager 争抢身份、容量与销毁职责。

## Consequences

### Positive

- 压缩策略可配置、可替换、可回放。
- 子 Agent 后端可以统一接入本地、子进程、远程、Codex 等实现。
- 生命周期与结果格式统一，调用方不需要知道后端细节。

### Negative / Trade-offs

- `run_query` 需要改为执行 `ContextPlan`，短期增加一层间接。
- provider 契约需要覆盖现有两套实现的差异，迁移期间可能出现双轨。
- manager 成为关键路径，必须有并发与取消测试。

## Migration

1. 先抽出 `ContextGovernor`，默认实现包装现有压缩，保持行为不变。
2. 把 `run_query` 的压缩调用改为“请求 plan → 执行 plan”。
3. 增加 `context.compaction.*` 事件与重放测试。
4. 定义 `SubagentProvider` 与 manager，先适配 `swarm/in_process.py`。
5. 再适配 `swarm/subprocess_backend.py` 与 `tasks/local_agent_task.py`。
6. provider 契约与 manager 稳定后，优先接入 Codex app-server（C3a，Phase 4）；它不依赖完整安全/上下文改造。
7. Codex 主后端（C3b）由 `AgentBackend` 负责，不复用 `SubagentManager` 的根会话职责；ACP 等外部后端可后置。

## Alternatives

- **保留 loop 内压缩**：无法替换策略，也无法从日志重放压缩决策。
- **只做配置开关，不做协议**：无法接入新的压缩实现。
- **用继承统一子 Agent**：现有实现差异大，继承会形成脆弱基类。
- **每个调用方自己管理后端**：生命周期与结果格式无法统一。

## Implementation

- `src/openharness/runtime/seams/governance/context.py`：`ContextGovernor`、`ContextPlan`、`ExistingCompactGovernor`、`NoCompactGovernor`。
- `src/openharness/engine/query.py`：`step.pre` / request-error 调用 governor，压缩事件进入事件日志。
- `src/openharness/services/subagent_provider.py`：`SubagentProvider`、`SubagentManager`、`LocalCallableSubagentProvider`。
- `src/openharness/services/backends/codex_app_server.py`：Codex 子 Agent provider。
- 测试：`tests/test_services/test_context_governor.py`、`tests/test_services/test_subagent_provider.py`、`tests/test_backends/test_codex_app_server.py`。

## References

- `src/openharness/engine/query.py:645-717`
- `src/openharness/services/compact/__init__.py`
- `src/openharness/services/session_memory/`
- `src/openharness/swarm/in_process.py`、`swarm/subprocess_backend.py`
- `src/openharness/tasks/local_agent_task.py`
- `docs/architecture.zh.md:112`
- `packages/compaction/compaction-basic/src/index.ts:172-243`
- `packages/subagent/subagent/src/types.ts:321-364`
- `packages/subagent/subagent/README.zh.md:94-148`
