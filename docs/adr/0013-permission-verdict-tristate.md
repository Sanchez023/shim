# ADR-0013: 权限判定的三态化 —— 可判定、不可判定、需确认

- 状态: Accepted（D1–D5 全部落地）
- 日期: 2026-11-18
- 决策者: OpenHarness maintainers
- 相关: ADR-0004, ADR-0010, ADR-0012

## Context

ADR-0004 把「工具权限、沙箱、审批、密钥访问」拆成四个维度，`permissions/command_safety.py` 的
只读判定也从 `startswith` 升级成了结构化的 flag 名解析。判定本身变准了，但**它的值域仍然只有
一个 bit**，于是两种完全不同的情况被压成同一个 `False`：

- 「我确认这条命令会写」；
- 「我没法确认这条命令会不会写」。

`permissions/checker.py:193-195` 直接 `if is_read_only: allow` 短路，`is_read_only_command()` 的
签名是 `-> bool`（`command_safety.py:265-297`）。在 DEFAULT 模式里这个压缩是良性的：非只读就
弹确认。但在 PLAN 模式里它变成死路 —— `checker.py:199-225` 对非只读工具一律返回
`allowed=False, requires_confirmation=False`，**没有任何确认入口**，而审批链路本身与模式无关
（`engine/tool_dispatch.py:221-250`，无 approval gate 时 fail-closed）。同样的命令在 DEFAULT 下
只需点一次，在 PLAN 下直接不可执行。

### 实测：plan mode 的误拦

| 命令 | 真实语义 | 判定结果 |
|---|---|---|
| `wc -l $(find src -name "*.py")` | 只读 | 拦截（命令替换） |
| `find tests -name "*.py" \| xargs wc -l` | 只读 | 拦截（`xargs` 不在白名单） |
| `find . -name "*.py" -exec wc -l {} +` | 只读 | 拦截（`-exec` 是写能力 flag） |
| `grep -n "environ\|shadow\|\.ssh\|netrc" src/openharness` | 只读 | **deny**（见下，这一个是 bug） |

前三条来自「判不出」：`_split_segments()` 遇到 `$`/反引号/重定向返回 `None`
（`command_safety.py:405-470`），`xargs` 被有意排除在 `_SAFE_COMMANDS` 之外（`:44-47` 的注释），
`find` 的 `-exec` 在 `_WRITE_CAPABLE_FLAGS` 里（`:531-536`）。这些保守选择**本身是对的**——
问题在于它们的结论被当成了「确定会写」。

第四条不是保守，是误报，有两层原因：

1. **反斜杠被无条件当路径分隔符**：`_path_token_is_sensitive()` 里
   `raw = token.replace("\\", "/")`（`command_safety.py:710`）。grep 正则
   `environ\|shadow\|\.ssh\|netrc` 因此变成 `environ/|shadow/|.ssh/|netrc`，
   再被 `SENSITIVE_PATH_PATTERNS` 的 `*/.ssh/*` 命中 —— 正则里的转义被读成了目录分隔符。
2. **grep 的模式串被当成路径**：`_READ_PATH_COMMANDS` 把 `grep` 的所有非 flag 参数都收进
   path 候选（`:476`、`:509-519`），而 grep 的第一个位置参数是**模式**，不是文件。

后果不只是"少跑一条命令"。deny 文本只说了命中的 pattern，模型无法得知是自己哪一段字符触发的，
只能换写法重试；而"被拦住 → 换更原始写法"这个反馈回路，恰好把 agent 推向更难静态判定的构造
（`python -c`、here-doc、写脚本再执行），也就是把可判定的命令变成不可判定的命令。

## Decision

**判定器输出三态，而不是布尔；每个非放行结论都必须携带机器可读的原因；能问用户就不要直接拒绝。**

### D1 判定结果改为结构化三态

```python
class Verdict(StrEnum):
    READ_ONLY = "read_only"   # 已证明不会改工作区
    MUTATING = "mutating"     # 已证明会改
    UNKNOWN = "unknown"       # 静态无法证明

@dataclass(frozen=True)
class ReadOnlyVerdict:
    verdict: Verdict
    reason_code: str      # e.g. "command_substitution" / "xargs" / "write_flag:-exec"
    trigger: str          # 触发判定的 token 或片段，用于解释
```

`is_read_only_command()` 改为返回 `ReadOnlyVerdict`；`→ bool` 的旧名保留为薄包装
(`verdict is Verdict.READ_ONLY`)，唯一的运行时调用方是 `tools/bash_tool.py:57`，迁移成本一次到位。

### D2 模式语义表

| 模式 | READ_ONLY | MUTATING | UNKNOWN |
|---|---|---|---|
| FULL_AUTO | 放行 | 放行 | 放行 |
| DEFAULT | 放行 | ask | ask |
| PLAN | 放行 | deny（带原因码） | **ask** |

**唯一的行为变化是 PLAN × UNKNOWN = ask。** 理由：plan mode 的约束是「不改工作区」，
不是「不许向用户提问」。把 UNKNOWN 当 deny 并不能提高安全性（对方可以换成更难判定的写法），
只会把「一次确认」变成「一次失败 + 一次改写」。没有 approval gate 的场景（headless / 子 Agent）
仍然 fail-closed，安全性不下降。

### D3 拒绝必须可解释

非放行结论返回 `reason_code` + `trigger`。deny 文本必须包含：触发构造、为什么判不出/判成写、
以及一条改写建议（例如「避免命令替换，改用两步」）。`reason_code` 同时进事件日志的
`permission.decision`，便于统计哪类构造最常被拦。

### D4 敏感路径判定精确化

1. **反斜杠归一化限定在 Windows 路径形态**：只有 token 匹配 `^[A-Za-z]:[\\/]` 或 UNC
   前缀 `^\\\\` 时才做 `\`→`/` 替换；其余情况保留原样。`_path_token_is_sensitive()`
   与 `_is_sensitive_path()` 都适用。
2. **区分「全部参数是路径」与「首参数是模式」**：把 `_READ_PATH_COMMANDS` 拆成
   `ALL_ARGS_ARE_PATHS = {cat, head, tail, less, ...}` 与
   `FIRST_ARG_IS_PATTERN = {grep, rg, ...}`；后者跳过第一个非 flag 参数。
3. **判不出时偏向 ask**：`_glob_reaches_sensitive_path()` 在展开失败/超限时保持「不可证明」，
   由 D2 的表决定 ask 还是 deny，而不是直接产生 sensitive 结论。

### D5 回归用例表

把这次实测的四条命令与既有绕过用例合并成参数化表，断言 `(verdict, reason_code)`，
而不是只断言布尔值：

| 命令 | 期望 |
|---|---|
| `grep -n "environ\|shadow" .` | READ_ONLY |
| `wc -l $(find . -name "*.py")` | UNKNOWN / `command_substitution` |
| `find . \| xargs wc -l` | UNKNOWN / `xargs` |
| `find . -exec rm {} +` | MUTATING / `write_flag:-exec` |
| `cat .[s]sh/id_rsa` | MUTATING / `sensitive_path` |
| `cat $HOME/.ssh/id_rsa` | UNKNOWN / `variable_substitution` |

## Consequences

### Positive

- plan mode 不再有「静默死路」：要么放行，要么问一次，要么给出可执行的拒绝理由。
- 误报的代价从「turn 卡住」降到「一次点击」。
- 每类拦截可统计（`reason_code`），未来可以按数据调整白名单，而不是靠直觉。
- 把「反斜杠 = 分隔符」这个错假设固定成回归用例。

### Negative / Trade-offs

- PLAN 模式下的确认框会变多（`xargs`、`$()`、here-doc 都会触发）。缓解：同一 turn 内合并同类
  确认；会话级放行必须带作用域（命令前缀 + 目录），不能是"本次会话全放行"。
- 三态迁移触及 `is_read_only_command` 的返回类型，虽然运行时调用方只有一处，但测试里断言
  `is True/False` 的地方较多，需要批量改（保留 bool 包装可让大部分用例不动）。
- `UNKNOWN → ask` 依赖 UI 有审批通道；纯 headless 场景行为不变（仍 deny），因此两种入口的
  体验会出现差异，需要在文档里说明。

## Migration

1. **P1**：`ReadOnlyVerdict` + `Verdict` 引入，`is_read_only_command` 返回新类型，旧 bool 名保留；
   现有测试不改先跑通（包装层保证）。
2. **P2**：D4 的两处精确化（反斜杠、grep 模式串）+ 回归表。这一步修掉实测误报。
3. **P3**：D2 的模式语义表落地（`checker.evaluate` 的 PLAN 分支改 ask），事件日志加 `reason_code`。
4. **P4**（可选）：把自研 tokenizer 换成 `bashlex`/tree-sitter-bash，把更多 UNKNOWN 转成
   READ_ONLY/MUTATING。作为独立评估，不在本 ADR 承诺范围内。

回滚点：D2 是单点改动（一行 `requires_confirmation`），D4 的两处精确化互相独立。

## Alternatives

- **继续靠提示词教育模型别写 `$()`/`xargs`**：软约束、不可验证，且模型的探索能力被削弱。
  实际观测到的行为是"换一种写法重试"，而不是"放弃这条命令"。
- **把 UNKNOWN 一律视为 MUTATING（现状）**：就是本 ADR 要修的问题。
- **把 UNKNOWN 一律视为 READ_ONLY**：安全性直接归零，否决。
- **一次性引入完整 shell 解析器**：能显著减少 UNKNOWN，但增加依赖、且解析器与真实 shell
  语义仍有差距（别名、函数、`eval`）。作为 P4 备选。

## References

- `permissions/checker.py:19-47`（`SENSITIVE_PATH_PATTERNS`）、`:193-195`、`:199-225`
- `permissions/command_safety.py:44-47`、`:265-297`、`:405-470`、`:476-519`、`:531-536`、`:706-735`
- `tools/bash_tool.py:57`、`engine/tool_dispatch.py:175`、`:221-250`
- `tests/test_permissions/test_bypass_regressions.py`、`tests/test_permissions/test_command_safety.py`
- ADR-0004（安全四维）、ADR-0010（端口与依赖方向）
