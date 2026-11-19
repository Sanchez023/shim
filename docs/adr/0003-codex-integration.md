# ADR-0003: Codex 集成采用“Provider 保真 + app-server 后端”双轨

- 状态: Implemented（C0–C3b 代码与 fake-server 测试完成；真实 `initialize` + `thread/start` smoke 通过，完整模型轮次需要 Codex 登录）
- 日期: 2026-11-15
- 决策者: OpenHarness maintainers
- 相关: ADR-0001, ADR-0002

## Context

OpenHarness 已经支持 Codex 作为模型 Provider：

- `CodexApiClient` 直连 `chatgpt.com/backend-api/codex/responses`（`src/openharness/api/codex_client.py:221`）。
- `codex` profile 使用 `openai_codex` 与 `codex_subscription`（`src/openharness/config/settings.py:220`）。
- 请求已设置 `store: false` 与 `include: ["reasoning.encrypted_content"]`（`src/openharness/api/codex_client.py:254-264`）。

但状态不保真：

- `_convert_messages_to_codex()`（`src/openharness/api/codex_client.py:77`）只重建 message / function_call，`reasoning` item 全部丢弃。
- `function_call` 的 item id 被改写为 `fc_*`，原始 id 丢失。
- 消息模型原先没有 reasoning block，无法持久化或回放。

Codex 侧的现实：

- `codex-rs/model-provider-info/src/lib.rs:117` 的 `WireApi` 只剩 `responses`，`chat` 已移除（`:110`）。
- 会话循环在 `codex-rs/core/src/session/turn.rs:215` 的 `run_turn`，文件 3862 行；`session/mod.rs` 6218 行。直接移植到 Python 不现实。
- `codex-rs/app-server` 是稳定的 JSON-RPC 集成面。

因此“基于 Codex 改装 OpenHarness”必须拆成两层，分别验收。

## Decision

### 轨道 A：Codex 作为模型 Provider（保真）

- 引入 `ReasoningBlock`，保存 `raw`、`item_id`、`summary`、`encrypted_content`。
- `_convert_messages_to_codex()` 按原始 item 顺序回放 reasoning → message → function_call。
- `ToolUseBlock` 保留原始 `item.id` 与 `call_id`。
- 解析 `response.output_item.done` 的 `reasoning` / `message` / `function_call`，保存 `response.completed` 的 response id、usage、status。
- `encrypted_content` 只用于回放；**允许写入会话私有事件日志**，但不得进入 UI、审计、指标或遥测。投影层必须按 `visibility` 脱敏，只保留 `provider`、`item_id`、`summary` 是否存在等元数据。

### 轨道 B：Codex 作为 Agent Runtime（app-server）

轨道 B 再拆成两步，先验证协议，再替换主循环：

- **C3a：Codex 作为 `SubagentProvider`（优先）**
  - 每次委派 spawn 新 app-server、创建临时 thread（`{cwd, ephemeral: true}`）、只执行一个 turn。
  - 握手：`initialize` → `initialized`；随后 `thread/start`、`turn/start`、`turn/interrupt`。
  - 权限模式显式映射：
    - `never` → `approvalPolicy: never`；
    - `approve-for-me` → `on-request` + `auto_review` + `workspace-write`；
    - `dangerously-bypass-approvals-and-sandbox` → `never` + `danger-full-access`。
  - 只接受与当前 thread/turn 匹配的通知，等待 `turn/completed`；从 `agentMessage` 选 `phase: "final_answer"`，否则 `partial_answer`，否则 `phase: null`；`commentary` 永不选中。
  - 结果归一化为 `(output, structured, diagnostic, stop_reason)`，由 `SubagentManager` 统一生命周期。
  - **C3a 不接入人工 `ApprovalGate`**：它是无人值守路径，只验证 Codex sandbox 拒绝、turn 失败与取消。
- **C3b：Codex 作为 `AgentBackend`（后置）**
  - 复用 C3a 的 wire，实现主循环后端 `CodexAppServerBackend`。
  - 第一版仍采用一次性模型；默认无人值守，可显式接入 Phase 3A 的 `ApprovalGate` 进行人工审批。
  - 与 `LocalPythonBackend` 做同一场景的事件序列对照。
- 协议版本固定（参考 DSH 实现为 `0.153.4`），默认关闭，通过 settings 显式启用。

先做 C3a 的原因：它不依赖完整安全/上下文改造，能在 Phase 2 之后尽早验证 app-server 协议；即使 C3b 延期，OH 也已经具备“委派 Codex 执行任务”的能力。

### 共同约束

- 不移植 Rust 代码，只移植 item/事件协议与边界概念。
- 轨道 A 的 C0–C2 是轨道 B 的前置；C3a 从 Phase 4 开始，C3b 在 Phase 5 完成。C3a 与 C3b 分阶段验收。
- 同一组场景（纯文本、工具调用、取消）在 local / codex-provider / codex-app-server 三条路径上做对照测试；人工审批拒绝只在 C3b 验证。

## Consequences

### Positive

- Codex 推理状态可无损回放，减少缓存失效和协议错误。
- 复用 Codex runtime 时不需要改 OpenHarness 核心循环。
- 两条轨道可以独立发布和回滚。

### Negative / Trade-offs

- Codex Responses 的 item schema 会演进，需要 fixture 和版本适配。
- app-server 仍是实验性协议，升级可能破坏兼容。
- `encrypted_content` 与账号绑定，不能跨账号复用；只允许写入会话私有日志，投影与审计必须脱敏。
- C3a 的无人值守路径不覆盖人工审批；需要人工审批时必须等 C3b 接入 `ApprovalGate`。

## Migration

1. **C0（进行中）**：完成 `ThinkingBlock` / `ReasoningBlock`；保持 `serialize_content_block()` 旧签名，新增 `serialize_anthropic_block()`；修复 Codex 消息转换，补 SSE fixture 测试。
2. **C1**：将 Codex SSE item 事件投影为 `AgentEvent`；`visibility=session_private` 的 reasoning 只进入会话日志。
3. **C2**：`CodexApiClient` 实现 `ModelClient` seam。
4. **C3a（Phase 4，优先）**：实现 `SubagentProvider` + `SubagentManager`，用一次性 app-server 跑通 Codex 委派；验证 Codex sandbox 拒绝、turn 失败、取消；fake server 契约测试 + 真实 smoke test。
5. **C3b（Phase 5，后置）**：把同一 wire 提升为 `CodexAppServerBackend`，接入 `ApprovalGate`，与 `LocalPythonBackend` 做契约对照。
6. **C4**：建立 local / codex-provider / codex-app-server 三条路径的场景对照测试。

## Alternatives

- **把 OpenHarness 整体替换为 Codex CLI**：放弃 OH 的 Provider、UI、权限、压缩与子 Agent 生态。
- **把 Codex Rust turn loop 移植到 Python**：涉及 6000+ 行核心逻辑，风险和收益不匹配。
- **继续只做扁平消息适配**：无法解决 reasoning 状态丢失，也无法复用 Codex runtime。
- **长期复用 app-server thread/process**：第一版不采用；先验证一次性无人值守路径。

## Implementation

- `src/openharness/engine/messages.py`：`ReasoningBlock`、`ThinkingBlock`、`visibility`、Anthropic 序列化。
- `src/openharness/api/codex_client.py`：reasoning item 解析/回放、`provider_item_id`、`capabilities()`。
- `src/openharness/api/openai_client.py`：`reasoning_content` 迁移到 `ReasoningBlock`。
- `src/openharness/api/capabilities.py`：声明式 `ModelCapabilities`。
- `src/openharness/services/backends/codex_app_server.py`：进程 JSON-RPC transport、一次性 thread/turn、无人值守审批、`CodexAppServerSubagentProvider`、`CodexAppServerBackend`、`default_codex_command()`（codex 或 npx fallback）。
- `src/openharness/tools/codex_subagent_tool.py`：模型可见的 `subagent_codex` 工具。
- `AgentBackend` 选择：`agent_backend = "local" | "codex"`，CLI `--agent-backend codex`；`--codex-subagent` 暴露工具。
- 测试：`tests/test_api/test_codex_client.py`、`tests/test_api/test_client.py`、`tests/test_api/test_openai_client.py`、`tests/test_backends/test_codex_app_server.py`。
- 真实 smoke：`npx @openai/codex app-server --stdio` 的 `initialize` 与 `thread/start` 已通过；完整模型轮次需要本机 Codex 登录。

## References

- `src/openharness/api/codex_client.py:77`、`:221`、`:254-264`
- `src/openharness/config/settings.py:220`
- `codex-rs/model-provider-info/src/lib.rs:110`、`:117`、`:149`
- `codex-rs/core/src/session/turn.rs:215`
- `codex-rs/app-server`
- DSH `packages/subagent/subagent-codex/README.zh.md:48-52`、`:95`、`:110`
- DSH `packages/subagent/subagent-codex/src/wire.ts`
