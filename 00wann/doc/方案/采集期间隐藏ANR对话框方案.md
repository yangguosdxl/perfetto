# 采集期间隐藏 ANR 对话框方案

## 背景

Native heap 使用较小采样间隔时，应用主线程可能超过 Android 的 5 秒输入
分发超时阈值。系统仍应正常记录 ANR，但 ANR 对话框会反复改变窗口焦点，
干扰登录、GM RPC 和手动目标场景。

## 确认方案

`run_heap_profile.sh` 和 `run_mmap_phys_profile.sh` 使用同一组公共函数：

1. 采集前读取 `settings get global hide_error_dialogs` 并保存原值。
2. 临时执行 `settings put global hide_error_dialogs 1`，只隐藏系统错误对话框。
3. 通过 Bash `EXIT` trap 覆盖正常、失败和中断退出路径。
4. 原值为 `null` 时执行 `settings delete`；原值为其他值时精确写回。
5. 读取或设置失败时输出结构化告警，但不阻断采集；恢复失败不覆盖采集原退出码。

该设置不修改 ANR 阈值，不阻止 ANR 记录，不改变 Perfetto 采集数据。

## 执行计划

- [x] 1. 在 `common_tools.sh` 实现设置保存、临时隐藏和原值恢复。
- [x] 2. 接入 `run_heap_profile.sh` 和 `run_mmap_phys_profile.sh` 的所有退出路径。
- [x] 3. 补充两个入口的自动测试，覆盖启用和恢复顺序。
- [x] 4. 同步 Native heap、mmap 和总览文档。
- [x] 5. 运行 Bash 语法检查、Python 编译检查和两个入口测试。

## 执行结果

- `test_run_heap_profile.sh` 通过。
- `test_run_mmap_phys_profile.sh` 通过。
- Bash 语法检查和 Python 编译检查通过。
- 真机 `1C111FDF600AW5` 完成 45 秒无栈 mmap 验证，结果目录为
  `PerfData/mmap_phys/2026-08-12_14-23-09`。
- 验证结果：`mmap events=6664`、`lifecycle=2611`、`smaps snapshots=31`，
  Perfetto/ftrace/perf 丢失指标均为 0，健康状态为 `pass`。
- 采集前输出 `state=suppressed|original=null`，退出时输出
  `state=restored|value=null`，设备最终值为 `null`。
