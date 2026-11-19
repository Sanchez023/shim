# ADR-0012: 未完成不是完成 —— turn 中断语义的 fail-closed 收敛

- 状态: Accepted（D1/D2/D3/D5/D6/D7 已落地；D4 的 `finally` shield 待办）
- 日期: 2026-11-18
- 决策者: OpenHarness maintainers
- 相关: ADR-0002, ADR-0004, ADR-0006, ADR-0011

## Context

ADR-0006 拆出了 `TurnExecutor` / `StepRunner`，ADR-0008 与 2026-11-18 的传输层加固把重试收口到了
`api/transport.py`，`api/finish_reason.py` 也给了引擎一套统一词汇。但**中断语义仍然是乐观的**：
流被截断、连接被断、用户取消、沙箱不可用这四种「没正常结束」的状态，代码里都还有把它们当成
正常完成继续推进的路径。下面每条都来自实测代码，不是推测。

### 1. 截断只报警，不拦截副作用

`engine/step_runner.py:240-255` 在 `is_truncated(finish_reason)` 为真时只发一个 `StatusEvent`，
然后流程继续走到 `:281` 把 `final_message.tool_uses` 拿去 dispatch。也就是说：模型在写文件时
撞上 `max_tokens`，只要被截断的 `arguments` 恰好还是一段合法 JSON，工具就会执行，把半截内容
写进文件。`:241-243` 的注释把这个后果描述得很准确（"silently persists a half-finished file"），
但代码选择的是警告后继续。

### 2. 完成信号不校验

三个适配器都不要求见到流的终止帧（Anthropic 的 `message_stop`、OpenAI 的 `[DONE]`/`completed`）：
`api/client.py:478-499`、`api/openai_client.py:600`、`api/codex_client.py:452`。连接在最后一个
delta 之后被掐断，仍然会产出 `ApiMessageCompleteEvent`，`finish_reason` 落到
`FinishReason.UNKNOWN`（`api/finish_reason.py:22`），而这个值目前**没有任何消费者**，
于是「没结束」被当作「结束了」持久化进事件日志。

### 3. 取消没有单一负责人

- 审批 gate 是无限 `await`：`engine/tool_dispatch.py:234-247`，用户不回答就永远挂住。
- MCP 客户端没有请求超时（`mcp/`），只有 bash / web_fetch / agent 自带超时。
- `engine/query_engine.py:892-906` 的 `finally` 里有 5 个 `await`；第二次取消会让
  `_commit_messages` 与 `turn.end` 都被跳过 —— 会话记录与服务端事实不一致。
- `ui/backend_host.py:312` 吞掉 `CancelledError` 并 `return True`。
- `engine/step_runner.py:353-373` 用 `gather(return_exceptions=True)`，随后把子任务的
  `CancelledError` 统一转成 `is_error=True` 的工具结果：**取消被伪装成工具失败**，
  批次里剩下的工具照常继续执行。

### 4. 沙箱静默降级

`sandbox/adapter.py:116-120`：`sandbox.enabled` 为真但 runtime 不可用时，只有
`fail_if_unavailable` 也为真才抛错，而这个开关默认是 `False`（`config/settings_schema.py:106`）。
开了沙箱的配置在 Docker 缺席时**静默回落宿主执行**，用户无法从行为上分辨。

### 5. 重试谓词三份

循环已经收口到 `api/transport.py:146` 的 `stream_with_retry`，但「哪些错误可重试」仍是三份独立实现：
`api/client.py:164`、`api/openai_client.py:692`、`api/codex_client.py:496`。同时
`api/errors.py` 的 `classify_api_error` 会产出 `retryable` 字段，但全仓没有任何调用方读它 ——
元数据与实现互相矛盾，漂移只是时间问题（529/504 在 Anthropic 路径可重试、在 OpenAI 兼容路径不可）。

### 6. 会话落盘顺序不安全

`services/session_storage.py:116-122` 先写 `latest.json`，再写 `session-<id>.json`。写第二个文件时
崩溃，会留下指向不存在会话的 `latest.json`。

## Decision

**统一原则：turn 只有「明确完成」与「明确未完成」两种结局，任何无法确认的状态都不推进副作用，
且每个中断都必须有唯一负责人。**

### D1 截断即中止本 step 的工具执行

`is_truncated()` 为真时，本 step 不 dispatch 任何 `tool_use`。为避免破坏 tool_use/tool_result
配对（ADR-0002 的不变量），每个未执行的调用回填
`ToolResultBlock(is_error=True, content="not executed: response truncated at max_tokens")`，
由模型在下一轮自行决定是否重发。**不允许**在截断后执行任何工具，无论其 `arguments` 是否可解析。

### D2 完成信号必须显式

只有 `finish_reason ∈ {END_TURN, TOOL_USE, STOP_SEQUENCE}` 才产出「完成」投影。
`UNKNOWN` 与缺失终止帧一律标为 `incomplete`：事件日志记录 `status="incomplete"`，
不写「已完成」投影，并在消息上加 `truncated`/`incomplete` 标记供上层展示。

### D3 取消只有一个负责人

- turn 级：`TurnExecutor` 持有 watchdog deadline（默认值可配，`None` 表示不设）。
- 工具级：每个工具声明 `timeout_seconds`，dispatch 处统一套 `asyncio.timeout()`；
  MCP 请求也走同一层。
- 审批级：审批 gate 必须接受超时，**超时等于拒绝**，不是挂起。
- 语义约束：`CancelledError` 只能被记录后**重新抛出**。删除所有「把取消转成 `is_error`
  工具结果」「吞掉取消并 `return True`」的路径。

### D4 `finally` 中的提交不可被二次取消跳过

`query_engine` 的 `_commit_messages` 与 `turn.end` 用 `asyncio.shield()` 保护，或改为显式提交点
（正常路径与异常路径各自调用一次、幂等）。会话提交属于「必须发生」的副作用，不能由取消决定。

### D5 沙箱不可用即失败，不静默回落

`sandbox.enabled=true` 且 runtime 不可用时默认报错。新增
`sandbox.unavailable_policy: "deny" | "warn"`，默认 `deny`；`warn` 是显式选择的降级，
并且必须发 `StatusEvent` + 事件日志记录，不能静默。`fail_if_unavailable` 保留为兼容别名。

### D6 重试判定单一来源

删除三个 client 的 `_is_retryable` / `_get_retry_delay` / `_retry_after_seconds`，
统一委托 `api/errors.classify_api_error`（`retryable` + `retry_after`），退避计算只在
`api/transport.py` 实现一次。

### D7 会话落盘原子且有序

先写 `session-<id>.json`（tmp + rename），成功后再更新 `latest.json`；两者都走
`atomic_write_text`，并且 `latest.json` 必须指向已存在的会话文件。

## Consequences

### Positive

- 半截文件、半截 patch 不再落盘；截断从「静默数据损坏」变成「一次可重试的往返」。
- 取消/超时有了确定归宿，卡死的出口不再只有 Ctrl-C。
- 「重试可重试的错误」只有一处定义，provider 差异写在数据里而不是散在三个类里。
- 会话快照与事件日志的一致性不再依赖进程没被杀。

### Negative / Trade-offs

- 截断后多一轮往返（模型要重发），token 成本略升，换来正确性。
- 工具级超时可能中断合法的长任务：需要为 `bash`/`agent` 这类工具单独配置较大默认值。
- `unavailable_policy: deny` 会让「配了沙箱但没装 docker」的旧配置直接失败 —— 这是一次
  有意的破坏性变更，需要迁移提示（见 Migration）。
- `UNKNOWN` 不再算完成，会让某些宽松 provider 的行为被显式暴露出来，短期噪声增加。

## Migration

1. **P1（无行为变化）**：`errors.classify_api_error` 增加 `retry_after`；`transport` 改用它；
   保留三个 `_is_retryable` 作为薄转发并标记 deprecated。
2. **P2（行为变化，需 CHANGELOG）**：D1、D2、D7 落地；补 `tests/test_engine/test_truncation.py`
   与 `tests/test_services/` 下的落盘顺序用例。
3. **P3（需用户确认）**：D5 的默认值翻转，附 `settings` 迁移：读取到 `sandbox.enabled=true` 且
   无 `unavailable_policy` 时，写入 `warn` 并打印一次性提示，避免老配置突然全红。
4. **P4**：D3 的 watchdog 与工具超时表，先只对 `bash` / `mcp` / `agent` 三类生效，再推广。

回滚点：D1 与 D2 各自独立，可单独 revert；D5 的默认值翻转通过 `unavailable_policy` 设置即可退回。

## Alternatives

- **截断直接终止整个 turn**：更安全，但丢掉已生成内容，用户要重新提问。回填 `is_error`
  保留了已生成的部分并让模型自我修正，综合成本更低。
- **保留 `StatusEvent` 警告语义（现状）**：被否决 —— 警告不是约束，模型看不到就不会改。
- **取消时把工具结果标记为「已取消」而不是抛异常**：会让上层无法区分「工具失败」与「用户中止」，
  且无法真正停下批次。
- **在 `finally` 里用 `try/except CancelledError` 重试提交**：语义混乱，`shield` 更直接。

## References

- `engine/step_runner.py:240-255`、`:281`、`:353-373`
- `api/client.py:478-499`、`api/openai_client.py:600`、`api/codex_client.py:452`
- `api/finish_reason.py:13-52`、`api/errors.py`（`classify_api_error`）
- `api/transport.py:146`（`stream_with_retry`）
- `engine/tool_dispatch.py:234-247`、`engine/query_engine.py:892-906`
- `ui/backend_host.py:312`、`sandbox/adapter.py:116-120`、`config/settings_schema.py:106`
- `services/session_storage.py:116-122`
- ADR-0002（事件日志不加锁追加）、ADR-0004（安全四维）、ADR-0006（turn 拆分）、ADR-0011（测试带）
