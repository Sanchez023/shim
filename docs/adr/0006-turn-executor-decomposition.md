# ADR-0006: QueryEngine 拆分为 SessionManager / TurnExecutor / StepRunner

- 状态: Implemented
- 日期: 2026-11-15
- 决策者: OpenHarness maintainers
- 相关: ADR-0001, ADR-0002, ADR-0005

## Context

改造前，`QueryEngine` 与 `run_query` 共同拥有：

- 会话消息与 usage（`QueryEngine._messages`、`_cost_tracker`）；
- turn 循环、`max_turns`、压缩与 reactive compact；
- 单次模型请求、工具批次、事件发射；
- 重试、token 上限调整、错误恢复。

这导致状态所有权模糊：消息既在 `QueryEngine` 内存里，又可由事件日志重建；turn/step 级临时状态没有明确边界；替换后端或增加恢复能力时必须修改核心循环。

## Decision

拆成三个职责明确的组件，`QueryEngine` 收敛为 facade。

### SessionManager

- 唯一拥有会话消息与聚合 usage。
- 提供 `messages`（防御性副本）、`working_messages`（引擎内可变列表）、`load_messages`、`append`、`replace`、`clear`、`add_usage`。
- 可选绑定 `EventLogStore`，通过 `restore_from_event_log()` 从追加式日志重建消息与 usage。

### TurnExecutor

- 拥有一个 turn 的临时状态：`max_turns`、`effective_max_tokens`、压缩状态、reactive compact 标记。
- 负责：context governor、auto-compact、reactive compact、token 上限重试、turn 循环。
- 每个 step 委托给 `StepRunner`；根据 `StepOutcome.needs_followup` 决定是否继续。

### StepRunner

- 只执行一次模型请求及其工具批次。
- 负责：`step.start/end`、assistant 消息、tool.call/result、tool result user message、`AssistantTurnComplete`。
- 返回 `StepOutcome`：`final_message`、`usage`、`needs_followup`、`status`、`step_end_emitted`。

### QueryEngine

- 负责构造 `QueryContext`、发射 `turn.start/end`、会话记忆/autodream 等生命周期钩子。
- 把执行委托给 `TurnExecutor`，把消息/usage 委托给 `SessionManager`。
- `messages`、`total_usage`、`clear`、`load_messages`、`has_pending_continuation` 全部转发到 `SessionManager`。

### 兼容层

- `run_query(context, messages)` 保留原签名，内部委托 `TurnExecutor`，供 swarm worker、第三方调用与既有测试使用。
- `QueryEngine.submit_message()` / `continue_pending()` 直接使用 `TurnExecutor`，不再经过 `run_query`。

### 状态权威

- 绑定 `EventLogStore` 时，事件日志是恢复权威；`SessionManager` 在启动时可重建消息与 usage。
- 运行中 `TurnExecutor` 修改 `SessionManager.working_messages`；turn 结束时提交 durable 消息（过滤 coordinator runtime context）。
- UI 的 `StreamEvent` 契约不变。

## Consequences

### Positive

- 会话状态、turn 编排、单步执行各自有明确所有者。
- 恢复、压缩、重试、工具批次可以独立测试。
- `QueryEngine` 不再同时持有长期状态与循环逻辑，替换 AgentBackend 更容易。
- `run_query` 兼容层保证现有调用方不受影响。

### Negative / Trade-offs

- 多了一层对象协作，调试时需要沿 `QueryEngine -> TurnExecutor -> StepRunner` 追踪。
- `SessionManager.working_messages` 是可变列表，必须遵守“turn 结束提交”的约定。
- `run_query` 兼容层与 `TurnExecutor` 两条入口需要保持一致。

## Implementation

- `src/openharness/engine/session_manager.py`：`SessionManager`。
- `src/openharness/engine/step_runner.py`：`StepRunner`、`StepOutcome`。
- `src/openharness/engine/turn_executor.py`：`TurnExecutor`、压缩/重试/多 step 编排。
- `src/openharness/engine/query_engine.py`：facade，持有 `SessionManager`，发射 turn 事件。
- `src/openharness/engine/query.py`：`run_query` 兼容 shim。
- 测试：`tests/test_engine/test_decomposition.py`、`tests/test_engine/test_query_engine.py`、`tests/test_engine/test_events.py`。

## Alternatives

- **继续由 `QueryEngine` 直接驱动循环**：状态所有权无法收敛，恢复与后端替换持续耦合。
- **一次重写为三个新类并删除旧入口**：风险过高；兼容 shim 允许渐进迁移。
- **只做事件日志，不拆类**：可观测性提升，但 turn/step 状态边界仍然模糊。

## References

- `docs/adr/0001-capability-seams.md`、`0002-event-log-and-projections.md`、`0005-context-and-subagent-seams.md`
- `src/openharness/engine/query_engine.py`
- `src/openharness/engine/session_manager.py`
- `src/openharness/engine/turn_executor.py`
- `src/openharness/engine/step_runner.py`
