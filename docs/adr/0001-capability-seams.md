# ADR-0001: 用 Capability Seam 替代隐式依赖

- 状态: Implemented
- 日期: 2026-11-15
- 决策者: OpenHarness maintainers
- 相关: ADR-0002, ADR-0003, ADR-0004, ADR-0005

## Context

`QueryEngine`（`src/openharness/engine/query_engine.py:21`）和 `run_query`（`src/openharness/engine/query.py:633`）直接持有或构造具体实现：API client、`ToolRegistry`、`PermissionChecker`、`CostTracker`、压缩服务。要替换其中任何一个，都需要改核心循环或 monkeypatch 内部函数。

DeepSeek Harness（DSH）的运行时把每个能力都做成 seam：Service Definition（接口）、Service Provider（实现）、Consumer（通常是面向模型的工具）。注册是副作用，卸载时自动撤销；默认 agent loop 也只是通过 factory 注册的一个 provider（`docs/architecture.zh.md:140-146`，`packages/core/agent/src/index.ts:171-206`）。

OpenHarness 不需要移植 Cordis，但需要同样的**可替换性**：默认实现不变，替换一个 provider 不影响其他部分。

## Decision

1. 为每个可替换能力定义 Python `Protocol`，但采用 **just-in-time** 方式：只在下一位 consumer 真正需要时定义，不要求一次冻结全部接口。
2. Phase 1 定义并落地四个核心协议：`EventSink`、`EventSubscriber`、`EventLogStore`、`Projection`。
3. **Projection 与 EventSubscriber 分离**：`Projection.apply()` 是同步纯 fold，不做 I/O；JSONL 写入、UI 队列、审计落盘属于 `EventSubscriber`。
4. **存储分层**：`EventLogStore` 是唯一事实源；`SessionStore` 是 snapshot/list/export/fork 的高层 facade，内部使用 `EventLogStore`，不新增第三套存储。
5. **状态可见性分层**：会话私有日志可保存 provider-opaque reasoning（含 `encrypted_content`）；UI/审计/指标/遥测投影必须脱敏。
6. Phase 2 新增 `CapabilityRegistry`，解析顺序为 `explicit > settings > default`；同时定义 `ToolExecutor`、`ModelClient`、`ApprovalGate`（仅协议）。
7. Phase 3A 定义安全四维协议；Phase 3B 定义 `ContextGovernor`；Phase 4 定义 `SubagentProvider`；Phase 5 定义 `AgentBackend`。
8. `CapabilityBundle` 按 session 创建，不放在全局可变单例中。
9. `register()` 必须返回 disposer；用 `contextlib.ExitStack` 逆序释放。注册与生命周期是同一件事。
10. 旧实现全部保留为默认 provider；旧构造参数转换为默认 bundle，保证向后兼容。
11. 协议按域拆分文件（`runtime/seams/core.py`、`execution.py`、`governance/security.py`、`governance/context.py`、`backends.py`），避免并行阶段改同一文件。
12. 不移植 Cordis 的 proxy/reflection、TypeScript declaration merging、HMR loader。

## Consequences

### Positive

- 替换工具执行器、权限策略、事件接收器、Agent 后端时不需要改 `run_query`。
- 每个 session 可以拥有不同的能力组合，便于测试、远程沙箱和外部 Agent 后端。
- 默认路径保持稳定，迁移可以逐 seam 进行。

### Negative / Trade-offs

- 增加一层间接调用和注册表，调试时需要看 bundle 解析结果。
- 必须严格管理 disposer 顺序，否则会出现半初始化状态。
- Protocol 需要持续维护，避免变成第二个 god object。
- `Projection` 纯度需要代码审查和测试保障；一旦在 fold 内做 I/O，重放和快照会失真。
- `EventLogStore` 与 `SessionStore` 的分层需要明确，否则容易出现双写分叉。

## Migration

1. Phase 0A：先跑全量测试并补 UI 事件 golden，建立行为基线。
2. Phase 0B：完成 reasoning/thinking 状态透传。
3. Phase 1：定义 `EventSink`、`EventSubscriber`、`EventLogStore`、`Projection` 并接入 `run_query`；`Projection` 保持纯 fold，I/O 放在 subscriber；结束时只冻结 envelope/kind/seq，payload 允许 additive 演进。
4. Phase 2：落地 `CapabilityRegistry`，同时定义 `ToolExecutor`、`ModelClient`、`ApprovalGate`。
5. Phase 3A / 3B 并行：安全四维与上下文治理各自定义协议并接入。
6. Phase 4：定义 `SubagentProvider`，先把 Codex 作为一次性子 Agent 后端跑通。
7. Phase 5：定义 `AgentBackend`，把 `QueryEngine` 收敛为 session facade。
8. 每个 seam 都有 fake provider 测试；默认 provider 必须通过旧测试。

关键约束：不要为了“以后可能需要”而提前定义 Protocol；每个协议都应有当前 Phase 的真实 consumer。

## Alternatives

- **直接重写为 Cordis 风格插件树**：成本过高，且需要重写 UI、工具和 Provider 生态。
- **继续用 monkeypatch/子类替换**：无法覆盖 per-session 隔离，也无法保证卸载顺序。
- **只做依赖注入容器，不做 seam 契约**：容器无法表达“哪些能力可以替换、替换后语义是什么”。

## Implementation

- `src/openharness/runtime/registry.py`：`CapabilityRegistry` 与 `CapabilityBundle`。
- `src/openharness/runtime/scope.py`：`SessionScope` + `ExitStack` 逆序释放。
- `src/openharness/runtime/seams/`：`core.py`、`execution.py`、`governance/security.py`、`governance/context.py`、`backends.py`。
- `src/openharness/runtime/assembly.py`：`CapabilityModule` 与 `CapabilityAssembler`，按依赖安装、逆序卸载（实验性，不进入 `openharness.runtime.__all__`）。
- `QueryEngine` 接受 `capability_bundle`，并把 tool executor、approval gate、event sink、context governor、security policy 注入 `QueryContext`。
- `QueryEngine` 的三层职责拆分见 [ADR-0006](0006-turn-executor-decomposition.md)。
- 测试：`tests/test_runtime/test_seams.py`、`tests/test_runtime/test_assembly.py`。

## References

- `docs/architecture.zh.md:140-146`（Capability Seam 定义）
- `packages/core/agent/src/index.ts:171-206`、`:246`、`:377`（AgentFactory/Registry）
- `packages/core/scope/src/index.ts:105-185`（per-agent scope）
- `docs/cordis-api/fiber.md:8-25`（effect/逆序清理）
