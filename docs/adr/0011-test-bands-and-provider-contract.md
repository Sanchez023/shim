# ADR-0011: 三层测试带、真 provider 契约与不变量守护

- 状态: Proposed
- 日期: 2026-11-18
- 决策者: OpenHarness maintainers
- 相关: ADR-0009, ADR-0010

## Context

测试体量不小（165 个文件、1438 个 `def test_`、2364 个 node id，test/impl ≈ 0.57），但形状有三个问题：

1. **pytest 里 0 个端到端**。真起进程的验证全在 `scripts/`（`scripts/react_tui_e2e.py:22` 用 pexpect，`scripts/e2e_smoke.py:754,769` 打真 endpoint），不进 CI、不能被 `pytest -k` 选中、无记录。
2. **无法分层选择**。`pyproject.toml:103-105` 只有 `testpaths` 与 `asyncio_mode`；全仓无 `mark.slow/integration/e2e`；CI 是裸 `uv run pytest -q`（`.github/workflows/ci.yml:38,64`）；无 timeout 插件。
3. **覆盖分布与风险倒挂**。test/impl：`autopilot` 0.09、`channels` 0.11、CLI 根层 0.14、`services` 0.39；而 `ohmo` 0.92，其中 `tests/test_ohmo/test_gateway.py` 单文件 3412 行、161 处 monkeypatch，测的是 gateway 私有函数。零覆盖约 638 行（`keybindings`/`themes`/`personality`/`state`/`output_styles`/`voice`/`vim`）。

另外：`tests/test_no_committed_secrets.py:54` 参数化 669 个用例（占全部 node id 的 28%），collection 阶段就跑 `git ls-files`（`:44`）；12 个文件直接 `import _私有名`，属于"改实现就得改测试"的硬耦合；`tests/conftest.py` 只有 13 行，没有共享 provider fake、没有 tmp HOME 隔离、没有 socket 禁用；约 40 处真实 `asyncio.sleep` 断言时序（如 `tests/test_engine/test_cache_warmup.py:140-146`）。

结论：**测试量掩盖了层次缺失**。没有真 provider 那一带，harness 的核心价值（真模型循环）实际上没有被验证。

## Decision

### D1. 三层测试带 + marker

`pyproject.toml` 加 `--strict-markers` 与以下 marker：

| 带 | 内容 | 目标时长 | 触发点 |
|---|---|---|---|
| `unit` | 单模块 + 协议级 fake | < 45s | pre-commit / `make test-fast` |
| `integration` | 真文件系统 + `FakeApiClient` + 真 `QueryEngine` 循环 | < 3min | PR CI |
| `provider` | 真 endpoint（`tests/smoke/`） | ~2min | nightly / 手动 |

配套：`make test-fast` / `make test-all` / `make test-provider`；`pytest-timeout` 全局上限；未打 marker 的测试默认视为 `unit` 并在 collection 时校验（用 `conftest.py` 的 `pytest_collection_modifyitems`）。

### D2. 真 provider 契约带（`tests/smoke/`）

把 `scripts/e2e_smoke.py` 与 `benchmarks/suites/real.py` 的能力收成 3 个 case：

1. **截断**：`max_tokens` 触顶时必须发 `StatusEvent` 警告，不得当作正常完成（对应 `api/finish_reason.py`）。
2. **tool-call 流式**：多 tool call 的分片拼装，含流中止后的可恢复性。
3. **可重试性分类**：连接超时 / 5xx 可重试，4xx 不可重试，且**已经吐出过事件的流不得重放**（对应 `api/transport.py`）。

约束：

- key 只从环境变量读取，**缺失即 skip 而不是 fail**，绝不允许默认值（`git show HEAD:tests/test_real_large_tasks.py:20` 的硬编码 key 是反面教材）。
- 永不进默认 CI；结果写入 `benchmarks/` 下的记录文件，供趋势对比。
- 断言只看协议级事实（finish_reason、事件序列、重试次数），不看模型措辞。

### D3. 不变量守护测试

| 不变量 | 守护手段 |
|---|---|
| I2 依赖单向 | `import-linter` 契约作为测试跑；违规项按 ADR-0010 逐条登记带到期日的 ignore，到期即红 |
| I3 状态单一写者 | 静态检查：指定状态面的赋值点数量上限（如 permission mode 从 7 → 1，含 plan-mode 工具与 `QueryEngine.set_permission_mode`）；`tests/test_config/test_permission_mode_writer.py` 已断言源码中无 `permission.mode =` 直接赋值 |
| I4 事件可重放 | 用同一份 `events.jsonl` 重放，断言投影结果与 live 流等价；`tests/test_engine/test_message_sot.py` 进一步比较 `_message_wire_payload` 的 hash 序列（含 coordinator context 与 continue_pending）。live 写路径每轮结束后也从同一投影重建缓存，重放 fold 应用与 resume 相同的 `sanitize_conversation_messages`。另加"双发射等价"测试（`step_runner.py:137-144`） |
| I5 宣称即可验收 | 解析 README 能力表，每项必须在 ADR-0009 D3 中存在且对应的 canonical 有测试 |
| I6 配置项有读者 | 反射扫描 `Settings` 字段：0 读者或 0 测试 → 必须在 `deprecated_fields.py` 登记 |
| I7 缓存字节稳定 | `tests/test_engine/test_cache_stability.py` 四条断言（`stable_sha` / `tools_sha` 不变、`activate_tools` append-only、记忆写入不产生 stable/tool drift 且不改写历史、`continue_pending` 与前缀兼容）；每个阶段 PR 附一行 cache delta（最近一次 `CacheFingerprint.stable_sha` + `summary()["hot_cache_hit_rate"]`） |

### D4. 按包覆盖率棘轮

不使用全仓单一百分比（0.57 同时掩盖了 0.09 与 0.92）。在 `scripts/quality_gate.py` 旁增加 per-package floor，从当前值起步、只升不降：`channels` 0.11、`autopilot` 0.09、`services` 0.39、CLI 根层 0.14，以及逐包 `engine`/`api`/`tools` 的现状值。

### D5. 测试基建

- `tests/conftest.py` 提供：共享 `FakeApiClient`（协议级，支持脚本化事件序列与失败注入，**记录每次请求的完整 wire bytes**：system、tools、messages，供 I7 复用）、tmp HOME 隔离、`autouse` 禁 socket（可 opt-out）、可注入 clock。
- `test_no_committed_secrets.py` 从 669 个 node 收成一个用例内循环，保留检测能力。
- 12 处私有名导入改为通过公共 API 断言，消除"改实现必须改测试"的硬耦合。
- ~40 处时序断言改为注入 clock。

## Consequences

### Positive

- harness 的核心价值（真模型循环）第一次被自动验证，且可重复。
- 快慢分离后，本地反馈时间从全量降到 < 45s。
- 不变量有了机器守护，"多条路径"不会在半年后重新长出来。

### Negative / Trade-offs

- `provider` 带有网络依赖与成本，只能手动/nightly，天然比 unit 弱（无法阻止 PR 级回归）。
- per-package floor 会让 `channels`/`autopilot` 的每个 PR 都必须带测试，短期拖慢这两块。
- 注入 clock 需要改约 40 处测试，属于纯机械但面积不小的改动。
- I7 的 cache delta 是人工判断项（一次性冷启动 vs 持续回归），闸门只能钉住字节，不能自动判定语义是否可接受。

## Migration

见 `docs/refactor-plan.md`：P1（marker + conftest + secret 扫描）、P4（不变量守护测试）、P5（provider 带 + per-package 闸门）。

## Alternatives

- **只加覆盖率百分比闸门**：被排除——0.57 的平均值会掩盖最危险的 `autopilot`/`channels`。
- **把真 provider 测试放进默认 CI**：被排除——需要密钥、有成本、且不稳定，会训练出"重跑就好"的习惯。
- **保留 `scripts/` 里的 e2e 脚本不动**：被排除——它们已经存在但没人跑，没有断言分层也没有记录。

## References

- `scripts/e2e_smoke.py:754,769`、`scripts/react_tui_e2e.py:22`
- `benchmarks/suites/real.py`、`benchmarks/suites/wire.py`
- `tests/test_no_committed_secrets.py:44,54`
- `tests/test_engine/test_cache_warmup.py:140-146`
- `tests/test_engine/test_cache_stability.py`（I7 闸门）
- `src/openharness/engine/cache_diagnostics.py:131,327`（`_message_wire_payload`、`summary`）
- `.github/workflows/ci.yml:38,64`、`pyproject.toml:103-105`
