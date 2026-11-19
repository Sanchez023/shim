# ADR-0004: 拆分工具权限、沙箱、审批与密钥访问四个安全维度

- 状态: Implemented（tool / secret / sandbox / approval 四个维度均有 provider；默认 sandbox provider 为 allow，因为现有 sandbox runtime 仍在执行时强制边界）
- 日期: 2026-11-15
- 决策者: OpenHarness maintainers
- 相关: ADR-0001, ADR-0002

## Context

当前安全检查集中在 `PermissionChecker.evaluate()`（`src/openharness/permissions/checker.py:57`），同时处理：

- 工具 allow/deny 列表；
- 路径规则与内置敏感路径（`SENSITIVE_PATH_PATTERNS`）；
- 命令 deny 模式；
- 是否需要用户确认。

沙箱配置在 `sandbox/`，审批通过 `run_query` 的 `permission_prompt` 回调，密钥访问只靠敏感路径匹配。四类决策混在一起，导致：

- 无法单独替换沙箱策略而不影响工具权限；
- 无法审计“哪一维度拒绝了什么”；
- 新增执行后端（远程沙箱、Codex app-server）时难以复用策略。

DSH 把沙箱、审批、工具执行分开，并通过能力事件把策略附加到 seam（`docs/architecture.zh.md:75`、`:140-146`）。

## Decision

拆分四个独立维度，每个维度实现同一个 `Policy` 协议：

1. `ToolPermissionPolicy`：工具 allow/deny、路径规则、敏感路径。
2. `SandboxPolicy`：文件系统读写根、网络、进程执行；复用 `sandbox/` 现有配置。
3. `ApprovalGate`：何时询问、终端/自动批准；复用现有 `permission_prompt`。
4. `SecretAccessPolicy`：凭据文件、环境变量、OAuth token 的读取限制；复用 `SENSITIVE_PATH_PATTERNS`。

`CompositePolicy` 按固定优先级合并：

```text
hard deny > sandbox deny > explicit deny > ask > allow
```

每次决策返回 `PolicyDecision(dimension, allowed, reason)`，并发射 `permission.decided` 事件。审计投影负责脱敏，不记录密钥内容。

`LocalToolExecutor` 必须统一走策略链，禁止工具绕过。

## Consequences

### Positive

- 每个维度可独立替换和测试，默认行为保持不变。
- 审计可以回答“哪个维度、基于什么规则拒绝了请求”。
- 远程沙箱或 Codex 审批可以复用同一套策略接口。

### Negative / Trade-offs

- 组合优先级需要明确测试，否则容易出现“一个维度允许、另一个拒绝”的歧义。
- 拆分后需要迁移现有权限测试，短期增加维护量。
- 审计事件必须脱敏，否则会引入新的泄露面。

## Migration

1. 从 `PermissionChecker` 抽出纯函数，保持现有 `evaluate()` 行为。
2. 引入 `PolicyDecision` 与 `CompositePolicy`，先用默认配置跑通旧测试。
3. 接入 `permission.decided` 事件与审计投影。
4. 再把 `SandboxPolicy`、`SecretAccessPolicy` 从 checker 中移出。
5. 最后接入 `ApprovalGate`，支持终端与 app-server 两种实现。

## Alternatives

- **保持单一 `PermissionChecker`**：无法独立替换，也无法按维度审计。
- **把安全完全交给沙箱**：工具级 allow/deny、密钥访问和审批无法由沙箱表达。
- **把审批放在 UI 层**：Bridge、Autopilot、Channels 会各自实现一套，行为不一致。

## Implementation

- `src/openharness/runtime/seams/governance/security.py`：`SecurityRequest`、`SecurityDecision`、`PermissionCheckerPolicy`、`CompositeSecurityPolicy`。
- `src/openharness/engine/query.py`：工具执行统一走策略链，并发射 `permission.decided`。
- `src/openharness/engine/projections/audit.py`：审计 fold 与敏感字段脱敏。
- 测试：`tests/test_permissions/test_policy_matrix.py`、`tests/test_engine/test_events.py`。
- `SecretAccessPolicy` 已独立拒绝凭据路径；`SandboxPolicy` 默认 provider 为 allow，现有 sandbox runtime 仍在执行时强制边界；`ApprovalGate` 已接入工具执行。

## References

- `src/openharness/permissions/checker.py:41-120`
- `src/openharness/sandbox/`
- `src/openharness/engine/query.py:887`（`_execute_tool_call`）
- `docs/architecture.zh.md:75`、`:140-146`
