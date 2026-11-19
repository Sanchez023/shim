# ADR-0007: 组合式系统提示词与项目规则管理

- 状态: Implemented
- 日期: 2026-11-15
- 决策者: OpenHarness maintainers
- 相关: ADR-0001, ADR-0002, ADR-0006

## Context

运行时、TUI、子 Agent 和工具能力扩展后，原来的系统提示词仍是“硬编码基础提示 + 运行时字符串拼接”：

- 基础提示一体化，无法按 provider/model 选择不同行为；
- 委托与子 Agent 指导是静态的，即使 `agent` 工具不可用也会出现；
- 项目规则只支持 Claude 的 `CLAUDE.md`，不识别 Codex/DSH 的 `AGENTS.md` 或 Cursor 的规则文件；
- 没有 section 排序、预算、截断和诊断，长项目规则/记忆可能挤占上下文；
- 子 Agent 提示词仍写着 “Claude Code”，与 OpenHarness 身份不符；
- 无法在 `oh --dry-run` 中看到提示词由哪些 section 组成。

## Decision

1. 新增 `PromptComposer` 与 `PromptSection`：
   - 每个 section 有 `name`、`order`、`content`、`cacheable`、`required`、`max_chars`。
   - composer 负责稳定排序、section 内截断、总预算（默认 60k chars）和诊断输出。
2. `build_runtime_system_prompt_result()` 返回 `PromptBuildResult`，包含最终文本、section 诊断、连续的 `cacheable_prefix` 与 `version`；`build_runtime_system_prompt()` 保留为返回字符串的兼容入口。
3. 新增 provider profile module（`prompts/profiles.py`）：
   - Anthropic/Claude：保留 tool_use/tool_result 配对、extended thinking 语义。
   - OpenAI-compatible：鼓励并行工具调用与简短进度更新。
   - Codex/Responses：依赖 harness 管理 reasoning/state，优先批量工具调用。
4. 委托 section 改为 capability-aware：
   - 只有 `agent` 工具可用时才注入完整委托指导；
   - 只有 `subagent_codex` 已注册时才补充 Codex 委派说明。
5. 项目规则统一发现：
   - Claude：`CLAUDE.md`、`.claude/CLAUDE.md`、`.claude/rules/*.md`。
   - Codex/DSH：`AGENTS.md`。
   - Cursor：`.cursor/rules/*.mdc`、`.cursor/rules/*.md`、`.cursorrules`。
   - `.mdc` 的 YAML frontmatter 会被解析：`description` 进入标题，`globs`/`alwaysApply` 作为作用域元数据注入。
   - `alwaysApply: false` 且带 `globs` 的规则按**当前 turn 的 active files** 过滤；只有任一 active file 匹配 glob 时才注入。
   - active files 来源于：用户提示中的 `@mention`/路径 token、`read_file_state`、`task_focus_state.active_artifacts`、`recent_work_log`。
   - 含文件上下文的 turn 会重建系统提示词；无文件上下文时保留当前规则集合。
6. 子 Agent 共享/Explore/Plan 提示词改为 OpenHarness 品牌，保留专门解释 Claude Code 的 guide agent。
7. `oh --dry-run` 在 `validation.system_prompt_sections` 中输出现有 section 的名称、顺序、字符数、是否 cacheable、是否被截断，并输出 prompt 版本与 cacheable 字符数。
8. Anthropic prompt caching：当连续 `cacheable_prefix` 不少于 2000 字符时，Anthropic client 把 system 发送为 text block 列表，并在 prefix 上设置 `cache_control: {"type": "ephemeral"}`；其余 provider 继续使用完整字符串。

## Consequences

### Positive

- 提示词可组合、可预算、可诊断，新增能力时只需增加 section。
- Provider 行为差异有显式位置，不再依赖单一模板。
- Claude、Codex/DSH、Cursor 规则文件可以共存。
- 不可用的工具不会再污染提示词。
- 为后续 prompt caching、A/B 和版本化提供结构性基础。

### Negative / Trade-offs

- Section 顺序和预算需要持续维护，错误排序可能影响模型行为。
- 预算截断可能丢弃低优先级记忆/规则；诊断只能帮助定位，不能自动判断语义重要性。
- Prompt caching 是否生效取决于 provider 的最小可缓存 token 数和网关支持；短 prefix 会回退为普通字符串。

## Implementation

- `src/openharness/prompts/composer.py`：`PromptSection`、`PromptBuildResult`、`PromptComposer`、`cacheable_prefix`、`PROMPT_VERSION`。
- `src/openharness/prompts/profiles.py`：Claude/GPT/Codex provider profile。
- `src/openharness/prompts/context.py`：section 组装、provider profile、capability-aware delegation、预算。
- `src/openharness/prompts/claudemd.py`：Claude/Codex/Cursor 规则发现、`.mdc` frontmatter 与 active-file glob 过滤。
- `src/openharness/prompts/active_files.py`：active file 收集与 Cursor glob 匹配。
- `src/openharness/engine/query_engine.py`：文件上下文 turn 的 prompt 重建钩子。
- `src/openharness/api/client.py`：`build_anthropic_system_param()` prompt caching block。
- `src/openharness/cli.py`：dry-run section 诊断。
- `src/openharness/coordinator/agent_definitions.py`：OpenHarness 品牌。
- 测试：`tests/test_prompts/test_prompt_composer.py`、`tests/test_prompts/test_claudemd.py`、`tests/test_commands/test_cli.py`。

## Alternatives

- **继续单一硬编码 prompt**：无法按 provider/能力裁剪，规则扩展会持续膨胀。
- **直接移植 Codex 的模型 prompt 文件**：需要大规模重写且会丢失 OH 的运行时指导。
- **只支持 CLAUDE.md**：无法覆盖 Codex/DSH/Cursor 用户。
- **在 prompt 中枚举所有工具**：与工具注册表重复，且容易在工具禁用时撒谎。

## References

- DSH `docs/architecture.zh.md`、`packages/core/system-prompt`
- Codex `codex-rs/core/gpt_5_codex_prompt.md` 等模型族提示词、`docs/agents_md.md`
- Cursor `.cursor/rules/*.mdc`、`.cursorrules` 约定
- Claude Code `CLAUDE.md` 层级、skills、subagent prompts
