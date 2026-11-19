# ADR-0014: 完成判定进入运行时（验证账本 + 一次纠正）

- 状态: Implemented
- 日期: 2026-11-18
- 决策者: Shim（实现）、用户（选定软门槛语义）

## Context

引擎能记录"看过什么"，却不能回答"改完之后验过没有"。具体事实：

1. `engine/step_runner.py` 在 `final_message.tool_uses` 为空时直接 `status="completed"`，
   没有任何"这批改动是否被验证过"的检查。改了五个文件、一次测试没跑，回合照样正常结束。
2. `TurnState.remember_verified_work()` 把 `Inspected file X`、`Ran bash ...`
   也记为 verified；`services/session_memory` 再把这个列表渲染成 `## Verified Work`。
   于是"探索"和"验证"在交班文档里被合并成同一个断言，而后者才是读者想知道的。
3. STOP hook 的返回值被 `await` 后丢弃（`step_runner.py` 中 `await execute(STOP, ...)` 的
   结果无人读），所以 `block_on_failure=True` 的 hook 实际上拦不住任何东西。
4. 提示词里只有"完成前回读清单"这类散文约束，没有执行点。

结论：问题不是模型不够自觉，而是"完成"这个事实没有被建模。任何只靠提示词的方案都不可观测、
不可测试，也无法在压缩后存活。

## Decision

把完成判定做成运行时状态，分三层，全部由确定性代码承担：

**D1 — 账本（`engine/verification_ledger.py`）。** 在 `tool_metadata` 里维护结构化状态：
编辑序号 `edit_seq`、不可见写操作序号 `opaque_seq`、验证运行记录（kind / 命令 / 返回码 /
scope / 当时的两个序号）。只把"能在改动后再现"的运行算作闭环：`returncode == 0`
且 `scope == full` 且两个序号都与当前一致。部分运行（`-k`、单文件、`::`、`--lf`）记为证据但
不闭环。无法识别的命令不猜——误判"已验证"比漏判更贵。

**D2 — 两条出口，都用已被验证的通道。**
`tools/todo_write_tool.py::_open_items_suffix` 已经证明"模型唯一可靠读回的是工具结果"，
因此验证状态追加在编辑类工具和验证类命令的结果尾部；回合结束时由 `step_runner` 判一次，
未闭环则注入一条 `synthetic` user 消息并把 `needs_followup` 置真，让循环再走一步。

**D3 — 一次纠正，不做硬门槛。** 每次"未验证连续段"只提醒一次（`nudge_limit`，默认 1），
任何全量绿灯把计数清零。STOP hook 的 blocked 与验证未闭环共用这份预算——要防的是死循环，
不是提醒太少。豁免就是"被提醒后仍然选择结束"，不需要新工具。

**D4 — 交班文档只说知道的事。** `Verified Work` 更名为 `Explored`（它本来就是探索记录），
新增 `## Verification`（最后一次全量绿灯 + 是否已失效）与 `## Open Items`（会话清单里
仍未勾选的项）。压缩可以丢细节，不能丢这三件事。

**D5 — 收尾检查（`engine/session_hygiene.py`）。** 首次工具调用时快照 `git worktree list`
与 `git stash list` 计数，回合结束时对比，新增的条目并入同一条提醒。只查这两类：它们是
"故意制造、之后忘记"的全局状态。未跟踪文件**不计**——写新测试文件本来就会产生未跟踪路径，
计入只会让人关掉开关。

## Consequences

### Positive

- "改完没验"从不可见变为可观测：工具结果、状态事件、会话记忆、交班文档四处一致。
- 可在测试里断言：`tests/test_engine/test_completion_gate.py` 用假客户端验证
  "恰好一次额外模型调用"与"提醒后放行"。
- 交班文档不再把探索包装成验证，压缩后的下一个会话不会继承这个错误结论。
- STOP hook 的 `block_on_failure` 第一次真的有效。

### Negative / Trade-offs

- 门槛不闭环时每个未验证回合多一次模型调用（受 `nudge_limit` 上限约束）。
- `scope` 判定基于命令文本启发式：`make validate` 这类自定义入口一律 `other`，不闭环。
  取舍是"漏判"而非"误判"，因为误判会导致假绿灯。
- `git add`/`git commit`/包安装被排除在 `opaque_seq` 之外：它们不能把绿灯变红，
  计入会让每次提交都触发提醒。
- 本 ADR 之前 `AssistantTurnComplete` 在提醒之前就已发出。不改这个顺序：该事件在本仓库
  早已是"本条消息完成"而非"整个回合完成"（多工具步也会先发它再跑工具），
  单独为门槛改序会与既有消费者（autopilot / channels）语义冲突。

## Migration

无数据迁移：账本键 `verification_ledger` 缺失时视为空，`verification_gate` 配置每次启动
由 `Settings.verification` 注入，不落盘。`nudge_limit=0` 即完全关闭提醒，
`verification.enabled=false` 连记账一起关闭。

## Alternatives

- **只改提示词。** 否决：不可观测、不可测试，压缩后失效。
- **硬门槛（不闭环禁止结束）。** 否决：无法表达"这里就是不需要跑测试"，会产生自锁；
  且与"一次纠正"相比，收益增量小于打扰成本。
- **用 LLM 判断"这次改动要不要验证"。** 否决：判定本身不可复现，且失败方向正是
  "看起来验证过了"。
- **把 git 工作区状态（含未跟踪文件）纳入。** 否决：噪声高于信号，见 D5。

## References

- `src/openharness/engine/verification_ledger.py`
- `src/openharness/engine/session_hygiene.py`
- `src/openharness/engine/step_runner.py`（完成分支与 STOP hook）
- `src/openharness/engine/tool_dispatch.py`（记账与工具结果注解）
- `src/openharness/services/session_memory/__init__.py`（交班块）
- ADR-0012（中断语义）：同样是"结束状态必须诚实"的一类问题。
