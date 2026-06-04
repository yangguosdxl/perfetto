# Native heap profile 采集脚本

`run_heap_profile.sh` 用于启动 Perfetto Native heap profile 采集，默认目标包名为 `com.tencent.dhwdxkty.trunk.profiler`，采集结果保存到 `00wann/PerfData/mem/<日期时间>/`。脚本会自动切换到自身所在目录，因此可以从仓库根目录执行 `00wann/run_heap_profile.sh`，也可以在 `00wann` 目录内执行 `./run_heap_profile.sh`。

默认执行时不限制采集时长，`heap_profile.py` 会持续采集直到人工中断：

```bash
00wann/run_heap_profile.sh
```

如需自动退出，可把采集时长作为第一个参数传入，单位为毫秒：

```bash
00wann/run_heap_profile.sh 45000
```

如需指定采样 interval，可把 interval 作为第二个参数传入，单位为 bytes。不传时沿用 `heap_profile.py` 默认值 4096：

```bash
00wann/run_heap_profile.sh 45000 1024
```

如需指定 heapprofd 共享缓冲区大小，可把 `shmem-size` 作为第三个参数传入，单位为 bytes。该值必须是 4096 的 2 的幂倍数且至少 8192：

```bash
00wann/run_heap_profile.sh 45000 1024 67108864
```

## 启动目标应用

脚本会先通过 `adb shell pidof <package>` 检查目标进程是否已经运行。

如果目标进程不存在，脚本会输出关键步骤日志，并执行一次：

```bash
adb shell monkey -p <package> 1
```

该命令只用于发送启动 Intent 拉起目标应用，不用于随机触发测试场景。应用启动后，脚本继续执行 `heap_profile.py` 采集。

脚本会导出 `PYTHONPATH="$PerfettoRoot/python"`，确保直接执行 `python/tools/heap_profile.py` 时可以导入仓库内的 `perfetto` Python 包。

脚本还会显式传入本地构建产物：

```bash
--traceconv-binary "$PerfettoRoot/out/linux_clang_release/traceconv"
--trace-processor-binary "$PerfettoRoot/out/linux_clang_release/trace_processor_shell"
```

这样可以避免 `heap_profile.py` 下载或复用 `~/.local/share/perfetto/prebuilts` 中与当前系统 glibc 不兼容的预构建二进制。

AI 做真机验证时必须传入 `45000`，表示采集 45 秒后自动停止并拉取 `raw-trace`、生成 `symbolized-trace` 和 `heap_dump.*.pb.gz`。下探采样 interval 时使用 `00wann/run_heap_profile.sh 45000 <interval_bytes>`；对比缓冲区时使用 `00wann/run_heap_profile.sh 45000 <interval_bytes> <shmem_size>`。

## 验证

修改 `run_heap_profile.sh` 后可运行：

```bash
bash 00wann/test_run_heap_profile.sh
bash -n 00wann/run_heap_profile.sh 00wann/test_run_heap_profile.sh
```

测试会用假的 `adb` 模拟“第一次 `pidof` 为空、第二次返回 PID”的状态，验证脚本在应用未启动时会先拉起应用，并且随后继续执行 Native heap profile 采集。

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
