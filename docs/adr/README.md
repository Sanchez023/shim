# Architecture Decision Records

本目录记录 OpenHarness 运行时架构改造的决策。每份 ADR 只记录一个决策，包含背景、决定、后果与被否决的替代方案。

## 索引

| ADR | 标题 | 状态 |
|---|---|---|
| [0001](0001-capability-seams.md) | 用 Capability Seam 替代隐式依赖 | Implemented |
| [0002](0002-event-log-and-projections.md) | 以追加式会话日志和分层投影作为可观测性基础 | Implemented |
| [0003](0003-codex-integration.md) | Codex 集成采用“Provider 保真 + app-server 后端”双轨 | Implemented（真实 handshake/thread smoke 通过；完整模型轮次需 Codex 登录） |
| [0004](0004-security-dimensions.md) | 拆分工具权限、沙箱、审批与密钥访问四个安全维度 | Implemented |
| [0005](0005-context-and-subagent-seams.md) | 上下文治理与子 Agent 后端可插拔 | Implemented |
| [0006](0006-turn-executor-decomposition.md) | QueryEngine 拆分为 SessionManager / TurnExecutor / StepRunner | Implemented |
| [0007](0007-prompt-management.md) | 组合式系统提示词与项目规则管理 | Implemented |
| [0008](0008-prompt-cache-stability.md) | 提示词缓存的稳定性、观测与预热 | Implemented |
| [0009](0009-canonical-paths-and-deprecation.md) | 以不变量与 canonical 路径收敛功能面（不删除能力） | Proposed |
| [0010](0010-dependency-direction-and-ports.md) | 依赖单向化与端口化（含 turn loop 收敛的边界） | Accepted（D4 部分待办） |
| [0011](0011-test-bands-and-provider-contract.md) | 三层测试带、真 provider 契约与不变量守护 | Proposed |
| [0012](0012-interrupt-semantics.md) | 未完成不是完成：turn 中断语义的 fail-closed 收敛 | Accepted（D4 待办） |
| [0013](0013-permission-verdict-tristate.md) | 权限判定的三态化：可判定、不可判定、需确认 | Accepted |
| [0014](0014-completion-gate.md) | 完成判定进入运行时（验证账本 + 一次纠正） | Implemented |
| [0015](0015-tool-images-and-vision-passthrough.md) | 工具产出的图片以 image block 进上下文 | Accepted |

## 状态说明

- **Proposed**：方向已确定，等待实现与评审。
- **Accepted**：已达成共识并进入实现。
- **Implemented**：已落地，文档描述当前行为。
- **Superseded**：被后续 ADR 取代。

## 模板

```markdown
# ADR-NNNN: 标题

- 状态: Proposed
- 日期: YYYY-MM-DD
- 决策者: ...

## Context

问题、约束、相关事实。

## Decision

做出的决定，以及必须遵守的约束。

## Consequences

### Positive
### Negative / Trade-offs

## Migration

分阶段落地方式与回滚点。

## Alternatives

被否决的方案及原因。

## References

源码、文档、外部资料。
```
