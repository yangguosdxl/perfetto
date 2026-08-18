# Native heap 内存差值改为健康提示方案

## 背景

`run_heap_profile.py` 会在 Native heap 采集收尾后对比：

```text
malloc_live_bytes
  与
dumpsys meminfo 的 Native Heap Alloc
```

当前默认允许差值为 64 MiB。只要实际差值超过阈值，脚本就输出
`HEAP_MEMINFO_VALIDATION=FAIL` 并返回失败，进而让 `run_heap_profile.sh` 跳过最新调用栈分析。

真机完整业务采集已确认两侧数据均可正常取得，Perfetto `health_sum=0`，但两种统计口径仍可能
长期存在百 MB 级差异。因此，差值本身适合作为健康提示和诊断依据，不应作为 Native heap
采集与自动分析的必须门禁。

## 目标语义

将验证结果拆分为三种状态：

```text
采集后验证
  ├─ 数据缺失、抓取失败、解析失败或 SQL 查询失败：FAIL，返回失败
  ├─ 数据完整且差值不超过阈值：PASS，返回成功
  └─ 数据完整但差值超过阈值：WARN，返回成功
                                      └─ 继续执行最新调用栈分析
```

其中：

- `PASS` 表示差值处于配置阈值内，现有行为保持不变；
- `WARN` 表示两侧数据完整、仅差值超过阈值，保留全部对比信息但不阻断流程；
- `FAIL` 仅用于无法完成健康对比的情况，继续作为硬失败门禁。

## 代码修改

修改 `run_heap_profile.py` 的 `validate_heap_profile_against_meminfo()`：

1. 保留以下硬失败及非零返回码：
   - `dumpsys_meminfo_failed`；
   - `trace_missing`；
   - `parse_native_heap_alloc_failed`；
   - `query_malloc_live_failed`。
2. 差值不超过 `HEAP_PROFILE_MEMINFO_ALLOWED_DIFF_BYTES` 时，继续输出
   `HEAP_MEMINFO_VALIDATION=PASS` 并返回 `0`。
3. 差值超过阈值时，将输出从 `HEAP_MEMINFO_VALIDATION=FAIL` 改为
   `HEAP_MEMINFO_VALIDATION=WARN`，原因仍使用
   `malloc_live_not_comparable_to_meminfo_alloc`，并返回 `0`。
4. `WARN` 行继续记录 `malloc_live_bytes`、`meminfo_native_heap_alloc_bytes`、`diff_bytes`、
   `allowed_diff_bytes`、`health_sum` 和 `heap_dump_count`，确保问题可追踪。

不修改允许差值的默认值，也不通过扩大阈值隐藏差异。

## 测试调整

更新 `test_run_heap_profile.sh` 中的大差值用例：

- 模拟约 227 MiB 差值；
- 断言输出 `HEAP_MEMINFO_VALIDATION=WARN`；
- 断言 wrapper 返回成功；
- 断言采集成功后继续调用假的 `run_heap_alloc_stacks_by_symbol_latest.sh`；
- 断言最终输出 `HEAP_PROFILE_POST_ANALYSIS=PASS`。

保留现有采集失败、动作失败和自动分析失败用例，确保真正的失败仍能向上传播退出码。

## 文档同步

同步修改：

- `README.md`：将差值校验说明改为健康对比，明确 `WARN` 不作为验收门禁；
- `docs/heap_profile.md`：说明三态结果、硬失败边界以及 `WARN` 后继续自动分析；
- 本方案：用户确认后追加执行计划，执行过程中逐项标记完成。

## 验证方式

自动验证：

```bash
bash -n run_heap_profile.sh test_run_heap_profile.sh
python -m py_compile run_heap_profile.py
bash test_run_heap_profile.sh
bash test_run_heap_alloc_stacks_by_symbol_latest.sh
```

本次只调整采集后健康提示的退出语义，不修改 Perfetto 配置、采集流程或调用栈处理逻辑。自动测试
应覆盖 `WARN` 后继续分析的完整控制流；如需再次进行真机主功能验收，仍严格使用设备
`1C111FDF600AW5`，不传 duration，等待登录、表加载、GM RPC 成功及稳定采集 120 秒后自然收尾。

## 待确认

请确认以下行为：

1. 仅“数据完整但差值超过阈值”降级为 `WARN`，并返回成功；
2. trace、meminfo 或 SQL 数据不可用仍输出 `FAIL` 并返回失败；
3. `WARN` 不阻断 `run_heap_profile.sh` 的最新调用栈自动分析。

以上行为已由用户确认。

## 执行计划

- [x] 1. 确认修改边界：仅完整数据的大差值降级为健康提示。
- [x] 2. 修改 `run_heap_profile.py`，输出 `WARN` 并返回成功。
- [x] 3. 更新 `test_run_heap_profile.sh`，验证 `WARN` 后继续自动分析。
- [x] 4. 同步更新 `README.md` 和 `docs/heap_profile.md`。
- [x] 5. 运行 Python、Bash 语法检查和相关 Shell 集成测试。

## 验证结果

- `python -m py_compile run_heap_profile.py`：通过；
- `bash -n run_heap_profile.sh test_run_heap_profile.sh`：通过；
- `bash test_run_heap_profile.sh`：通过，覆盖大差值输出 `WARN`、返回成功并继续自动分析；
- `bash test_run_heap_alloc_stacks_by_symbol_latest.sh`：通过。

本次未修改 Perfetto 配置、真机采集时序或调用栈处理逻辑，因此未重复触发耗时的真机采集。
