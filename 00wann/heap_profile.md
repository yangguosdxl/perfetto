# Native heap profile 采集脚本

`run_heap_profile.sh` 用于启动 Perfetto Native heap profile 采集，默认目标包名为 `com.tencent.dhwdxkty.trunk.profiler`，采集结果保存到 `00wann/PerfData/mem/<日期时间>/`。`run_heap_profile.sh` 只是兼容入口，实际流程由 `run_heap_profile.py` 执行；入口会自动切换到自身所在目录，因此可以从仓库根目录执行 `00wann/run_heap_profile.sh`，也可以在 `00wann` 目录内执行 `./run_heap_profile.sh`。

默认执行时不限制采集时长，`heap_profile.py` 会持续采集直到人工中断：

```bash
00wann/run_heap_profile.sh
```

人工按 Ctrl+C 时，Python 主脚本会把 SIGINT 转发给 `heap_profile.py`，让 Perfetto 进入 `Waiting for profiler shutdown...` 收尾流程。主脚本不会直接 130 退出；它会继续等待 `heap_profile.py` 把 `raw-trace`、`symbolized-trace` 和 `heap_dump.*.pb.gz` 拉回本地并完成处理，然后保存 `heap_profile.log`、抓取 `dumpsys meminfo`，并执行后续 malloc live 与 `Native Heap Alloc` 验证。

如需自动退出，可把采集时长作为第一个参数传入，单位为毫秒：

```bash
00wann/run_heap_profile.sh 45000
```

如需指定采样 interval，可把 interval 作为第二个参数传入，单位为 bytes。不传时脚本默认使用 1024。真机验证中 4096 曾出现 malloc live 与 `meminfo Native Heap Alloc` 相差百 MB 级的问题；1024 在当前设备上通过 64MiB 绝对阈值验证：

```bash
00wann/run_heap_profile.sh 45000 1024
```

如需指定 heapprofd 共享缓冲区大小，可把 `shmem-size` 作为第三个参数传入，单位为 bytes。该值必须是 4096 的 2 的幂倍数且至少 8192：

```bash
00wann/run_heap_profile.sh 45000 1024 67108864
```

## 启动目标应用

为了让 heapprofd 的 malloc live 总量和采集后 `dumpsys meminfo` 的 `Native Heap / Heap Alloc` 具备同口径可比性，脚本会在采集前重启目标应用：

```bash
adb shell am force-stop com.tencent.dhwdxkty.trunk.profiler
```

随后脚本以 `--no-running` 启动 `heap_profile.py`，等待日志出现 `Profiling active`，再执行一次：

```bash
adb shell monkey -p <package> 1
```

该命令只用于发送启动 Intent 拉起目标应用，不用于随机触发测试场景。需要测试数据或目标场景交互时，仍应在手机上手动操作。

这个顺序保证目标进程启动后的 native malloc/free 会被 heapprofd 观察到；如果附加到已经运行很久的进程，heapprofd 无法还原采集开始前已经发生的 native 分配，`malloc_live_bytes` 会明显低于 `meminfo Native Heap Alloc`。

Python 主脚本会导出 `PYTHONPATH="$PerfettoRoot/python"`，确保直接执行 `python/tools/heap_profile.py` 时可以导入仓库内的 `perfetto` Python 包。

Python 主脚本同时导出 `PYTHONUNBUFFERED=1`，避免 `heap_profile.py` 的 `Profiling active` 输出因为管道缓冲而延迟。profiler 原始日志会在采集结束后保存到：

```text
PerfData/mem/<日期时间>/heap_profile.log
```

Python 主脚本还会显式传入本地构建产物：

```bash
--traceconv-binary "$PerfettoRoot/out/linux_clang_release/traceconv"
--trace-processor-binary "$PerfettoRoot/out/linux_clang_release/trace_processor_shell"
```

这样可以避免 `heap_profile.py` 下载或复用 `~/.local/share/perfetto/prebuilts` 中与当前系统 glibc 不兼容的预构建二进制。

AI 做真机验证时必须传入 `45000`，表示采集 45 秒后自动停止并拉取 `raw-trace`、生成 `symbolized-trace` 和 `heap_dump.*.pb.gz`。下探采样 interval 时使用 `00wann/run_heap_profile.sh 45000 <interval_bytes>`；对比缓冲区时使用 `00wann/run_heap_profile.sh 45000 <interval_bytes> <shmem_size>`。

## 验证

每次 `heap_profile.py` 输出 `Waiting for profiler shutdown...` 后，脚本会在 host 侧 trace 转换、符号化和 pprof 生成完成前立刻执行：

```bash
adb shell dumpsys meminfo com.tencent.dhwdxkty.trunk.profiler
```

原始输出保存为：

```text
PerfData/mem/<日期时间>/dumpsys_meminfo.txt
```

随后脚本使用 `trace_processor_shell` 查询采集 trace：

```sql
select coalesce(sum(size), 0) as malloc_live_bytes from heap_profile_allocation;
```

这个值是 `heap_profile_allocation` 全采集窗口的累计净 malloc live bytes。它是本验证的主判断口径，不能替换成 `max(ts)` 最新 dump 分片。

脚本会解析 `dumpsys meminfo` 主表 `Native Heap` 行的 `Heap Alloc` 列，并换算为 bytes。验证结果保存到：

```text
PerfData/mem/<日期时间>/heap_meminfo_validation.txt
```

默认判定规则：

```text
abs(malloc_live_bytes - meminfo_native_heap_alloc_bytes)
  <= 64 MiB
```

可通过环境变量调整：

```bash
HEAP_PROFILE_MEMINFO_ALLOWED_DIFF_BYTES=67108864
```

验证通过时输出 `HEAP_MEMINFO_VALIDATION=PASS`。如果不相当，脚本输出 `HEAP_MEMINFO_VALIDATION=FAIL` 并返回失败。百 MB 级差异不能通过百分比阈值放行，必须继续定位是否存在 heapprofd 丢包、trace 缺失、采样间隔过粗、启动前分配未覆盖、meminfo 抓取晚于采集窗口，或 `Native Heap Alloc` 中存在 heapprofd 未统计来源等根因。报告中会保留 `health_sum`、`heap_dump_count`、trace 路径和 meminfo 路径。

修改 `run_heap_profile.sh` 后可运行：

```bash
bash 00wann/test_run_heap_profile.sh
bash -n 00wann/run_heap_profile.sh 00wann/test_run_heap_profile.sh
/usr/bin/python3 -m py_compile 00wann/run_heap_profile.py
```

测试会用假的 `adb` 模拟“第一次 `pidof` 为空、第二次返回 PID”的状态，验证脚本在应用未启动时会先拉起应用，并且随后继续执行 Native heap profile 采集。测试还会模拟人工 Ctrl+C，确认中断会传递给 `heap_profile.py`，并在 `heap_profile.py` 完成 trace/heap dump 本地收尾后继续完成 meminfo 抓取和 SQL 验证。

## 启动耗时评估

`run_heap_startup_eval.sh` 用于评估 Native heap profile 参数对应用启动流程的影响。脚本会先跑一轮无 heapprofd 基线，再对每个 interval 执行：

```text
force-stop 应用
启动 heapprofd，并等待 Profiling active
am start 拉起 MainActivity
等待 logcat 出现 “LAN 更新流程开始”
等待 45 秒采集结束
检查 heap_dump、heap_profile_allocation 行数和样本丢失统计
```

默认参数：

```bash
00wann/run_heap_startup_eval.sh
```

等价于：

```text
duration_ms = 45000
shmem_size = 268435456
intervals = 512 256 128 64 32 16
```

也可以手动指定：

```bash
00wann/run_heap_startup_eval.sh 45000 268435456 1024 2048 4096
```

输出中的关键字段：

```text
LAN_STARTUP_MS  -> 从 am start 前设备时间到 “LAN 更新流程开始” 日志出现的耗时
HEAP_DUMP_COUNT -> heap_dump.*.pb.gz 文件数量
ALLOC           -> heap_profile_allocation 的 alloc_rows、positive_rows、net_size
HEALTH_SUM      -> heapprofd/perfetto 丢失统计求和；0 表示没有发现样本丢失
```

干跑检查参数：

```bash
HEAP_STARTUP_DRY_RUN=1 00wann/run_heap_startup_eval.sh 45000 268435456 512 256
```

脚本默认目标日志为 `LAN 更新流程开始`。如需调整，可通过环境变量覆盖：

```bash
HEAP_STARTUP_PATTERN="LAN 更新流程开始" \
HEAP_STARTUP_APP="com.tencent.dhwdxkty.trunk.profiler" \
HEAP_STARTUP_ACTIVITY="com.tencent.dhwdxkty.trunk.profiler/com.dhplugin.unity.MainActivity" \
00wann/run_heap_startup_eval.sh
```
