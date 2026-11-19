# ADR-0002: 以追加式会话日志和分层投影作为可观测性基础

- 状态: Implemented
- 日期: 2026-11-15
- 决策者: OpenHarness maintainers
- 相关: ADR-0001, ADR-0003

## Context

当前 `StreamEvent`（`src/openharness/engine/stream_events.py:82`）是瞬时的 UI 数据类，`QueryEngine.submit_message()` 直接把它 yield 给 UI、Bridge、Autopilot 和 Channels。会话快照由 `services/session_storage.py` 单独保存，日志、审计、指标各自为政。

结果是：

- 无法从持久记录重建模型历史、工具结果和 usage。
- 无法对同一轮对话同时提供 UI 流、审计流和指标流。
- 出错时只能依赖进程日志，不能回放事件。

DSH 的做法是：会话日志是模型上下文的唯一来源，“模型可见即已记录”；`Session.append()` 先提交再发布 `session/event`，`deriveMessages()` 从日志投影模型历史（`docs/architecture.zh.md:128-136`，`packages/core/session/src/index.ts:809`、`:858`、`:1079`）。投影是纯 fold，可在统一 watermark 上快照并 checkpoint（`packages/session/session-projection/src/index.ts:83-133`、`:514-528`、`:711`）。

## Decision

1. 引入追加式 `AgentEvent` 日志，作为会话事实的唯一来源。
2. `AgentEvent` envelope 固定包含：`event_id`、`session_id`、`turn_id`、`step_id`、`seq`、`ts`、`kind`、`payload`、`schema_version`、`visibility`。
3. `visibility` 分为 `session_private` 与 `user_visible`。`ReasoningBlock.raw` / `encrypted_content` 只允许 `session_private`，不得进入 UI、审计、指标或遥测。
4. 事件分三类：
   - 持久会话事件：`session.created`、`turn.start/end`、`step.start/end`、`user.message`、`assistant.message`、`tool.call/result`、`context.compacted`、`permission.decided`。
   - 实时 Agent 事件：`assistant.text.delta`、`tool.started/completed`、`status`、`error`、`compact.progress`。
   - 能力事件：`model.request/stream`、`tools.pre/post`、`telemetry.*`。
5. `Projection` 的 `apply(state, event)` 是同步纯函数，不做 I/O；不感兴趣时返回同一引用。`snapshot(as_of_seq)` 返回只读视图。
6. `EventSubscriber` 负责异步副作用：`UIEventSubscriber`（队列）、`JSONLEventSubscriber`（落盘）、可选 `AuditSubscriber`。`Projection` 不写盘、不 yield。
7. 存储分层：`EventLogStore` 是唯一事实源；`SessionStore` 是 snapshot/list/export/fork 的高层 facade，内部使用 `EventLogStore`。
8. `QueryEngine.submit_message()` 保持返回 `AsyncIterator[StreamEvent]`，内部由 `UIEventSubscriber` 适配，UI 不改。
9. 事件只追加、不修改；模型可见的事实必须先成为事件。
10. Schema freeze 只覆盖 envelope、事件 kind 名称、`seq`/`event_id` 语义；payload 允许使用 `schema_version` additive 演进，破坏性变更需新增 ADR。

## Consequences

### Positive

- 会话可以从日志重建，支持回放、审计、fork、遥测。
- UI、审计、指标共享同一事件源，不会出现三套事实。
- 投影是纯函数，容易测试和 checkpoint。

### Negative / Trade-offs

- 事件 schema 需要版本管理，旧日志需要迁移或兼容读取。
- 每次模型可见输入都要显式记录，短期增加代码量。
- 有界队列和 subscriber fan-out 需要考虑背压。
- `Projection` 纯度需要测试与代码审查保障；I/O 误入 fold 会导致重放失真。
- `session_private` 事件必须经过脱敏投影才能进入 UI/审计/指标，否则会泄露 provider-opaque 状态。
- `EventLogStore` 与 `SessionStore` 的分层需要明确，避免双写分叉。

## Migration

1. Phase 0A 先补 UI 事件 golden 与全量测试基线。
2. 引入 `AgentEvent`、`EventSink`、`EventSubscriber`、`EventLogStore`，把 `run_query` 的现有事件映射进去。
3. 增加 `UIEventSubscriber`，保持 UI 行为不变；随后冻结 envelope/kind/seq，payload 允许 additive 演进。
4. 增加 `SessionLogState` + `JSONLEventLogStore`，双写快照与 JSONL，验证可重建。
5. 增加 `AuditState` + 可选 `AuditSubscriber`、`MetricsState`；所有 `session_private` 字段在进入这些 subscriber 前脱敏。
6. 把 `SessionStore` 实现为 `EventLogStore` 的 facade，切换为日志优先，保留旧快照读取。
7. 增加 turn 边界一致性测试：`derive_messages()` 与 `QueryEngine._messages` 必须一致。

## Alternatives

- **继续使用瞬时 StreamEvent + 快照**：无法回放，也无法多路投影。
- **只增加结构化日志（logging）**：日志不是事实源，无法保证顺序和可重建。
- **直接引入外部事件总线**：增加部署依赖；先做进程内 seam，后续可替换。

## Implementation

- `src/openharness/engine/events.py`：`AgentEvent`、`EventSink`、`EventSubscriber`、`Projection`、`EventSequencer`、`ProjectionRegistry`。
- `src/openharness/engine/projections/`：`SessionLogState`、`AuditState`、`MetricsState`、`CodexReasoningState`。
- `src/openharness/engine/subscribers/`：`UIEventSubscriber`、`JSONLEventSubscriber`、`ProjectionSubscriber`。
- `src/openharness/services/event_log.py`：`JSONLEventLogStore` / `InMemoryEventLogStore`。
- `src/openharness/services/event_pipeline.py`：默认 pipeline 与恢复逻辑。
- `UI runtime` 创建 pipeline 并通过 `QueryEngine` 发射事件；UI 仍消费原有 `StreamEvent`。
- 测试：`tests/test_engine/test_events.py`、`tests/test_services/test_session_store.py`。

## References

- `docs/architecture.zh.md:71-79`（三类事件域）
- `docs/architecture.zh.md:128-136`（会话日志与投影）
- `packages/core/session/src/index.ts:809`、`:858`、`:1079`
- `packages/session/session-projection/src/index.ts:83-133`、`:514-528`、`:711`
