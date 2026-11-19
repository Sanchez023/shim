# ADR-0015: 工具产出的图片以 image block 进上下文（不再拍成 base64 文本）

- 状态: Accepted（D1–D5 已落地）
- 日期: 2026-11-19
- 决策者: OpenHarness maintainers
- 相关: ADR-0008（prompt 缓存稳定性）, ADR-0010（依赖方向）, ADR-0011（provider 契约）
- 编号说明: 0014 已被 ADR-0014（完成判定门禁，分支 `feat/verification-gate` 待合入）占用，故跳号。

## Context

截图类工具（Playwright MCP 的 `browser_take_screenshot`、`image_generation`、渲染类工具）返回的是
MCP `ImageContent`：`{"type": "image", "mimeType": "image/png", "data": "<base64>"}`。
`mcp/client.py::call_tool` 只区分 text 与非 text，非 text 一律 `item.model_dump_json()`，
于是**整段 base64 变成工具结果里的文本**。实测一张 1280×720 的 PNG 是 31,684~65,820 chars。

这条链上有三个互相独立的缺口，任何一处单独修都不解决问题：

1. **能力判定认不出模型。** `api/provider.py::_MULTIMODAL_MODEL_PATTERNS` 只有 `^deepseek-vl` /
   `^deepseek-vision`，对 `deepseek-v4-flash`、`deepreasoning-ds-v4.1flash` 这类 v4 聊天模型返回
   `False`（`is_model_multimodal`，`provider.py:190`）。
2. **MCP 层把图片降级成文本**（上文的 `model_dump_json`）。
3. **domain 层没有放图片的位置。** `ToolResultBlock.content` 是 `str`（`domain/messages.py:63-69`），
   tool_result 里无法承载 image block。

结果是两个都不好的选择：让 base64 文本进上下文（约 900 tokens 的不可见噪音，最多能看到被截断的前
8000 chars），或者用 `--image-responses omit` 把它整个丢掉（2026-11-19 的临时规避，
见 `mcp-cache-impact-2026-11-19`）。

顺带澄清一个常见误解：**填 `vision` 三项不是修法**。`engine/step_support.py::_preprocess_images_in_messages`
在 `vision_model_config` 为空时提前 return，反而让 user 消息里的 `ImageBlock` 原样发给 API ——
即"粘贴图片"本来就能用。只有在 `is_model_multimodal` 认不出模型**且** `vision` 已配置时，
才会走"每张图调用一次 `image_to_text`"的回退路径。所以对 v4 这类模型，**修判定才是正解**，
填 `vision` 是负优化（每张图多一次往返）。

### 实测（2026-11-19）

- 直接向 `https://aicoding.wenge.com/v1/messages` 发原生 anthropic image block（64×64 左红右蓝 PNG）：
  200，模型答 "The left half is red and the right half is blue."，`input_tokens=204`，
  上游真实模型 `deepseek-v4-flash-202605`。网关与模型都支持图片输入。
- 改完后端到端：settings 里的 Playwright MCP → `browser_navigate` + `browser_take_screenshot`
  → 65,436 chars base64（49,077 字节 PNG）→ `ImageBlock` → 真实 client → 模型正确描述页面，
  `prompt_tokens=821`。

## Decision

### D1. 能力判定按模型家族扩展，别名也覆盖

`provider.py` 的模式表增加：

- `^deepseek-v[3-9]` —— `deepseek-v4-flash-202605` 这类上游名；
- `(?:^|[-\s/])ds-v[3-9]` —— 网关别名（`deepreasoning-ds-v4.1flash`），前缀任意但 `ds-v4` 段必须独立。

仍然保留"认不出就当不支持"的保守语义：误判为多模态会让图片直接撞到 provider 上（400），
误判为不支持只会多一次 `image_to_text` 往返。

### D2. MCP 调用把图片作为一等输出

`mcp/client.py::call_tool` 的返回类型从 `str` 改为 `McpToolOutput(text, images)`
（`client.py:32`，`__str__` 回落到 `text`，让只当字符串用的调用方继续工作）。
`_image_block_from_mcp` 只接受 `image/*` 且非空的 `data`，并有两条上限：
`MAX_IMAGES_PER_RESULT = 4`、`MAX_IMAGE_B64_CHARS = 4_000_000`（约 3 MB 二进制）。
被拒的图片不会静默消失，文本里补一行 `[N image(s) omitted: unsupported, oversized or over the per-call limit]`。
上限存在的理由：MCP server 是第三方进程，一个返回整页 gallery 的实现不该能撑爆 prompt。

`tools/base.py::ToolResult.images` 承接这些图片，`engine/tool_dispatch.py:346` 原样带到
`ToolResultBlock.images`。

### D3. 图片是 tool_result 的**兄弟块**，不在 tool_result 内部

provider 对"tool_result 里能不能有图片"并不一致：Anthropic 允许 content 数组里混 image，
OpenAI chat 的 `role: "tool"` 消息只有字符串 content。所以图片不进 tool_result，而是搬到
**同一条 user 消息**里、紧跟 tool_result 之后（`step_runner.py:51` 的 `_take_tool_images`）：

```
user:
  tool_result(toolu_1, "[1 image(s) returned]")
  text("[image from mcp__playwright__browser_take_screenshot]")
  image(png, base64…)
```

这样两个 adapter 都不需要改：`ConversationMessage.to_api_param` 直接序列化，
`openai_client._convert_anthropic_messages` 本来就把"同一条消息里的 tool_result 拆成 role=tool、
其余块合成一条 user 消息"当作既有路径（`openai_client.py:169-193`）。

`ToolResultBlock.images` 因此是**纯传输字段**：`Field(exclude=True)`，不进 wire payload、
不进 session dump、也不进缓存指纹；搬运完就清空，避免同一份 base64 出现两次。

### D4. UI 投影不发图片

`user.message` 事件里的 `message` 是**去掉图片**的版本（`step_runner.py:504-518`）：
TUI 不渲染图片，把 base64 塞进 stdout 管道只是把 500 KB 的噪音搬一次家。
持久化的历史（session 文件）保留图片，这样 resume 之后前缀缓存和模型视野都不变。

### D5. 压缩时图片跟着它的 tool result 一起走

`microcompact` 清掉一条 tool result 时，同一消息里 `origin_tool_use_id` 匹配的 `ImageBlock`
和它的 `[image from …]` 标题一起丢弃（`microcompact.py:31` 的 `_is_orphan_image_caption`），
并计入 `estimate_image_tokens()` 的节省量（`tokens.py:62`，默认值可用
`OPENHARNESS_IMAGE_TOKEN_ESTIMATE` 覆盖）。

用户自己粘贴的图片 `origin_tool_use_id` 为空，**永不被压缩清理** —— 那条规则只针对工具产出。

## Consequences

- 一张截图的价格从"约 926 tokens 的不可见 base64 噪音"变成"约 700~800 tokens 的真图片"，
  且模型真的能看见它。**想要零成本**：给 `browser_take_screenshot` 传 `filename`，
  MCP 只回路径文本，不进图片。
- MCP 配置从 `--image-responses omit` 回退到 `allow`。这个开关是 per-server argv，
  所以**改动要新开会话才生效**（MCP 子进程持有旧 argv）。
- 会话文件会变大（每张保留的截图约 65 KB base64）。代价换来的是 resume 后缓存与视野一致；
  过期的截图由 D5 清掉。
- 缓存语义不变：图片只出现在 append-only 的 message 里，system/tools 段没有变化；
  `tests/test_engine/test_cache_payload_digest.py` 仍绿（`images` 用 `exclude=True` 才做得到 ——
  该门禁用 `message.model_dump()` 当 wire 代理，普通字段会立刻让 `tool_roundtrip_second.messages` 变红）。
- 非多模态模型的行为完全不变：兄弟块也是 user 消息里的 `ImageBlock`，
  `_preprocess_images_in_messages` 的扫描范围本来就只认 role=user。

## 验证

- 单元：`tests/test_mcp/test_image_content.py`（10 条：转块/占位文本/上限/非 image mime/legacy 文本 manager/
  `_take_tool_images`/wire 形状/非多模态回退）、`tests/test_services/test_microcompact_images.py`（3 条）、
  `tests/test_api/test_provider_vision.py`（11 条 parametrize）。
- 全量 `pytest -q`：**2518 passed / 13 skipped**（基线 2491/13）。
- `scripts/quality_gate.py`：ruff 144/144、mypy 527（≤531）、core 0，ratchet 通过。
- 真机端到端见上文 Context 的实测记录（真实 MCP 子进程 + 真实 provider）。

## 未做

- 前端/TUI 不渲染图片：transcript 里只有 `[image from …]` 和工具原文。
- 不做服务端缩放/转码：环境里没有 Pillow，图片按 MCP 给的字节原样发送，
  体积靠 D2 的上限和调用方（`filename`）控制。
- `image_to_text` 回退路径保持不变（它仍是文本模型的正解），未与新的兄弟块做进一步整合。
