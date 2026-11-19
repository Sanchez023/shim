# ADR-0008: 提示词缓存的稳定性、观测与预热

- 状态: Implemented
- 日期: 2026-11-17
- 决策者: OpenHarness maintainers
- 相关: ADR-0007

## Context

OpenHarness 同时支持 Anthropic 的显式缓存（`cache_control`）和 DeepSeek/OpenAI 的隐式前缀缓存。实际使用中出现了三类问题：

- **看不见**：OpenAI-compatible 适配器只读 `prompt_tokens_details.cached_tokens`，不读 DeepSeek 的 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`；带 tools 时还会无条件移除 `stream_options`，导致 usage chunk 缺失。
- **前缀中段漂移**：cwd、git branch、date 位于 `base`（order 0）；permission mode、fast mode、effort 位于 order 10/15/20；glob-scoped Cursor 规则混在 order 50。切换 plan mode、`/fast`、`/effort`、`@file` 或 `git checkout` 都会让稳定前缀从中间断开。
- **工具中段插入**：`tool_search` 激活的 deferred 工具按注册位置插入 schema 列表中间，导致 tools 段之后的缓存全部失效。
- **重建丢失断点**：`set_system_prompt()` 只更新文本，不更新 `cacheable_prefix`；Anthropic 的 `text.startswith(prefix)` 检查因此可能失败。

冷启动本身无法消除：每种模型/前缀的首次请求必然 miss。问题在于不要把可修复的漂移误认为冷启动，也不要用预热去粉饰报表。

## Decision

1. **统一 usage 归一化**：OpenAI-compatible 适配器按 `prompt_tokens_details.cached_tokens` → `prompt_cache_hit_tokens` → `prompt_cache_miss_tokens` 的顺序解析，并统一成 `input_tokens` / `cache_read_input_tokens`。`stream_options.include_usage` 默认保留，仅对 Kimi/Moonshot（或显式 `OPENHARNESS_DROP_STREAM_OPTIONS=1`）移除；若 provider 返回 400 且错误提到 `stream_options`，自动去键重试一次并记住。
2. **稳定前缀只放稳定字节**：环境段拆成 stable（OS/架构/shell/Python/venv/git-repo）与 live（cwd/date/branch）。live 段放在 order 95 的尾部非 cacheable section。permission/fast/effort 移到 order 80–82，glob-scoped 规则移到 order 85，均非 cacheable。
3. **工具 schema 只追加**：`ToolRegistry.to_api_schema(append_order=...)` 把 deferred 工具移到列表尾部；`QueryEngine` 维护 `_loaded_tool_order`，`activate_tools` 只追加、不重排，并同步当前 turn 的 `QueryContext`。
4. **重建同步断点**：`QueryEngine.set_system_prompt(prompt, cacheable_prefix=None, section_hashes=None)` 同时更新文本、连续可缓存前缀和 section 哈希；新增 `set_system_prompt_result()`。所有运行时重建点改用 `build_runtime_system_prompt_result()`。
5. **本地归因**：`engine/cache_diagnostics.py` 对每次请求计算 system/tools/history 指纹，分类为 `cold_start` / `system_drift` / `tools_drift` / `history_append` / `history_rewrite` / `unchanged`。只有稳定前缀变化才算 drift；仅尾部变化记为 `tail_changed`。当非冷启动、非压缩的请求命中率低于 70% 且 miss 超过 max(2000, 5%) 时发出告警。
6. **预热是可选延迟优化**：`CacheWarmup` 在会话启动时后台发送一次 `max_tokens=1`、`stream=False` 的请求，按 provider/model/system/tools 前缀和 TTL 去重。默认对 `deepseek`/`openai` 启用，`OPENHARNESS_CACHE_WARMUP=0|1` 可覆盖。OpenAI-compatible 与 Anthropic-compatible 客户端都实现了 `warmup()`；后者需要一条位于 system+tools 之后的最小 user 消息，因此命中覆盖的是稳定前缀，而不是那条 dummy 消息。预热 usage 单独记账，不计入用户可见命中率；不做空闲会话定时 ping。

## Follow-up: freeze the prefix and budget the new tokens

真实测试后追加了以下约束：

1. **稳定 system prompt + 逐轮 user context**：`build_stable_system_prompt_result()` 只包含 base、personality、model profile、tool discovery、skills、delegation、always-apply 项目规则和 local rules；`build_turn_context()` 包含 permission/fast/effort、memory、relevant memory、scoped rules、issue/PR/repo context、cwd/date/branch。`QueryEngine` 在请求时把 turn context 作为 `<turn_context>` 文本块附加到最新 user 消息，**不写入 session history**。因此 system+tools 的字节在整个会话内不变，预热和首轮真实请求完全对齐。
2. **cache budget**：`engine/cache_budget.py` 用上一次请求的 `prompt_tokens` 计算 `N_budget = P × (1-target)/target`（默认 target=0.95）。工具结果超过预算时，最大的结果优先落盘到 `tool_artifacts/`，只保留短预览；完整输出仍可被 `read_file`/`grep` 读取。
3. **压缩冷却**：`OPENHARNESS_COMPACT_COOLDOWN_SECONDS`（默认 180s）内不再重复压缩，除非接近硬上限。DeepSeek 的上下文窗口按 128k 计算，避免迟到的 reactive compact。
4. **预热等待**：首次 `submit_message` 会短暂等待后台 warmup（`cache_warmup_wait_seconds`，默认 2s），headless `-p` 不再和缓存写入竞态。
5. **wire-format 指纹**：`result_metadata`、`provider_item_id`、`visibility`、`source_path` 不参与 hash，避免本地记账变化被误判为 `history_rewrite`。
6. **大段用户文本落盘**：`engine/large_paste.py` 对纯文本 user 消息做长度检查（默认 24,000 字符）。超过阈值时把全文写入 `<data>/user_pastes/`，消息体替换为 `[Large paste offloaded]` 标记、首尾预览和 artifact 路径；模型需要精确内容时用 `read_file` 的 offset/limit 或 `grep` 读取。该变换只作用于纯文本 user 消息，不碰图片、工具结果和 assistant 消息；数据目录不可写时回退为原文。`OPENHARNESS_OFFLOAD_LARGE_PASTES=0` 可关闭，阈值和预览长度可用环境变量调整。
7. **空闲/长工具保温**：`CacheKeepalive` 在 `OPENHARNESS_CACHE_REFRESH=1` 时为隐式缓存 provider 提供 opt-in keepalive。它跟踪最近一次真实模型请求的时间（`step_runner` 在请求前后调用 `note_model_request_start/end`），超过 `cache_refresh_idle_seconds`（默认 600s）没有请求时发送一次强制 `max_tokens=1` 预热；有真实请求在途时跳过，避免并发重复。默认关闭：provider TTL 不公开，定时刷新本身会消耗 token。`/usage` 单独报告 refreshes/busy-skips/failures。

## Consequences

### Positive

- DeepSeek 的缓存命中终于可见，`/usage` 能同时报告整体命中率、热请求命中率和冷启动次数。
- 切换 plan mode、`/fast`、`/effort`、`@file` 或 git 分支不再打断稳定前缀。
- `tool_search` 激活工具不再破坏 tools 段缓存。
- 漂移可归因到具体 section，避免把压缩或冷启动当成缓存失效来优化。
- 预热可把首次请求的 miss 移出关键路径，并支持同进程短会话复用。

### Negative / Trade-offs

- 预热本身不减少总 token（前缀仍支付一次 miss），只是把 miss 提前并额外支付一次 hit；默认仅在隐式缓存 provider 上启用，且不计入命中率。
- 运行时开关移到尾部可能轻微影响模型对 plan mode 指令的即时注意力；尾部仍紧邻最新 user 消息。
- 归因依赖本地指纹，无法看到 provider 的真实分词边界；DeepSeek 的 64-token 块对齐仍会造成约 1 个块的天然 miss。
- `CacheWarmup` 是进程内去重；多个 `oh` 进程各自预热一次。
- 大段文本落盘后，模型必须显式调用 `read_file`/`grep` 才能看到全文；预览只覆盖首尾，因此首轮回答可能多一次工具调用。粘贴内容会留在数据目录中，目前不做自动清理。
- keepalive 默认关闭；开启后会在长工具或空闲期间额外发送一 token 请求，且刷新时机只能按本地时钟近似 provider TTL。

## Validation

2026-11-17 用真实 `deepseek-chat` 调用验证：

| 路径 | 请求 | 结果 |
|---|---|---|
| Anthropic 网关 `https://api.deepseek.com/anthropic` | 3 轮 / 6 次请求 | 首次 14.8%（1024/6925），随后 96.0% / 96.2% / 96.7% / 97.0% / 97.4%；整体 84.0%，剔除首次后 96.7%；drift / alert 均为 0 |
| OpenAI-compatible `https://api.deepseek.com` | 3 次请求 | 0% → 92.7% → 92.3%；带 tools 时 `stream_options.include_usage` 被接受 |
| Anthropic 预热 + 1s 间隔 | 1 次预热 + 2 次真实请求 | 预热 miss 2584；真实请求 hit 2432/2588 = 94.0%，重复请求相同 |
| 完整 `oh -p`（运行时预热启用） | 1 轮 / 2 次请求 | 首次 3584/7285 = 49.2%，第二次 7296/7684 = 95.0% |
| 冻结 system prompt + 持久化 turn context | 3 轮 / 5 次请求 | 首次 6528/7000 = 93.3%，后续 96.3% / 96.7% / 97.7% / 98.0%；整体 **96.43%**，drift / rewrite / alert 均为 0 |

Anthropic 网关通过 `cache_read_input_tokens` 报告命中，`cache_creation_input_tokens` 始终为 0。预热写入存在最终一致性：紧接预热后的第一次真实请求可能仍然 miss，间隔 1 秒或正常交互输入延迟后即可命中。Anthropic 预热的一 token dummy user 消息位于 system+tools 之后，真实请求只 miss 自己的 user 消息。

## Implementation

- `api/usage.py`、`api/openai_client.py`：usage 归一化、`stream_options` 条件化、`warmup()`。
- `engine/cache_diagnostics.py`、`engine/cache_warmup.py`：指纹归因、预热控制器与 `CacheKeepalive`。
- `engine/large_paste.py`：大段纯文本 user 消息落盘为 `user_pastes/` artifact。
- `engine/query_engine.py`、`engine/step_runner.py`：缓存诊断注入、工具 append-only、断点同步、请求在途标记与 keepalive 生命周期。
- `prompts/system_prompt.py`、`prompts/context.py`、`prompts/composer.py`、`prompts/claudemd.py`：stable/live 拆分、section 重排、section SHA-1。
- `tools/base.py`：`to_api_schema(append_order=...)`。
- `ui/runtime.py`、`ui/backend_host.py`、`ui/protocol.py`、`commands/builtin/session.py`：启动预热、状态栏与 `/usage`。
- 测试：`tests/test_api/test_usage.py`、`tests/test_api/test_openai_client.py`、`tests/test_engine/test_cache_diagnostics.py`、`tests/test_engine/test_cache_warmup.py`、`tests/test_engine/test_tool_ordering.py`、`tests/test_prompts/test_prompt_composer.py`。
