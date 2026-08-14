# 00wann 工具总说明

本文是 `00wann` 目录的统一入口，集中说明每个工具的用途、默认行为、参数和输出。更细的实现逻辑仍保留在专项文档中：


| 专项文档                                                | 内容                                                 |
| --------------------------------------------------- | -------------------------------------------------- |
| `mmap_phys_analyzer.md`                             | mmap 真实物理内存归因、无栈 mmap 验证、分类输出和已知边界。                |
| `heap_profile.md`                                   | Native heap profile 采集、meminfo 对比和启动耗时评估。          |
| `device_test_framework.md`                          | 通用配置、平台适配、malloc/mmap 插件、流程和统一报告。             |
| `hybridclr_mmap_malloc_symbol_report_2026-06-22.md` | HybridCLR mmap 优化的符号路径根因、重新符号化和 malloc 分类验证报告。     |
| `heap_analyzer/README.md`                           | Native heap 调用栈查询、fs.ini 分类、pprof 和 speedscope 输出。 |
| `meminfo_android_demo_validation.md`                | `dumpsys meminfo` 真机 demo 的构建、指标和验证结论。             |
| `dumpsys_meminfo_metrics.md`                        | Android `dumpsys meminfo` 各行各列口径说明。                |
| `superpower_memory_perf_injection_slow_spec.md`     | 验证内存性能模块注入后程序运行过慢问题的任务规格、采集数据和验收标准。                |


## 环境和运行入口

```text
Windows 宿主机
  -> 使用 Git Bash 执行脚本，bash 路径为 D:\Program Files\Git\bin\bash.exe
  -> 示例：& 'D:\Program Files\Git\bin\bash.exe' -lc "cd /d/dr2/Misc/perfetto/00wann && ./run_meminfo_android_demo.sh"

Linux 宿主机
  -> 直接在 00wann 目录执行 bash 脚本

Android 真机
  -> 需要 adb devices 能看到 device 状态
  -> 采集目标 App 场景时不要使用 adb monkey 触发随机事件，应在手机上手动操作目标场景
```

工具链默认通过 `common_tools.sh` 自动探测：

```text
Python
  -> 依次尝试 PYTHON、python3、python、py

Perfetto 工具
  -> Windows Git Bash 优先使用 ../../perfetto/out/win_clang/*.exe
  -> Linux 优先使用 ../../perfetto/out/linux_clang_release/*

Android SDK/NDK/JDK
  -> 优先使用环境变量
  -> Windows 默认回退到 Unity 2022.3.62 自带 AndroidPlayer 工具链
```

## 工具关系图

```text
真机采集
  run_heap_profile.sh / run_mmap_phys_profile.sh
    -> run_device_test.sh
      -> AndroidAdapter + FeaturePlugin + FlowSpec
        -> run_heap_profile.py / collect_mmap_phys_data.py
      -> mmap_phys_analyzer.py

离线 mmap 分析
  run_mmap_phys_analyze_latest.sh
    -> mmap_phys_analyzer.py

Native heap profile
  run_heap_profile.sh
    -> run_heap_profile.py
      -> Perfetto python/tools/heap_profile.py

Native heap 调用栈分析
  run_heap_alloc_stacks_by_symbol_latest.sh
    -> heap_analyzer/query_heap_alloc_stacks_by_symbol.py
      -> heap_analyzer/classification.py

验证 demo
  run_meminfo_android_demo.sh
    -> meminfo_android_demo/build_demo_apk.sh
    -> meminfo_android_demo/verify_meminfo_demo.py

  run_heapprofd_malloc_apk_demo.sh
    -> heapprofd_malloc_apk_demo/*
```

## 验收规则

修改采集、分析或 demo 代码后，需要按影响范围运行对应验证：


| 变更类型                       | 必跑验证                                                                                                                                                                     |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| mmap 采集、mmap 验证、host 兼容性改动 | 45 秒无栈 mmap 验证：`MMAP_PHYS_APP=com.example.meminfodemo MMAP_PHYS_ACTIVITY=com.example.meminfodemo/.MainActivity ./run_mmap_phys_profile.sh --no-mmap-callstacks -d 45000` |
| mmap 调用栈归因改动               | 跑主功能：`./run_mmap_phys_profile.sh`，采集期间在手机上手动触发目标场景                                                                                                                       |
| Native heap profile 改动     | AI 验证不传时长：`./run_heap_profile.sh`，登录和表就绪后按 `config.sh` 执行测试模块，由模块协程或最长等待时间收尾                                                                                                           |
| heapprofd malloc 统计改动      | 跑独立 demo：`./run_heapprofd_malloc_apk_demo.sh`                                                                                                                            |
| meminfo demo 或解析改动         | 跑 `./run_meminfo_android_demo.sh`                                                                                                                                        |


无栈 mmap 验证只检查 mmap syscall events 和 smaps 健康状态，不启用 heapprofd malloc，不做 malloc/native heap 对比。

---

## `run_mmap_phys_profile.sh`

用途：mmap 真实物理内存归因的主入口，也负责无栈 mmap 健康验证。

采集前会临时隐藏 Android 系统错误/ANR 对话框，避免对话框改变目标应用
焦点；所有退出路径会恢复 `global.hide_error_dialogs` 原值。这不会禁用系统 ANR
检测和记录。

默认行为：

```text
1. 切换到 00wann 目录。
2. 设置 PERFETTO_SYMBOLIZER_MODE=index。
3. 设置 PERFETTO_BINARY_PATH=./workspace/allsymbols/arm64-v8a。
4. 读取 config.sh 和 common_tools.sh。
5. Windows Git Bash 下把 `PerfettoRoot/buildtools/win/clang/bin` 加入 `PATH`，确保 `traceconv.exe` 能启动 `llvm-symbolizer.exe`。
6. 默认目标进程来自 `MMAP_PHYS_APP`；当前 `config.sh` 配置为 `com.tencent.dhwdxkty.trunk.profiler`，未配置时脚本回退同一包名。
7. 推送 FSBootCmdLine.cfg；仅当目标包是 FS 包时推送 debugconfig.txt，demo/其他包会跳过该 FS 专用配置。
8. 调用 collect_mmap_phys_data.py。
9. 默认启用 mmap 调用栈采集，并追加 --classify-config heap_analyzer/fs.ini --top-n 0。
10. 主调用栈模式先抑制 init lazy producer并确认唯一 root `traced_perf`，再启动仅含 ftrace/process_stats 的生命周期 Perfetto 会话，然后重启 App；生命周期会话先于 App，保证启动期 mmap syscall 不漏采。
11. App PID 出现后再启动仅含 `linux.perf` 的调用栈会话，避免进程刚创建时首次描述符 lookup 超时后 PID 永久进入 `kFdsTimedOut`。测试模块结束后停止两个会话和 root producer，并恢复原始系统状态。
```

常用命令：

```bash
./run_mmap_phys_profile.sh

MMAP_PHYS_APP=com.example.meminfodemo \
MMAP_PHYS_ACTIVITY=com.example.meminfodemo/.MainActivity \
./run_mmap_phys_profile.sh --no-mmap-callstacks -d 45000
```

参数说明：本脚本把参数原样透传给 `collect_mmap_phys_data.py`，常用参数如下。


| 参数                     | 默认值                    | 说明                                         |
| ---------------------- | ---------------------- | ------------------------------------------ |
| `-d, --duration-ms`    | `75000`                | 固定时长路径的 Perfetto 采集时长；主调用栈模式由测试模块结束。       |
| `--smaps-interval-ms`  | `1000`                 | smaps 快照间隔，单位 ms。                          |
| `-o, --output`         | 自动生成                   | 输出目录。                                      |
| `--mmap-callstacks`    | 默认开启                   | 采集 mmap 调用栈并运行物理归因分析。                      |
| `--no-mmap-callstacks` | 关闭项                    | 进入无栈验证，只检查 mmap 事件和 smaps。                 |
| `--no-ftrace`          | 关闭项                    | 不启用 ftrace syscall 采集，会跳过无栈 mmap 验证。       |
| `--no-kernel-frames`   | 关闭项                    | mmap 调用栈不采内核帧。                             |
| `--use-su`             | 关闭项                    | 强制用 `su 0` 读取 `/proc/<pid>/smaps`。         |
| `--no-analyze`         | 关闭项                    | 只采集 trace 和 smaps，不运行离线分析。                 |
| `--trace-processor`    | 自动探测                   | 指定 `trace_processor_shell`。                |
| `--traceconv`          | 自动探测                   | 指定 `traceconv`，主功能用于生成 `symbolized-trace`。 |
| `--classify-config`    | `heap_analyzer/fs.ini` | 分类规则文件。                                    |
| `--top-n`              | `0`                    | 输出调用栈数量，`0` 表示全部。                          |


环境变量：


| 变量                   | 默认值                                   | 说明                                    |
| -------------------- | ------------------------------------- | ------------------------------------- |
| `MMAP_PHYS_APP`      | `config.sh` 当前配置为 `com.tencent.dhwdxkty.trunk.profiler` | 目标包名或进程名。 |
| `MMAP_PHYS_ACTIVITY` | 空                                     | 目标进程不存在时用 `am start -n` 拉起的 Activity。 |
| `PERF_PROFILE_ACTION_SCRIPT` | `profile_actions/send_battle_record_gm.py` | 登录和表就绪后执行的 Python 测试模块。 |
| `MMAP_PHYS_USE_ROOT_TRACED_PERF` | `1` | 主调用栈模式是否在 Perfetto 会话前启动唯一 root standalone `traced_perf`；采集期间临时抑制 init lazy producer，收尾恢复原状态。正式归因建议保持开启。 |
| `PYTHON`             | 自动探测                                  | 指定 Python。                            |
| `TRACE_PROCESSOR`    | 自动探测                                  | 覆盖 trace processor。                   |
| `TRACECONV`          | 自动探测                                  | 覆盖 traceconv。                         |

符号化要求：

`PERFETTO_BINARY_PATH=./workspace/allsymbols/arm64-v8a` 只决定 `traceconv symbolize` 去哪里找 `libil2cpp.so` 等带符号 so。Windows 下如果 `traceconv.exe` 不能从 `PATH` 找到 `llvm-symbolizer.exe`，生成的 `symbols` 可能只有地址、没有 `lines.function_name`，导入后 `stack_profile_symbol` 为空，`libil2cpp.so` 在 pprof 中就无法展开到 `il2cpp::vm::Class::Init`、`GlobalMetadata` 等函数名。`run_mmap_phys_profile.sh` 会自动补 `PerfettoRoot/buildtools/win/clang/bin`；手动重跑 `traceconv.exe symbolize` 时也要保留这个路径。


输出：

```text
PerfData/mmap_phys/<时间戳>/
  mmap_phys_config.pbtxt
  mmap_trace.perfetto-trace
  symbolized-trace
  smaps/
  dumpsys_meminfo.txt
  memory_validation.json
  mmap_health_report.md
  mmap_health_report.json
  mmap_phys_attribution.json
  mmap_phys_attribution.pprof.pb.gz
  mmap_classification_summary.xlsx
  mmap_classification_summary.pprof.pb.gz
  pprof_categories/
```

`mmap_health_report.md` 是终端“mmap 健康报告”的 Markdown 落盘版本，优先用表格展示健康检查、smaps/meminfo 对齐和 smaps 分类；`mmap_health_report.json` 保留同一内容的机器可读版本。其中 meminfo 对齐只解析 `dumpsys meminfo` 主表行，标题和 `App Summary` 不进入分类。

验收要点：

```text
主功能：重点看 mmap_phys_attribution.json 和 pprof 数据。
无栈验证：memory_validation.json 中 validation.status 应为 pass，mmap.syscall_events 和 mmap.smaps_snapshots 应大于 0，trace_health 丢失项应为 0。
主功能健康：除无栈字段外，还要求 trace_health.perf_samples_skipped_dataloss=0 且 trace_health.perf_callsites>0。
如果目标进程已有 perf_samples 但 perf_callsites=0，入口输出 MMAP_PROFILE_FAILED|reason=perf_callstacks_missing 并返回失败；常见原因是 traced_perf 无权读取 /proc/<pid>/maps，空 JSON/pprof 不能作为成功结果。
```

两个真机采集脚本现在是统一框架的兼容入口。通用配置位于 `device_test.ini`，
旧 `config.sh` 和环境变量继续兼容；每轮在原专业结果目录额外生成
`run_config.json`、`run_manifest.json`、`run_summary.txt` 和 `report.md`。
通用核心通过 `device_test_framework/` Git 子模块引用，malloc/mmap 和 FS 流程保留在
`device_test_plugins/`；框架结构、配置优先级和子模块升级方式见
`docs/device_test_framework.md`。

主功能保留两份独立 trace：`mmap_trace.perfetto-trace` 保存 App 前启动的 mmap 生命周期，
`mmap_callstack_trace.perfetto-trace` 保存 App PID 出现后启动的 perf 调用栈。分析器按 Linux
全局 tid 和 BOOTTIME 纳秒时间关联；不能直接字节拼接，因为两个 session 的 clock
snapshot 会倒序并触发 `invalid_clock_snapshots` / `sorter_push_event_out_of_order`。

## `collect_mmap_phys_data.py`

用途：底层 mmap 采集器，启动 Perfetto，周期拉取 smaps，保存 meminfo，并按模式调用离线分析器或生成无栈验证报告。

默认行为：

```text
1. 等待目标进程；目标未运行时用 `MMAP_PHYS_ACTIVITY` 或解析到的 launcher Activity 执行 `am start -n`，不使用 `adb monkey`。
2. 生成 mmap_phys_config.pbtxt。
3. 启动设备端 perfetto。
4. 周期保存 /proc/<pid>/smaps。
5. 拉回 mmap_trace.perfetto-trace。
6. 采集结束后保存 dumpsys_meminfo.txt。
7. 生成 memory_validation.json。
8. 默认采 mmap 调用栈，并调用 mmap_phys_analyzer.py。
```

参数说明：


| 参数                                  | 默认值                     | 说明                                                  |
| ----------------------------------- | ----------------------- | --------------------------------------------------- |
| `-n, --name`                        | 必填                      | 目标进程名或包名。                                           |
| `-d, --duration-ms`                 | `75000`                 | Perfetto 采集时长，单位 ms。                                |
| `--smaps-interval-ms`               | `1000`                  | smaps 采样间隔。                                         |
| `-o, --output`                      | 自动生成                    | 输出目录。                                               |
| `--wait-timeout-s`                  | `120`                   | 等待目标进程启动超时，`0` 表示无限等待。                              |
| `--buffer-kb`                       | `262144`                | Perfetto ring buffer 大小，单位 KiB。                     |
| `--perf-ring-buffer-pages`          | `32768`                 | linux.perf 每 CPU ring buffer 页数，`0` 使用 Perfetto 默认。 |
| `--perf-ring-buffer-read-period-ms` | `25`                    | linux.perf ring buffer 读取周期，`0` 使用 Perfetto 默认。     |
| `--mmap-callstacks`                 | 开启                      | 采集 mmap 调用栈并分析。                                     |
| `--no-mmap-callstacks`              | 关闭项                     | 只运行无栈 mmap 事件健康检查。                                  |
| `--no-ftrace`                       | 关闭项                     | 不启用 ftrace syscall 采集。                              |
| `--no-kernel-frames`                | 关闭项                     | perf 调用栈不采内核帧。                                      |
| `--no-guardrails`                   | 关闭项                     | 传递给设备端 `perfetto --no-guardrails`。                  |
| `--use-su`                          | 关闭项                     | 用 `su 0` 读取 smaps。                                  |
| `--no-analyze`                      | 关闭项                     | 不运行离线分析器。                                           |
| `--trace-processor`                 | 自动探测                    | 传给分析器和验证 SQL 的 trace processor。                     |
| `--traceconv`                       | 自动探测                    | 用于符号化 trace。                                        |
| `--classify-config`                 | 空                       | 传给 mmap 分析器的分类配置。                                   |
| `--top-n`                           | 空                       | 传给 mmap 分析器；`0` 表示全部。                               |
| `--analyzer`                        | `mmap_phys_analyzer.py` | 指定离线分析器路径。                                          |


### linux.perf 丢样调参：

先按计数器区分丢样层级；详细流程见
[traced_perf 内部 perf sample 丢失口径](docs/mmap_phys_analyzer.md#traced_perf-内部-perf-sample-丢失口径)。

`memory_validation.json` 中 `trace_health.perf_data_loss` 对应 Perfetto
`perf_cpu_lost_records`，含义是 linux.perf 每 CPU kernel ring buffer overrun。
这个问题优先调 linux.perf kernel ring buffer：


```bash
# 旧默认值；如果出现 perf_data_loss，先不要作为最终结果使用
--perf-ring-buffer-pages 8192 --perf-ring-buffer-read-period-ms 100

# 第一档：启动期 mmap 峰值下常用，实测 perf_data_loss 957 -> 186
--perf-ring-buffer-pages 16384 --perf-ring-buffer-read-period-ms 50

# 第二档：仍有 perf_data_loss 时使用；Pixel 6 约 128 MiB/CPU，总量约 1 GiB
--perf-ring-buffer-pages 32768 --perf-ring-buffer-read-period-ms 50

# 当前默认值/无丢样档：32768/25ms；用于 32768/50ms 仍有丢样时；实测 perf_data_loss=0
--perf-ring-buffer-pages 32768 --perf-ring-buffer-read-period-ms 25
```

`ring_buffer_pages` 是每 CPU 4 KiB 页数，Perfetto 要求该值必须是 2 的幂。

`trace_health.perf_samples_skipped_dataloss` 对应 trace_processor 的
`perf_samples_skipped_dataloss`，含义是 traced_perf 内部 reader 到 unwinder 队列阶段丢失了本应展开调用栈的 perf samples，常见原因是 load shedding。它不是 kernel ring buffer overrun；如果 `perf_data_loss=0` 但该字段非 0，不要只继续增大 `--perf-ring-buffer-pages`。

这类丢样不等同于 Perfetto 全局 trace buffer 不够；如果
`perfetto_data_loss=0`、`ftrace_data_loss=0`，不要优先增大 `--buffer-kb`。

主功能 mmap 调用栈采样使用 `raw_syscalls:sys_enter` + `period: 1`，降低采样频率会改变“每次 mmap enter 都尝试取栈”的语义。当前 FS 启动 mmap 调用栈采样保持 `32768/25ms`，并显式生成 `user_frames: UNWIND_DWARF`。不要用 `--no-kernel-frames` 作为主功能验收手段：mmap tracepoint sample 可能全部处于 kernel cpu_mode，关闭 kernel frames 后会出现 perf samples 存在但 `callsite_id` 全空的无效归因 trace。

缩短 `--perf-ring-buffer-read-period-ms` 主要用于减少 kernel perf ring buffer overrun；它不会降低 sample 产生速率，也不会提高 unwinder 吞吐。对 `perf_samples_skipped_dataloss`，只有在确认问题来自 reader 单次批量入队峰值过大时，才可能通过更频繁、更小批次读取来缓解；如果 unwinder 平均处理速度已经低于输入速度，缩短读取周期只会更频繁地喂队列，不能解决队列满。

当前脚本没有设置 `max_enqueued_footprint_kb`，Perfetto 侧等价于 `0`，即关闭 footprint 阈值检查；因此当前这类 `perf_samples_skipped_dataloss` 更可能来自 unwinder queue 写入失败/队列满，而不是命中 footprint 上限。若仍非 0，继续规避主要是降低调用栈采样压力；这会带来归因完整性的取舍。

本地 Perfetto 的 traced_perf unwinder queue 已从 1024 扩到 4096；若仍出现
`perf_samples_skipped_dataloss`，看 logcat 中 `traced_perf unwind enqueue skipped` 和
`traced_perf unwind enqueue summary`：`queue_full_skips` 表示队列满，`footprint_limit_skips`
表示命中 `max_enqueued_footprint_kb`，`max_queue_size=.../4096` 可判断峰值是否顶到容量。

2026-07-07 在 Pixel 6 `1C111FDF600AW5`、`com.fs.t.prf` 上的结论：历史 loss 来自
`PROFILER_SKIP_UNWIND_ENQUEUE -> perf_samples_skipped_dataloss`，不是
`perf_cpu_lost_records`。安装本地 queue=4096 且带 enqueue 诊断日志的 `traced_perf` 后，
120 秒主采集 `perf_samples_skipped_dataloss=0`。若使用 GN standalone 版
`traced_perf` 侧载到 init service，service 仍按 `user nobody` 运行，可能因
`/proc/<pid>/maps` 权限导致 `callsite_id` 全空。不能只手工再启动一个 root producer：
`linux.perf` 会同时分配给 root 与 init lazy 拉起的 nobody producer。主功能脚本会在
Perfetto 会话前临时抑制 lazy producer并确认设备上只有一个 root `traced_perf`，收尾恢复
原始属性和 service 状态。60 秒登录场景验证结果：
`perf_samples_skipped_dataloss=0`、`perf_cpu_lost_records=0`、
`perf_samples=92046`、`samples_with_callsite=8275`，离线归因输出
`mmap_phys_attribution.json` 和 `mmap_phys_attribution.pprof.pb.gz` 成功。

2026-08-13 Pixel 6 对照结果：唯一 root producer但 perf会话先于 App时为
`perf_samples=21829, perf_callsites=0`；同一 producer改为 App PID出现后再启动 perf短
探针为 `766/766`。双会话完整默认流程为目标 PID `208/208`，且独立加载两份原始 trace
没有 clock snapshot乱序错误。

额外环境变量：


| 变量                   | 默认值 | 说明                                             |
| -------------------- | --- | ---------------------------------------------- |
| `MMAP_PHYS_ACTIVITY` | 空   | 目标未启动时使用该 Activity 拉起，格式为 `package/.Activity`。 |


验收要点：无栈验证不要检查 heapprofd malloc 字段；只看 mmap 事件、smaps 快照和 Perfetto/ftrace 健康。

## `mmap_phys_analyzer.py`

用途：离线 mmap 物理内存归因分析器，把 Perfetto trace 中的 mmap 生命周期、perf 调用栈和 smaps 快照做地址重叠归因。

默认行为：

```text
1. 读取 perf 调用栈采样。
2. 读取 stack_profile 表并展开调用栈。
3. 读取目标 pid 的 mmap/munmap/mremap syscall。
4. 构建 live mmap range 生命周期。
5. 读取 smaps 快照。
6. 按地址重叠把 smaps PSS/RSS 分摊到 mmap 调用栈。
7. 输出 Perfetto Chrome JSON。
8. 如传入 pprof、speedscope 或分类参数，同时输出剖析数据和分类表。
```

参数说明：


| 参数                                  | 默认值       | 说明                                                     |
| ----------------------------------- | --------- | ------------------------------------------------------ |
| `--trace`                           | 必填        | 包含 mmap syscall 生命周期的 Perfetto trace。                   |
| `--callstack-trace`                 | 同 `--trace` | 独立的 linux.perf 调用栈 trace；双 session采集时传 `symbolized-trace`。 |
| `--smaps-dir`                       | 必填        | smaps 快照目录。                                            |
| `--pid`                             | 必填        | 目标进程 pid。                                              |
| `--output`                          | 必填        | 输出 Chrome JSON trace。                                  |
| `--speedscope-output`               | 空         | 额外输出 speedscope JSON。                                  |
| `--pprof-output`                    | 空         | 额外输出 pprof profile.pb.gz。                              |
| `--classify-config`                 | 空         | 使用 `fs.ini` 规则分类 mmap 调用栈。                             |
| `--classify-summary-out`            | 自动路径      | 分类统计 XLSX 输出路径。                                        |
| `--classify-summary-speedscope-out` | 空         | 分类汇总 speedscope 输出路径。                                  |
| `--classify-speedscope-dir`         | 空         | 每个分类单独输出 speedscope 的目录。                               |
| `--classify-summary-pprof-out`      | 空         | 分类汇总 pprof 输出路径。                                       |
| `--classify-pprof-dir`              | 空         | 每个分类单独输出 pprof 的目录。                                    |
| `--trace-processor`                 | 自动探测      | trace processor 路径。                                    |
| `--smaps-ts-unit`                   | `auto`    | smaps 文件名时间戳单位，可选 `auto/ns/us/ms/s`。                   |
| `--smaps-ts-offset-ns`              | `0`       | smaps 时间戳到 trace 时间轴的偏移。                               |
| `--stack-window-ns`                 | `5000000` | mmap enter 与 perf sample 匹配窗口。                         |
| `--top-n`                           | `50`      | 每个快照输出 PSS 最大的 N 个调用栈，`0` 表示全部。                        |


输出验收：

```text
mmap_phys_attribution.json
  -> Perfetto UI 可加载；metadata.final_summary 中 pss_bytes 是主指标。

mmap_phys_attribution.pprof.pb.gz
  -> go tool pprof 可加载；默认 sample type 是 pss_bytes，同时包含 RSS/virtual/dirty/clean/range_count。

mmap_phys_attribution.speedscope.json
  -> 仅显式传 --speedscope-output 时生成；权重单位 bytes，默认按 PSS。
```

## `run_mmap_phys_analyze_latest.sh`

用途：离线分析最近一次 mmap 采集目录，适合只重跑分类、top-n 或输出格式。

默认行为：

```text
1. 在 PerfData/mmap_phys 下寻找最近一个包含 trace 和 smaps 的目录。
2. 优先使用 symbolized-trace，没有则回退 mmap_trace.perfetto-trace。
3. 未传 --pid 时，按 MMAP_PHYS_APP 从 trace 中自动查询 pid。
4. 默认输出 mmap_phys_attribution.json 和 mmap_phys_attribution.pprof.pb.gz。
5. 默认追加 --classify-config heap_analyzer/fs.ini --classify-summary-pprof-out mmap_classification_summary.pprof.pb.gz --classify-pprof-dir pprof_categories --top-n 0。
6. 用户参数追加在默认参数之后，可覆盖本次分析口径。
7. 传入 --latestdir 时直接使用指定目录，不再自动扫描最近目录；该参数只由 wrapper 消费，不透传给分析器。
```

命令：

```bash
./run_mmap_phys_analyze_latest.sh
MMAP_PHYS_APP=com.example.app ./run_mmap_phys_analyze_latest.sh
./run_mmap_phys_analyze_latest.sh --latestdir PerfData/mmap_phys/<时间戳> --pid 1234
./run_mmap_phys_analyze_latest.sh --pid 1234 --top-n 25
```

参数说明：除 `--latestdir` 由 wrapper 消费外，其余参数会透传给 `mmap_phys_analyzer.py`。


| 参数                  | 默认值                    | 说明               |
| ------------------- | ---------------------- | ---------------- |
| `--pid`             | 自动查询                   | 目标 pid。          |
| `--latestdir`       | 自动扫描最近目录                | 指定本次离线分析使用的采集目录；不透传给分析器。 |
| `--trace`           | 最近目录 trace             | 指定 trace。        |
| `--smaps-dir`       | 最近目录 smaps             | 指定 smaps。        |
| `--top-n`           | `0`                    | 输出调用栈数量。         |
| `--classify-config` | `heap_analyzer/fs.ini` | 分类配置。            |
| `--trace-processor` | 自动探测                   | trace processor。 |
| `-h, --help`        | 无                      | 打印用法。            |


环境变量：


| 变量                   | 默认值                                   | 说明                  |
| -------------------- | ------------------------------------- | ------------------- |
| `MMAP_PHYS_DATA_DIR` | `PerfData/mmap_phys`                  | 最近采集目录搜索根目录。        |
| `MMAP_PHYS_APP`      | `com.tencent.dhwdxkty.trunk.profiler` | 自动查询 pid 时使用的进程名。   |
| `TRACE_PROCESSOR`    | 自动探测                                  | 覆盖 trace processor。 |


## `run_heap_profile.sh`

用途：Native heap profile 采集入口，包装 `run_heap_profile.py`。

采集前会临时隐藏 Android 系统错误/ANR 对话框，避免高开销采样期间反复
失焦；所有退出路径会恢复 `global.hide_error_dialogs` 原值。这不会延长 ANR
阈值，系统仍会记录 ANR。

默认行为：

```text
1. 切换到脚本目录并加载 config.sh，再自动选择 Python。
2. 调用 run_heap_profile.py。
3. 目标包名读取 config.sh 的 MMAP_PHYS_APP。
4. 不传 duration；脚本依次等待 `登录场景完成` 和 `RegistForGameStart.LoadOtherTable.End`，再执行 `PERF_PROFILE_ACTION_SCRIPT` 配置的异步测试模块。模块协程完成或最长等待时间到期后请求 Perfetto 收尾；App 死亡时失败。人工 Ctrl+C 会先取消并等待模块清理，再触发 Perfetto 的 SIGINT 收尾。
5. 不传 interval 时使用 1024 bytes。
6. 不传 shmem-size 时使用 8388608 bytes。
7. 未设置 `PERFETTO_BINARY_PATH` 时，优先使用当前 FS 打包产物 `unityLibrary/symbols/arm64-v8a`，并追加 `workspace/allsymbols/arm64-v8a` 作为补充符号目录。
```

命令：

```bash
./run_heap_profile.sh
./run_heap_profile.sh 1024
./run_heap_profile.sh 1024 67108864
```

参数说明：


| 位置参数                | 默认值       | 说明                          |
| ------------------- | --------- | --------------------------- |
| `1: interval_bytes` | `1024`    | heapprofd 采样间隔，单位 bytes。    |
| `2: shmem_size`     | `8388608` | heapprofd 共享缓冲区大小，单位 bytes。 |


环境变量：


| 变量                                        | 默认值            | 说明                                            |
| ----------------------------------------- | -------------- | --------------------------------------------- |
| `RUN_HEAP_PROFILE_PYTHON`                 | 自动探测           | wrapper 使用的 Python。                           |
| `RUN_HEAP_PROFILE_INNER_PYTHON`           | 当前 Python      | 调用 Perfetto `heap_profile.py` 的 Python。       |
| `RUN_HEAP_PROFILE_EXTRA_PATH`             | 空              | 追加到 PATH 前面的路径。                               |
| `ADB_BINARY`                              | `adb`          | adb 可执行文件。                                    |
| `CP_BINARY`                               | `cp`           | 复制命令。                                         |
| `TRACE_PROCESSOR`                         | 自动探测           | trace processor。                              |
| `TRACECONV`                               | 自动探测           | traceconv。                                    |
| `PERFETTO_BINARY_PATH`                    | 自动生成           | traceconv 符号搜索路径；显式设置时脚本原样保留。                 |
| `RUN_HEAP_PROFILE_SYMBOLS_DIR`            | 当前 FS 打包产物符号目录 | 未设置 `PERFETTO_BINARY_PATH` 时，用于覆盖优先符号目录。      |
| `HEAP_PROFILE_ACTIVE_TIMEOUT_S`           | `60`           | 等待 `Profiling active` 的超时。                    |
| `HEAP_PROFILE_LOGIN_TIMEOUT_S`            | `0`            | 等待 `登录场景完成` 的超时；`0` 表示不限制，真机验收不要设置。           |
| `HEAP_PROFILE_GM_READY_TIMEOUT_S`         | `180`          | 登录后等待 `RegistForGameStart.LoadOtherTable.End` 的超时。          |
| `PERF_PROFILE_ACTION_SCRIPT`              | `profile_actions/send_battle_record_gm.py` | 表加载完成后执行的 Python 测试模块。                  |
| `HEAP_PROFILE_LOGIN_STABLE_S`             | `120`          | 默认 GM 模块的兼容等待时间覆盖；新模块直接实现 `get_collection_wait_seconds()`。 |
| `HEAP_PROFILE_RPC_LOCAL_PORT`              | `12346`        | Poco RPC 使用的本机 ADB 转发端口。                                  |
| `HEAP_PROFILE_RPC_TIMEOUT_S`               | `10`           | Poco RPC 连接和响应超时。                                           |
| `HEAP_PROFILE_SHUTDOWN_SIGNAL_TIMEOUT_S`  | `600`          | 等待 profiler shutdown 的超时。                     |
| `HEAP_PROFILE_MEMINFO_ALLOWED_DIFF_BYTES` | `67108864`     | malloc live 和 meminfo Native Heap Alloc 允许差值。 |

执行器为自定义测试模块创建一次性的 `ProfileActionSession`。脚本通过 `session.run_adb()`、`session.invoke_rpc()` 和 `session.wait_for_app_log()` 使用公共功能；设备选择、一次性 Poco 端口发现、复用转发、Poco JSON-RPC 协议、日志和详情文件均由 Session 处理。运行失败返回 `success=False`，是否抛异常由测试脚本决定；RPC 默认要求 `result=True` 并写 `gm_rpc.txt`。


Windows 下脚本会把 `PerfettoRoot/buildtools/win/clang/bin` 加入 `PATH`，确保 `traceconv.exe` 可以启动 `llvm-symbolizer.exe` 完成符号化。

输出：

```text
PerfData/mem/<时间戳>/
  heap_profile.log
  heap_profile_config.txt
  run_summary.txt
  gm_rpc.txt
  raw-trace
  symbolized-trace
  heap_dump.*.pb 或 heap_dump.*.pb.gz
  dumpsys_meminfo.txt
  heap_meminfo_validation.txt
```

验收要点：`heap_meminfo_validation.txt` 应输出 `HEAP_MEMINFO_VALIDATION=PASS`；如果出现 `heapprofd_rejected_concurrent`，需要先清理残留 profiling session 再重跑。

## `run_heap_profile.py`

用途：Native heap profile 的实际控制器，负责子进程管理、中断转发、启动目标 App、meminfo 抓取和采集后验证。

默认行为：

```text
1. 使用入口从 config.sh 导出的 MMAP_PHYS_APP，并读取 PerfettoRoot。
2. 设置符号化和 PYTHONPATH 环境。
3. 自动选择 traceconv 和 trace_processor。
4. 推送 FSBootCmdLine.cfg 和 debugconfig.txt。
5. force-stop 目标 App。
6. 启动 Perfetto heap_profile.py，并等待 Profiling active。
7. 使用 `adb shell am start -n $MMAP_PHYS_APP/com.dhplugin.unity.MainActivity` 拉起固定 Activity。
8. 等待 logcat 输出 `登录场景完成` 和 `RegistForGameStart.LoadOtherTable.End`，再加载 `PERF_PROFILE_ACTION_SCRIPT` 指定的测试模块。
9. 创建一次 `ProfileActionSession`，先调用同步 `get_collection_wait_seconds(session)`，再运行异步 `run_profile_action(session)`；协程完成、最长等待时间、人工中断或 App 死亡进行竞速，最后由 Session 统一清理公共资源。默认模块会调用战斗录像 GM 并声明 120 秒最长等待。
10. 查询 heap_profile_allocation 累计 live bytes，与 dumpsys meminfo Native Heap Alloc 做 64 MiB 阈值验证。
```

参数说明：


| 位置参数                | 默认值       | 说明                                 |
| ------------------- | --------- | ---------------------------------- |
| `1: interval_bytes` | `1024`    | 传给 `heap_profile.py -i`。           |
| `2: shmem_size`     | `8388608` | 传给 `heap_profile.py --shmem-size`。 |


环境变量同 `run_heap_profile.sh`。

注意：Native heap 采集使用 `config.sh` 中目标包的固定 `com.dhplugin.unity.MainActivity`，不使用 `adb monkey` 随机触发测试场景。

## `run_heap_startup_eval.sh`

用途：评估 Native heap profile 参数对目标 App 启动耗时和采样健康的影响。

默认行为：

```text
1. 先跑一轮无 heapprofd baseline。
2. 默认对 interval 512、256、128、64、32、16 逐个采集。
3. 每轮先 force-stop App，再启动 heapprofd。
4. 等待 Profiling active 后启动 Activity。
5. 等待 logcat 出现 “LAN 更新流程开始”。
6. 等 45 秒采集结束。
7. 输出启动耗时、heap dump 数、allocation 行数和 health_sum。
```

命令：

```bash
./run_heap_startup_eval.sh
./run_heap_startup_eval.sh 45000 268435456 1024 2048 4096
HEAP_STARTUP_DRY_RUN=1 ./run_heap_startup_eval.sh 45000 268435456 512 256
```

参数说明：


| 位置参数              | 默认值                    | 说明                  |
| ----------------- | ---------------------- | ------------------- |
| `1: duration_ms`  | `45000`                | 每轮 heapprofd 采集时长。  |
| `2: shmem_size`   | `268435456`            | heapprofd shmem 大小。 |
| `3...: intervals` | `512 256 128 64 32 16` | 待评估的采样间隔列表。         |


环境变量：


| 变量                            | 默认值                                                                   | 说明                  |
| ----------------------------- | --------------------------------------------------------------------- | ------------------- |
| `HEAP_STARTUP_APP`            | `com.tencent.dhwdxkty.trunk.profiler`                                 | 目标包名。               |
| `HEAP_STARTUP_ACTIVITY`       | `com.tencent.dhwdxkty.trunk.profiler/com.dhplugin.unity.MainActivity` | 启动 Activity。        |
| `HEAP_STARTUP_PATTERN`        | `LAN 更新流程开始`                                                          | 业务启动完成日志。           |
| `HEAP_STARTUP_WAIT_TIMEOUT_S` | `90`                                                                  | 等待目标日志超时。           |
| `HEAP_STARTUP_DRY_RUN`        | `0`                                                                   | 为 `1` 时只打印配置，不执行采集。 |
| `TRACE_PROCESSOR`             | 自动探测                                                                  | trace processor。    |
| `TRACECONV`                   | 自动探测                                                                  | traceconv。          |


输出：终端输出 `CONFIG|...`、`BASELINE|...`、`CASE|...` 和 `RESULT|...` 行；采集文件保存到 `PerfData/mem/startup_eval_<时间戳>_<标签>/`。

验收要点：启动完成点必须以 logcat 中 `LAN 更新流程开始` 为准，不能用 `am start -W` 的 Activity 可见时间替代。

## `run_heap_alloc_stacks_by_symbol_latest.sh`

用途：分析最近一次 Native heap trace，查询指定符号相关分配栈，默认做全量 fs.ini 分类。

默认行为：

```text
1. 在 PerfData/mem 下找最近一次 Native heap trace 目录。
2. 优先选择 symbolized-trace，其次 raw-trace，再其次 *.perfetto-trace。
3. 默认调用 heap_analyzer/query_heap_alloc_stacks_by_symbol.py。
4. 默认追加 --classify-config heap_analyzer/fs.ini --all-allocations --limit 0。
5. 如果用户显式传入 --symbol，则不强制追加 --all-allocations。
6. Windows Git Bash 调用原生 Windows Python 时，会先把 Python 入口脚本路径转成盘符路径，避免 `/d/...` 被误解析成 `D:\d\...`。
```

命令：

```bash
./run_heap_alloc_stacks_by_symbol_latest.sh
./run_heap_alloc_stacks_by_symbol_latest.sh --limit 25
./run_heap_alloc_stacks_by_symbol_latest.sh --symbol malloc --limit 50
```

参数说明：参数原样透传给 `heap_analyzer/query_heap_alloc_stacks_by_symbol.py`。


| 参数                  | 默认值                    | 说明                |
| ------------------- | ---------------------- | ----------------- |
| `--trace`           | 最近 trace               | 指定 trace。         |
| `--symbol`          | 不默认强塞                  | 按符号筛选调用栈。         |
| `--limit`           | `0`                    | wrapper 默认不打印长明细。 |
| `--classify-config` | `heap_analyzer/fs.ini` | 分类配置。             |
| `-h, --help`        | 无                      | 打印用法。             |


环境变量：


| 变量                      | 默认值            | 说明             |
| ----------------------- | -------------- | -------------- |
| `HEAP_PROFILE_DATA_DIR` | `PerfData/mem` | 最近 trace 搜索目录。 |
| `PYTHON`                | 自动探测           | 指定 Python。     |


## `heap_analyzer/query_heap_alloc_stacks_by_symbol.py`

用途：Native heap trace 调用栈查询和分类分析工具。

默认行为：

```text
1. 读取 symbolized trace。
2. 使用 trace_processor 导出 frame、callsite、allocation 和 process 基础表。
3. 默认按符号 il2cpp::vm::Class::Init 筛选调用栈。
4. 如果传入 --classify-config 且未显式传 --symbol，则分析全部 heap_profile_allocation。
5. 默认终端输出最多 50 条分配栈。
6. 默认不输出 speedscope；pprof 参数不传时也不额外输出 pprof。
```

参数说明：


| 参数                                  | 默认值                       | 说明                                                       |
| ----------------------------------- | ------------------------- | -------------------------------------------------------- |
| `--trace`                           | 内置历史路径                    | symbolized trace 路径；实际使用建议显式指定或通过 wrapper。               |
| `--symbol`                          | `il2cpp::vm::Class::Init` | 调用栈匹配符号子串；配合 `--all-allocations` 时忽略。                    |
| `--all-allocations`                 | 关闭                        | 不按符号过滤，分析全部分配栈。                                          |
| `--trace-processor`                 | 内置默认路径                    | trace processor 可执行文件。                                   |
| `--limit`                           | `50`                      | 终端输出分配栈数量，`0` 表示只输出摘要和文件。                                |
| `--speedscope-out`                  | 空                         | 输出 speedscope JSON；相对路径写入 trace 同级 `heap_analyze/`。      |
| `--pprof-out`                       | 空                         | 输出 pprof；不带路径时写入 `heap_analyze/native_heap.pprof.pb.gz`。 |
| `--speedscope-weight`               | `positive-net`            | `positive-net` 只看正向净分配；`absolute-net` 看净变化绝对值。           |
| `--classify-config`                 | 空                         | 按 fs.ini 对分配栈分类。                                         |
| `--classify-speedscope-dir`         | 空                         | 每分类输出 speedscope 的目录。                                    |
| `--classify-summary-out`            | 自动路径                      | 分类统计 XLSX 输出路径。                                          |
| `--classify-summary-speedscope-out` | 自动路径                      | 分类汇总 speedscope 输出路径。                                    |


输出：

```text
<trace 同级>/heap_analyze/
  native_heap.pprof.pb.gz
  category_summary.pprof.pb.gz
  pprof_categories/
  summary.xlsx
  summary.speedscope.json
  <自定义 speedscope 输出>
```

`pprof_categories` 内分类文件名前的两位序号按分类树先序生成：大分类首次出现顺序来自 `fs.ini`，同一大分类的父分类和子分类连续编号；规则命中优先级仍按 `fs.ini` 文件顺序执行。

验收要点：`net_alloc_bytes` 是带符号净变化，可能为负；看最终净增长用带符号值，看变化规模用 `--speedscope-weight absolute-net`。

## `heap_analyzer/classification.py`

用途：fs.ini 调用栈分类共用库，被 Native heap 和 mmap 分类输出复用。

默认行为：

```text
1. 解析 fs.ini。
2. 规则按文件顺序匹配。
3. 一条调用栈命中一个分类后，不再进入后续分类。
4. 未命中的调用栈进入 remaining。
5. 匹配前会移除 C++ 函数参数表，避免参数类型名触发分类；函数名本身的关键字仍可命中。
6. 分类名中的 / 会展开为层级，父节点聚合子分类。
7. 可写出不依赖 openpyxl 的 xlsx 文件。
```

参数说明：该文件不是命令行工具，没有 CLI 参数。主要函数参数如下。


| 函数                                               | 参数                         | 说明              |
| ------------------------------------------------ | -------------------------- | --------------- |
| `parse_classification_config(path)`              | `path`                     | 读取 fs.ini 分类配置。 |
| `classify_items(items, rules, stack_getter)`     | `items/rules/stack_getter` | 按规则顺序分类。        |
| `build_hierarchy_entries(classified, remaining)` | 分类结果                       | 生成带父子层级的分类节点；输出顺序按大分类聚合，让父分类和子分类文件编号相邻。 |
| `write_xlsx(path, sheets)`                       | 输出路径和 sheet 数据             | 写出 xlsx。        |


## `heap_analyzer/heap_alloc_stacks_by_symbol.sql`

用途：Native heap 调用栈查询的 SQL 语义参考。

默认行为：

```text
1. 使用递归 CTE 展开调用栈。
2. 默认目标符号需要在 SQL 内的 target(symbol) 中配置。
3. 在大 trace 上可能超时，因此生产使用优先选 Python 查询工具。
```

参数说明：SQL 文件没有命令行参数；运行时由 `trace_processor_shell query -f` 提供 trace。

命令示例：

```bash
../../perfetto/out/linux_clang_release/trace_processor_shell query \
  -f heap_analyzer/heap_alloc_stacks_by_symbol.sql \
  PerfData/mem/<时间戳>/symbolized-trace
```

## `run_heapprofd_malloc_apk_demo.sh`

用途：独立验证 Perfetto/heapprofd malloc 分配量统计能力。

默认行为：

```text
1. 构建 heapprofd malloc demo APK。
2. 安装包名 com.example.heapprofddemo。
3. force-stop 后启动 Perfetto/heapprofd。
4. 冷启动 APK demo。
5. demo 默认累计 malloc 1 GiB，并保持 live 分配。
6. 抓取 dumpsys meminfo。
7. 拉回 trace 并查询 heap_profile_allocation。
8. 生成 malloc_demo_report.json。
```

命令：

```bash
./run_heapprofd_malloc_apk_demo.sh

TOTAL_BYTES=1073741824 \
START_DELAY_SECONDS=10 \
ALLOC_SECONDS=60 \
HOLD_SECONDS=20 \
MALLOC_SHMEM_SIZE_BYTES=268435456 \
./run_heapprofd_malloc_apk_demo.sh
```

参数说明：该脚本不接收位置参数，使用环境变量配置。


| 环境变量                             | 默认值                                                | 说明                    |
| -------------------------------- | -------------------------------------------------- | --------------------- |
| `TOTAL_BYTES`                    | `1073741824`                                       | demo 计划持有的 malloc 总量。 |
| `START_DELAY_SECONDS`            | `10`                                               | 启动后延迟分配时间。            |
| `ALLOC_SECONDS`                  | `60`                                               | 分配持续时间。               |
| `HOLD_SECONDS`                   | `20`                                               | 分配完成后保持 live 的时间。     |
| `DURATION_MS`                    | `(START_DELAY_SECONDS + ALLOC_SECONDS + 5) * 1000` | Perfetto 采集时长。        |
| `MALLOC_SAMPLING_INTERVAL_BYTES` | `4096`                                             | heapprofd 采样间隔。       |
| `MALLOC_SHMEM_SIZE_BYTES`        | `268435456`                                        | heapprofd shmem 大小。   |
| `TRACE_PROCESSOR`                | 自动探测                                               | trace processor。      |
| `ANDROID_SDK_ROOT`               | 自动探测                                               | Android SDK。          |
| `ANDROID_NDK_ROOT`               | 自动探测                                               | Android NDK。          |
| `ANDROID_BUILD_TOOLS`            | 自动探测                                               | build-tools 目录。       |
| `ANDROID_JAR`                    | 自动探测                                               | android.jar。          |
| `UNITY_ANDROID_ROOT`             | Unity 2022.3.62 AndroidPlayer                      | Windows 默认工具链根目录。     |


输出：

```text
PerfData/heapprofd_malloc_apk_demo/<时间戳>/
  heapprofd_malloc_apk_demo.apk
  perfetto_config.pbtxt
  malloc_demo.perfetto-trace
  dumpsys_meminfo.txt
  malloc_demo_report.json
```

验收要点：

```text
health.heapprofd_data_loss == 0
health.heapprofd_errors == 0
heapprofd.cumulative.live_bytes 接近 demo.expected_live_bytes
```

## `run_meminfo_android_demo.sh`

用途：构建、安装并运行 meminfo Android demo，验证 `dumpsys meminfo` 各类指标是否可观测。

默认行为：

```text
1. 调用 meminfo_android_demo/build_demo_apk.sh 构建 APK。
2. 安装 com.example.meminfodemo。
3. 启动普通 Activity，抓 baseline dumpsys meminfo。
4. force-stop。
5. 带 auto_allocate=true 启动 Activity。
6. 等待 MEMINFO_DEMO_READY。
7. 抓 after dumpsys meminfo。
8. 调用 verify_meminfo_demo.py 比较增长。
9. 输出 verify.txt。
```

命令：

```bash
./run_meminfo_android_demo.sh
```

参数说明：该脚本无位置参数。

环境变量：


| 变量                    | 默认值                           | 说明                |
| --------------------- | ----------------------------- | ----------------- |
| `ANDROID_SDK_ROOT`    | 自动探测                          | Android SDK。      |
| `ANDROID_NDK_ROOT`    | 自动探测                          | Android NDK。      |
| `ANDROID_BUILD_TOOLS` | 自动探测                          | build-tools。      |
| `ANDROID_JAR`         | 自动探测                          | android.jar。      |
| `UNITY_ANDROID_ROOT`  | Unity 2022.3.62 AndroidPlayer | Windows 默认工具链根目录。 |
| `PYTHON`              | 自动探测                          | 校验脚本使用的 Python。   |


输出：

```text
PerfData/meminfo_demo_YYYYMMDD_HHMMSS/
  baseline_meminfo.txt
  after_meminfo.txt
  verify.txt
```

验收要点：`verify.txt` 中 Native Heap、Other mmap、Unknown、SQLite 相关检查必须 PASS；Graphics 受设备 memtrack HAL 影响，当前真机已可 PASS。

## `meminfo_android_demo/build_demo_apk.sh`

用途：构建 meminfo demo APK。

默认行为：

```text
1. 编译 JNI native lib。
2. 编译 Android resources。
3. 编译 Java。
4. 用 dx.jar 生成 classes.dex。
5. 用 JDK jar 组装 APK。
6. zipalign。
7. 用 apksigner.jar 签名。
8. 输出 build/meminfo-demo.apk。
```

命令：

```bash
meminfo_android_demo/build_demo_apk.sh
```

参数说明：无位置参数，使用工具链环境变量。


| 环境变量                  | 默认值                           | 说明                |
| --------------------- | ----------------------------- | ----------------- |
| `ANDROID_SDK_ROOT`    | 自动探测                          | Android SDK。      |
| `ANDROID_NDK_ROOT`    | 自动探测                          | Android NDK。      |
| `ANDROID_BUILD_TOOLS` | 自动探测                          | build-tools。      |
| `ANDROID_JAR`         | 自动探测                          | android.jar。      |
| `UNITY_ANDROID_ROOT`  | Unity 2022.3.62 AndroidPlayer | Windows 默认工具链根目录。 |


默认兼容策略：Windows Git Bash 下直接调用 NDK `clang++`、`dx.jar`、`apksigner.jar` 和 JDK `jar`，避免 `.cmd/.bat` 与空格路径问题。

## `meminfo_android_demo/verify_meminfo_demo.py`

用途：解析两份 `dumpsys meminfo`，验证 demo 自动分配后的指标增长。

默认行为：

```text
1. 读取 baseline meminfo。
2. 读取 after meminfo。
3. 解析主表、App Summary、SQL 和 DATABASES。
4. 检查 Native Heap、Other mmap、Unknown、Graphics、SQLite MEMORY_USED、SQLite db size 增长。
5. 必选项失败时返回非 0。
```

参数说明：


| 参数           | 默认值 | 说明                             |
| ------------ | --- | ------------------------------ |
| `--baseline` | 必填  | baseline `dumpsys meminfo` 文本。 |
| `--after`    | 必填  | after `dumpsys meminfo` 文本。    |


命令：

```bash
python meminfo_android_demo/verify_meminfo_demo.py \
  --baseline PerfData/meminfo_demo_<时间戳>/baseline_meminfo.txt \
  --after PerfData/meminfo_demo_<时间戳>/after_meminfo.txt
```

## `fsbootcmd_push_to_phone.sh`

用途：把 `FSBootCmdLine.cfg` 推送到设备 `/data/local/tmp`，供目标 App 或调试流程读取。

默认行为：

```text
1. 读取 config.sh。
2. 设置 MSYS_NO_PATHCONV=1。
3. 删除设备端旧 /data/local/tmp/FSBootCmdLine.cfg。
4. adb push 本地 FSBootCmdLine.cfg。
5. touch 文件并打印 ls -l。
```

命令：

```bash
source fsbootcmd_push_to_phone.sh
```

参数说明：无位置参数。

可调整项：


| 变量          | 默认值                 | 说明             |
| ----------- | ------------------- | -------------- |
| `phonePath` | `/data/local/tmp`   | 设备端目录，脚本内固定赋值。 |
| `cfgName`   | `FSBootCmdLine.cfg` | 配置文件名，脚本内固定赋值。 |


## `pprof.sh`

用途：简化 `go tool pprof` HTTP UI 启动。

默认行为：

```text
1. 调用 go tool pprof。
2. 监听 0.0.0.0:8001。
3. 后续参数原样传给 pprof。
```

命令：

```bash
./pprof.sh PerfData/mem/<时间戳>/heap_analyze/native_heap.pprof.pb.gz
```

参数说明：


| 参数   | 默认值 | 说明                                       |
| ---- | --- | ---------------------------------------- |
| `$@` | 无   | 原样传给 `go tool pprof -http=0.0.0.0:8001`。 |


## `common_tools.sh`

用途：宿主机工具探测和 Windows Git Bash 兼容函数库。

默认行为：

```text
1. 不直接执行采集。
2. 被各入口脚本 source。
3. 根据宿主机选择 Python、Perfetto 工具、Android SDK/NDK/JDK。
4. Windows Git Bash 下把宿主机工具路径转换为 Windows 路径执行。
```

参数说明：该文件不是命令行入口，没有位置参数。主要函数参数如下。


| 函数                                                 | 参数           | 默认行为                                                |
| -------------------------------------------------- | ------------ | --------------------------------------------------- |
| `select_python`                                    | 无            | 依次找 `PYTHON`、`python3`、`python`、`py`。               |
| `select_perfetto_tool tool perfetto_root override` | 工具名、根目录、覆盖路径 | 根据 Windows/Linux 优先级选择 trace processor 或 traceconv。 |
| `select_android_sdk_root`                          | 无            | 优先环境变量，再回退 Unity AndroidPlayer/SDK。                 |
| `select_android_ndk_root`                          | 无            | 优先环境变量，再回退 Unity AndroidPlayer/NDK。                 |
| `select_build_tools_dir sdk_root`                  | SDK 根目录      | 优先 `ANDROID_BUILD_TOOLS`，再找 build-tools。            |
| `select_android_jar sdk_root`                      | SDK 根目录      | 优先 `ANDROID_JAR`，再找 android.jar。                    |
| `run_host_tool tool args...`                       | 工具和参数        | Windows Git Bash 下处理 `.cmd/.bat` 与路径转换。             |


## `config.sh`

用途：提供 00wann 脚本统一的 Perfetto 源码根目录。

默认行为：

```text
export PerfettoRoot='../../perfetto'
```

参数说明：无命令行参数；被其他脚本 `source` 后提供 `PerfettoRoot` 环境变量。

## `heap_analyzer/fs.ini`

用途：Native heap 和 mmap 分类规则配置。

默认行为：

```text
1. 以 # 开头的行为分类名。
2. 后续非空行为该分类关键字。
3. 规则按文件顺序匹配。
4. 分类名中的 / 表示层级。
5. 分类输出文件编号按大分类聚合，父分类和子分类相邻。
6. 未匹配项进入 remaining。
```

参数说明：配置文件无参数；通过 `--classify-config heap_analyzer/fs.ini` 传给分析工具。

## `dumpsys_meminfo_metrics.md`

用途：说明 `adb shell dumpsys meminfo <package>` 指标口径。

默认行为：这是文档，不执行命令；用于解释 PSS、RSS、Private Dirty、Native Heap、Other mmap、Unknown、Graphics、SQL 和 DATABASES。

参数说明：无参数。

---

## 测试工具

### `test_mmap_phys_analyzer.py`

用途：mmap 离线分析器单元测试。

默认行为：构造合成 trace_processor 输出和 smaps，验证 mmap 生命周期、PSS 分摊、JSON 输出、分类、无栈验证 SQL 口径等。

参数说明：使用 Python `unittest` 参数。

命令：

```bash
python -B -m unittest -v test_mmap_phys_analyzer.py
```

可选环境变量：


| 变量                                 | 默认值  | 说明                       |
| ---------------------------------- | ---- | ------------------------ |
| `MMAP_PHYS_TEST_OUTPUT`            | 临时目录 | 保留测试生成的 Perfetto JSON。   |
| `MMAP_PHYS_TEST_SPEEDSCOPE_OUTPUT` | 临时目录 | 保留测试生成的 speedscope JSON。 |


### `test_run_mmap_phys_profile.sh`

用途：测试 `run_mmap_phys_profile.sh` 参数转发和默认配置。

默认行为：使用假工具模拟采集环境，检查默认主功能、无栈验证、分类参数和默认时长。

参数说明：无位置参数。

命令：

```bash
bash test_run_mmap_phys_profile.sh
```

### `test_run_mmap_phys_analyze_latest.sh`

用途：测试 `run_mmap_phys_analyze_latest.sh` 最近目录选择、pid 自动查询和用户参数覆盖。

默认行为：构造临时 `PerfData/mmap_phys`，用假 trace processor 和假分析器验证 wrapper 行为。

参数说明：无位置参数。

命令：

```bash
bash test_run_mmap_phys_analyze_latest.sh
```

### `test_run_heap_profile.sh`

用途：测试 `run_heap_profile.sh` 和 `run_heap_profile.py` 的启动、参数和中断处理。

默认行为：使用假 adb、假 Perfetto 工具和假 heap_profile.py，验证应用未启动时会先拉起、Ctrl+C 会转发给 profiler、Windows Ctrl-Break bridge 会触发 SIGINT 收尾、采集后会做 meminfo 验证。

参数说明：无位置参数。

命令：

```bash
bash test_run_heap_profile.sh
python -m py_compile run_heap_profile.py
```

### `test_run_heap_startup_eval.sh`

用途：测试启动耗时评估脚本的 dry-run 和配置输出。

默认行为：验证无 duration、登录日志触发收尾、shmem、intervals、目标 App 和日志 pattern。

参数说明：无位置参数；脚本内部会设置 dry-run 流程。

命令：

```bash
bash test_run_heap_startup_eval.sh
```

### `test_run_heap_alloc_stacks_by_symbol_latest.sh`

用途：测试 Native heap 最新 trace wrapper。

默认行为：构造临时 trace 目录，验证最近目录选择、默认 `--all-allocations`、显式 `--symbol` 时的覆盖行为。

参数说明：无位置参数。

命令：

```bash
bash test_run_heap_alloc_stacks_by_symbol_latest.sh
```

### `heap_analyzer/test_query_heap_alloc_stacks_by_symbol.py`

用途：测试 Native heap 调用栈查询、分类、pprof 和 speedscope 输出。

默认行为：使用 Python `unittest` 构造基础表数据，验证分类、权重、输出路径和符号匹配。

参数说明：使用 Python `unittest` 参数。

命令：

```bash
python -B -m unittest -v heap_analyzer/test_query_heap_alloc_stacks_by_symbol.py
```

### `heap_analyzer/test_classification.py`

用途：测试 fs.ini 分类库。

默认行为：验证分类解析、顺序匹配、remaining、层级聚合和 xlsx 写出。

参数说明：使用 Python `unittest` 参数。

命令：

```bash
python -B -m unittest -v heap_analyzer/test_classification.py
```

### `test_meminfo_android_demo.py`

用途：测试 meminfo demo 解析和增长校验逻辑。

默认行为：构造 meminfo 样本，验证 Native Heap、Other mmap、Unknown、Graphics 和 SQLite 解析与阈值判断。

参数说明：使用 Python `unittest` 参数。

命令：

```bash
python -B -m unittest -v test_meminfo_android_demo.py
```

## 常用真机验证命令

```bash
# 1. 确认设备在线
adb devices

# 2. meminfo demo
./run_meminfo_android_demo.sh

# 3. heapprofd malloc 独立 demo
./run_heapprofd_malloc_apk_demo.sh

# 4. 45 秒无栈 mmap 验证
MMAP_PHYS_APP=com.example.meminfodemo \
MMAP_PHYS_ACTIVITY=com.example.meminfodemo/.MainActivity \
./run_mmap_phys_profile.sh --no-mmap-callstacks -d 45000

# 5. Native heap profile AI 验证
./run_heap_profile.sh
```
