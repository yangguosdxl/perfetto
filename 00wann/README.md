# 00wann 工具总说明

本文是 `00wann` 目录的统一入口，集中说明每个工具的用途、默认行为、参数和输出。更细的实现逻辑仍保留在专项文档中：

| 专项文档 | 内容 |
| --- | --- |
| `mmap_phys_analyzer.md` | mmap 真实物理内存归因、无栈 mmap 验证、分类输出和已知边界。 |
| `heap_profile.md` | Native heap profile 采集、meminfo 对比和启动耗时评估。 |
| `heap_analyzer/README.md` | Native heap 调用栈查询、fs.ini 分类、pprof 和 speedscope 输出。 |
| `meminfo_android_demo_validation.md` | `dumpsys meminfo` 真机 demo 的构建、指标和验证结论。 |
| `dumpsys_meminfo_metrics.md` | Android `dumpsys meminfo` 各行各列口径说明。 |

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
  run_mmap_phys_profile.sh
    -> collect_mmap_phys_data.py
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

| 变更类型 | 必跑验证 |
| --- | --- |
| mmap 采集、mmap 验证、host 兼容性改动 | 45 秒无栈 mmap 验证：`MMAP_PHYS_APP=com.example.meminfodemo MMAP_PHYS_ACTIVITY=com.example.meminfodemo/.MainActivity ./run_mmap_phys_profile.sh --no-mmap-callstacks -d 45000` |
| mmap 调用栈归因改动 | 跑主功能：`./run_mmap_phys_profile.sh`，采集期间在手机上手动触发目标场景 |
| Native heap profile 改动 | AI 验证必须带时长：`./run_heap_profile.sh 45000` |
| heapprofd malloc 统计改动 | 跑独立 demo：`./run_heapprofd_malloc_apk_demo.sh` |
| meminfo demo 或解析改动 | 跑 `./run_meminfo_android_demo.sh` |

无栈 mmap 验证只检查 mmap syscall events 和 smaps 健康状态，不启用 heapprofd malloc，不做 malloc/native heap 对比。

---

## `run_mmap_phys_profile.sh`

用途：mmap 真实物理内存归因的主入口，也负责无栈 mmap 健康验证。

默认行为：

```text
1. 切换到 00wann 目录。
2. 设置 PERFETTO_SYMBOLIZER_MODE=index。
3. 设置 PERFETTO_BINARY_PATH=./workspace/allsymbols/arm64-v8a。
4. 读取 config.sh 和 common_tools.sh。
5. 默认目标进程为 com.tencent.dhwdxkty.trunk.profiler。
6. 推送 FSBootCmdLine.cfg 和 debugconfig.txt。
7. 调用 collect_mmap_phys_data.py。
8. 默认启用 mmap 调用栈采集，并追加 --classify-config heap_analyzer/fs.ini --top-n 0。
9. 输出到 PerfData/mmap_phys/<时间戳>/。
```

常用命令：

```bash
./run_mmap_phys_profile.sh

MMAP_PHYS_APP=com.example.meminfodemo \
MMAP_PHYS_ACTIVITY=com.example.meminfodemo/.MainActivity \
./run_mmap_phys_profile.sh --no-mmap-callstacks -d 45000
```

参数说明：本脚本把参数原样透传给 `collect_mmap_phys_data.py`，常用参数如下。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-d, --duration-ms` | `75000` | Perfetto 采集时长，单位 ms。 |
| `--smaps-interval-ms` | `1000` | smaps 快照间隔，单位 ms。 |
| `-o, --output` | 自动生成 | 输出目录。 |
| `--mmap-callstacks` | 默认开启 | 采集 mmap 调用栈并运行物理归因分析。 |
| `--no-mmap-callstacks` | 关闭项 | 进入无栈验证，只检查 mmap 事件和 smaps。 |
| `--no-ftrace` | 关闭项 | 不启用 ftrace syscall 采集，会跳过无栈 mmap 验证。 |
| `--no-kernel-frames` | 关闭项 | mmap 调用栈不采内核帧。 |
| `--use-su` | 关闭项 | 强制用 `su 0` 读取 `/proc/<pid>/smaps`。 |
| `--no-analyze` | 关闭项 | 只采集 trace 和 smaps，不运行离线分析。 |
| `--trace-processor` | 自动探测 | 指定 `trace_processor_shell`。 |
| `--traceconv` | 自动探测 | 指定 `traceconv`，主功能用于生成 `symbolized-trace`。 |
| `--classify-config` | `heap_analyzer/fs.ini` | 分类规则文件。 |
| `--top-n` | `0` | 输出调用栈数量，`0` 表示全部。 |

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MMAP_PHYS_APP` | `com.tencent.dhwdxkty.trunk.profiler` | 目标包名或进程名。 |
| `MMAP_PHYS_ACTIVITY` | 空 | 目标进程不存在时用 `am start -n` 拉起的 Activity。 |
| `PYTHON` | 自动探测 | 指定 Python。 |
| `TRACE_PROCESSOR` | 自动探测 | 覆盖 trace processor。 |
| `TRACECONV` | 自动探测 | 覆盖 traceconv。 |

输出：

```text
PerfData/mmap_phys/<时间戳>/
  mmap_phys_config.pbtxt
  mmap_trace.perfetto-trace
  symbolized-trace
  smaps/
  dumpsys_meminfo.txt
  memory_validation.json
  mmap_phys_attribution.json
  mmap_phys_attribution.speedscope.json
  mmap_classification_summary.xlsx
  mmap_classification_summary.speedscope.json
  mmap_categories/
```

验收要点：

```text
主功能：重点看 mmap_phys_attribution.json 和 speedscope 火焰图。
无栈验证：memory_validation.json 中 validation.status 应为 pass，mmap.syscall_events 和 mmap.smaps_snapshots 应大于 0，trace_health 丢失项应为 0。
```

## `collect_mmap_phys_data.py`

用途：底层 mmap 采集器，启动 Perfetto，周期拉取 smaps，保存 meminfo，并按模式调用离线分析器或生成无栈验证报告。

默认行为：

```text
1. 等待或启动目标进程。
2. 生成 mmap_phys_config.pbtxt。
3. 启动设备端 perfetto。
4. 周期保存 /proc/<pid>/smaps。
5. 拉回 mmap_trace.perfetto-trace。
6. 采集结束后保存 dumpsys_meminfo.txt。
7. 生成 memory_validation.json。
8. 默认采 mmap 调用栈，并调用 mmap_phys_analyzer.py。
```

参数说明：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-n, --name` | 必填 | 目标进程名或包名。 |
| `-d, --duration-ms` | `75000` | Perfetto 采集时长，单位 ms。 |
| `--smaps-interval-ms` | `1000` | smaps 采样间隔。 |
| `-o, --output` | 自动生成 | 输出目录。 |
| `--wait-timeout-s` | `120` | 等待目标进程启动超时，`0` 表示无限等待。 |
| `--buffer-kb` | `262144` | Perfetto ring buffer 大小，单位 KiB。 |
| `--perf-ring-buffer-pages` | `8192` | linux.perf 每 CPU ring buffer 页数，`0` 使用 Perfetto 默认。 |
| `--perf-ring-buffer-read-period-ms` | `100` | linux.perf ring buffer 读取周期，`0` 使用 Perfetto 默认。 |
| `--mmap-callstacks` | 开启 | 采集 mmap 调用栈并分析。 |
| `--no-mmap-callstacks` | 关闭项 | 只运行无栈 mmap 事件健康检查。 |
| `--no-ftrace` | 关闭项 | 不启用 ftrace syscall 采集。 |
| `--no-kernel-frames` | 关闭项 | perf 调用栈不采内核帧。 |
| `--no-guardrails` | 关闭项 | 传递给设备端 `perfetto --no-guardrails`。 |
| `--use-su` | 关闭项 | 用 `su 0` 读取 smaps。 |
| `--no-analyze` | 关闭项 | 不运行离线分析器。 |
| `--trace-processor` | 自动探测 | 传给分析器和验证 SQL 的 trace processor。 |
| `--traceconv` | 自动探测 | 用于符号化 trace。 |
| `--classify-config` | 空 | 传给 mmap 分析器的分类配置。 |
| `--top-n` | 空 | 传给 mmap 分析器；`0` 表示全部。 |
| `--analyzer` | `mmap_phys_analyzer.py` | 指定离线分析器路径。 |

额外环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MMAP_PHYS_ACTIVITY` | 空 | 目标未启动时使用该 Activity 拉起，格式为 `package/.Activity`。 |

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
8. 如传入 speedscope 或分类参数，同时输出火焰图和分类表。
```

参数说明：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--trace` | 必填 | 包含 mmap/perf 事件的 Perfetto trace，推荐 `symbolized-trace`。 |
| `--smaps-dir` | 必填 | smaps 快照目录。 |
| `--pid` | 必填 | 目标进程 pid。 |
| `--output` | 必填 | 输出 Chrome JSON trace。 |
| `--speedscope-output` | 空 | 额外输出 speedscope JSON。 |
| `--classify-config` | 空 | 使用 `fs.ini` 规则分类 mmap 调用栈。 |
| `--classify-summary-out` | 自动路径 | 分类统计 XLSX 输出路径。 |
| `--classify-summary-speedscope-out` | 自动路径 | 分类汇总 speedscope 输出路径。 |
| `--classify-speedscope-dir` | 空 | 每个分类单独输出 speedscope 的目录。 |
| `--trace-processor` | 自动探测 | trace processor 路径。 |
| `--smaps-ts-unit` | `auto` | smaps 文件名时间戳单位，可选 `auto/ns/us/ms/s`。 |
| `--smaps-ts-offset-ns` | `0` | smaps 时间戳到 trace 时间轴的偏移。 |
| `--stack-window-ns` | `5000000` | mmap enter 与 perf sample 匹配窗口。 |
| `--top-n` | `50` | 每个快照输出 PSS 最大的 N 个调用栈，`0` 表示全部。 |

输出验收：

```text
mmap_phys_attribution.json
  -> Perfetto UI 可加载；metadata.final_summary 中 pss_bytes 是主指标。

mmap_phys_attribution.speedscope.json
  -> Speedscope 可加载；权重单位 bytes，默认按 PSS。
```

## `run_mmap_phys_analyze_latest.sh`

用途：离线分析最近一次 mmap 采集目录，适合只重跑分类、top-n 或输出格式。

默认行为：

```text
1. 在 PerfData/mmap_phys 下寻找最近一个包含 trace 和 smaps 的目录。
2. 优先使用 symbolized-trace，没有则回退 mmap_trace.perfetto-trace。
3. 未传 --pid 时，按 MMAP_PHYS_APP 从 trace 中自动查询 pid。
4. 默认输出 mmap_phys_attribution.json 和 mmap_phys_attribution.speedscope.json。
5. 默认追加 --classify-config heap_analyzer/fs.ini --classify-speedscope-dir mmap_categories --top-n 0。
6. 用户参数追加在默认参数之后，可覆盖本次分析口径。
```

命令：

```bash
./run_mmap_phys_analyze_latest.sh
MMAP_PHYS_APP=com.example.app ./run_mmap_phys_analyze_latest.sh
./run_mmap_phys_analyze_latest.sh --pid 1234 --top-n 25
```

参数说明：参数原样透传给 `mmap_phys_analyzer.py`。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--pid` | 自动查询 | 目标 pid。 |
| `--trace` | 最近目录 trace | 指定 trace。 |
| `--smaps-dir` | 最近目录 smaps | 指定 smaps。 |
| `--top-n` | `0` | 输出调用栈数量。 |
| `--classify-config` | `heap_analyzer/fs.ini` | 分类配置。 |
| `--trace-processor` | 自动探测 | trace processor。 |
| `-h, --help` | 无 | 打印用法。 |

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MMAP_PHYS_DATA_DIR` | `PerfData/mmap_phys` | 最近采集目录搜索根目录。 |
| `MMAP_PHYS_APP` | `com.tencent.dhwdxkty.trunk.profiler` | 自动查询 pid 时使用的进程名。 |
| `TRACE_PROCESSOR` | 自动探测 | 覆盖 trace processor。 |

## `run_heap_profile.sh`

用途：Native heap profile 采集入口，包装 `run_heap_profile.py`。

默认行为：

```text
1. 自动选择 Python。
2. 调用 run_heap_profile.py。
3. 目标包名固定为 com.fs.t.prf。
4. 不传 duration 时持续采集，直到人工 Ctrl+C；Windows 下通过新进程组和 Ctrl-Break bridge 触发 Perfetto 的 SIGINT 收尾。
5. 不传 interval 时使用 1024 bytes。
6. 不传 shmem-size 时使用 8388608 bytes。
```

命令：

```bash
./run_heap_profile.sh
./run_heap_profile.sh 45000
./run_heap_profile.sh 45000 1024
./run_heap_profile.sh 45000 1024 67108864
```

参数说明：

| 位置参数 | 默认值 | 说明 |
| --- | --- | --- |
| `1: duration_ms` | 空 | 采集时长，单位 ms；空表示人工停止。AI 验证必须传 `45000`。 |
| `2: interval_bytes` | `1024` | heapprofd 采样间隔，单位 bytes。 |
| `3: shmem_size` | `8388608` | heapprofd 共享缓冲区大小，单位 bytes。 |

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RUN_HEAP_PROFILE_PYTHON` | 自动探测 | wrapper 使用的 Python。 |
| `RUN_HEAP_PROFILE_INNER_PYTHON` | 当前 Python | 调用 Perfetto `heap_profile.py` 的 Python。 |
| `RUN_HEAP_PROFILE_EXTRA_PATH` | 空 | 追加到 PATH 前面的路径。 |
| `ADB_BINARY` | `adb` | adb 可执行文件。 |
| `CP_BINARY` | `cp` | 复制命令。 |
| `TRACE_PROCESSOR` | 自动探测 | trace processor。 |
| `TRACECONV` | 自动探测 | traceconv。 |
| `HEAP_PROFILE_ACTIVE_TIMEOUT_S` | `60` | 等待 `Profiling active` 的超时。 |
| `HEAP_PROFILE_SHUTDOWN_SIGNAL_TIMEOUT_S` | `600` | 等待 profiler shutdown 的超时。 |
| `HEAP_PROFILE_MEMINFO_ALLOWED_DIFF_BYTES` | `67108864` | malloc live 和 meminfo Native Heap Alloc 允许差值。 |

Windows 下脚本会把 `PerfettoRoot/buildtools/win/clang/bin` 加入 `PATH`，确保 `traceconv.exe` 可以启动 `llvm-symbolizer.exe` 完成符号化。

输出：

```text
PerfData/mem/<时间戳>/
  heap_profile.log
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
1. 读取 config.sh 中的 PerfettoRoot。
2. 设置符号化和 PYTHONPATH 环境。
3. 自动选择 traceconv 和 trace_processor。
4. 推送 FSBootCmdLine.cfg 和 debugconfig.txt。
5. force-stop 目标 App。
6. 启动 Perfetto heap_profile.py，并等待 Profiling active。
7. 使用 adb shell monkey -p <package> 1 只拉起目标 App。
8. 等待 profiler shutdown 后保存 meminfo。
9. 查询 heap_profile_allocation 累计 live bytes。
10. 与 dumpsys meminfo Native Heap Alloc 做 64 MiB 阈值验证。
```

参数说明：

| 位置参数 | 默认值 | 说明 |
| --- | --- | --- |
| `1: duration_ms` | 空 | 传给 Perfetto `heap_profile.py -d`。 |
| `2: interval_bytes` | `1024` | 传给 `heap_profile.py -i`。 |
| `3: shmem_size` | `8388608` | 传给 `heap_profile.py --shmem-size`。 |

环境变量同 `run_heap_profile.sh`。

注意：这里的 `adb monkey -p <package> 1` 只用于发送启动 Intent，不用于随机触发测试场景。

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

| 位置参数 | 默认值 | 说明 |
| --- | --- | --- |
| `1: duration_ms` | `45000` | 每轮 heapprofd 采集时长。 |
| `2: shmem_size` | `268435456` | heapprofd shmem 大小。 |
| `3...: intervals` | `512 256 128 64 32 16` | 待评估的采样间隔列表。 |

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HEAP_STARTUP_APP` | `com.tencent.dhwdxkty.trunk.profiler` | 目标包名。 |
| `HEAP_STARTUP_ACTIVITY` | `com.tencent.dhwdxkty.trunk.profiler/com.dhplugin.unity.MainActivity` | 启动 Activity。 |
| `HEAP_STARTUP_PATTERN` | `LAN 更新流程开始` | 业务启动完成日志。 |
| `HEAP_STARTUP_WAIT_TIMEOUT_S` | `90` | 等待目标日志超时。 |
| `HEAP_STARTUP_DRY_RUN` | `0` | 为 `1` 时只打印配置，不执行采集。 |
| `TRACE_PROCESSOR` | 自动探测 | trace processor。 |
| `TRACECONV` | 自动探测 | traceconv。 |

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

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--trace` | 最近 trace | 指定 trace。 |
| `--symbol` | 不默认强塞 | 按符号筛选调用栈。 |
| `--limit` | `0` | wrapper 默认不打印长明细。 |
| `--classify-config` | `heap_analyzer/fs.ini` | 分类配置。 |
| `-h, --help` | 无 | 打印用法。 |

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HEAP_PROFILE_DATA_DIR` | `PerfData/mem` | 最近 trace 搜索目录。 |
| `PYTHON` | 自动探测 | 指定 Python。 |

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

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--trace` | 内置历史路径 | symbolized trace 路径；实际使用建议显式指定或通过 wrapper。 |
| `--symbol` | `il2cpp::vm::Class::Init` | 调用栈匹配符号子串；配合 `--all-allocations` 时忽略。 |
| `--all-allocations` | 关闭 | 不按符号过滤，分析全部分配栈。 |
| `--trace-processor` | 内置默认路径 | trace processor 可执行文件。 |
| `--limit` | `50` | 终端输出分配栈数量，`0` 表示只输出摘要和文件。 |
| `--speedscope-out` | 空 | 输出 speedscope JSON；相对路径写入 trace 同级 `heap_analyze/`。 |
| `--pprof-out` | 空 | 输出 pprof；不带路径时写入 `heap_analyze/native_heap.pprof.pb.gz`。 |
| `--speedscope-weight` | `positive-net` | `positive-net` 只看正向净分配；`absolute-net` 看净变化绝对值。 |
| `--classify-config` | 空 | 按 fs.ini 对分配栈分类。 |
| `--classify-speedscope-dir` | 空 | 每分类输出 speedscope 的目录。 |
| `--classify-summary-out` | 自动路径 | 分类统计 XLSX 输出路径。 |
| `--classify-summary-speedscope-out` | 自动路径 | 分类汇总 speedscope 输出路径。 |

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

验收要点：`net_alloc_bytes` 是带符号净变化，可能为负；看最终净增长用带符号值，看变化规模用 `--speedscope-weight absolute-net`。

## `heap_analyzer/classification.py`

用途：fs.ini 调用栈分类共用库，被 Native heap 和 mmap 分类输出复用。

默认行为：

```text
1. 解析 fs.ini。
2. 规则按文件顺序匹配。
3. 一条调用栈命中一个分类后，不再进入后续分类。
4. 未命中的调用栈进入 remaining。
5. 分类名中的 / 会展开为层级，父节点聚合子分类。
6. 可写出不依赖 openpyxl 的 xlsx 文件。
```

参数说明：该文件不是命令行工具，没有 CLI 参数。主要函数参数如下。

| 函数 | 参数 | 说明 |
| --- | --- | --- |
| `parse_classification_config(path)` | `path` | 读取 fs.ini 分类配置。 |
| `classify_items(items, rules, stack_getter)` | `items/rules/stack_getter` | 按规则顺序分类。 |
| `build_hierarchy_entries(classified, remaining)` | 分类结果 | 生成带父子层级的分类节点。 |
| `write_xlsx(path, sheets)` | 输出路径和 sheet 数据 | 写出 xlsx。 |

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

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TOTAL_BYTES` | `1073741824` | demo 计划持有的 malloc 总量。 |
| `START_DELAY_SECONDS` | `10` | 启动后延迟分配时间。 |
| `ALLOC_SECONDS` | `60` | 分配持续时间。 |
| `HOLD_SECONDS` | `20` | 分配完成后保持 live 的时间。 |
| `DURATION_MS` | `(START_DELAY_SECONDS + ALLOC_SECONDS + 5) * 1000` | Perfetto 采集时长。 |
| `MALLOC_SAMPLING_INTERVAL_BYTES` | `4096` | heapprofd 采样间隔。 |
| `MALLOC_SHMEM_SIZE_BYTES` | `268435456` | heapprofd shmem 大小。 |
| `TRACE_PROCESSOR` | 自动探测 | trace processor。 |
| `ANDROID_SDK_ROOT` | 自动探测 | Android SDK。 |
| `ANDROID_NDK_ROOT` | 自动探测 | Android NDK。 |
| `ANDROID_BUILD_TOOLS` | 自动探测 | build-tools 目录。 |
| `ANDROID_JAR` | 自动探测 | android.jar。 |
| `UNITY_ANDROID_ROOT` | Unity 2022.3.62 AndroidPlayer | Windows 默认工具链根目录。 |

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

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ANDROID_SDK_ROOT` | 自动探测 | Android SDK。 |
| `ANDROID_NDK_ROOT` | 自动探测 | Android NDK。 |
| `ANDROID_BUILD_TOOLS` | 自动探测 | build-tools。 |
| `ANDROID_JAR` | 自动探测 | android.jar。 |
| `UNITY_ANDROID_ROOT` | Unity 2022.3.62 AndroidPlayer | Windows 默认工具链根目录。 |
| `PYTHON` | 自动探测 | 校验脚本使用的 Python。 |

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

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ANDROID_SDK_ROOT` | 自动探测 | Android SDK。 |
| `ANDROID_NDK_ROOT` | 自动探测 | Android NDK。 |
| `ANDROID_BUILD_TOOLS` | 自动探测 | build-tools。 |
| `ANDROID_JAR` | 自动探测 | android.jar。 |
| `UNITY_ANDROID_ROOT` | Unity 2022.3.62 AndroidPlayer | Windows 默认工具链根目录。 |

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

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--baseline` | 必填 | baseline `dumpsys meminfo` 文本。 |
| `--after` | 必填 | after `dumpsys meminfo` 文本。 |

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

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `phonePath` | `/data/local/tmp` | 设备端目录，脚本内固定赋值。 |
| `cfgName` | `FSBootCmdLine.cfg` | 配置文件名，脚本内固定赋值。 |

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

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `$@` | 无 | 原样传给 `go tool pprof -http=0.0.0.0:8001`。 |

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

| 函数 | 参数 | 默认行为 |
| --- | --- | --- |
| `select_python` | 无 | 依次找 `PYTHON`、`python3`、`python`、`py`。 |
| `select_perfetto_tool tool perfetto_root override` | 工具名、根目录、覆盖路径 | 根据 Windows/Linux 优先级选择 trace processor 或 traceconv。 |
| `select_android_sdk_root` | 无 | 优先环境变量，再回退 Unity AndroidPlayer/SDK。 |
| `select_android_ndk_root` | 无 | 优先环境变量，再回退 Unity AndroidPlayer/NDK。 |
| `select_build_tools_dir sdk_root` | SDK 根目录 | 优先 `ANDROID_BUILD_TOOLS`，再找 build-tools。 |
| `select_android_jar sdk_root` | SDK 根目录 | 优先 `ANDROID_JAR`，再找 android.jar。 |
| `run_host_tool tool args...` | 工具和参数 | Windows Git Bash 下处理 `.cmd/.bat` 与路径转换。 |

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
5. 未匹配项进入 remaining。
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

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MMAP_PHYS_TEST_OUTPUT` | 临时目录 | 保留测试生成的 Perfetto JSON。 |
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

默认行为：验证默认 duration、shmem、intervals、目标 App 和日志 pattern。

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
./run_heap_profile.sh 45000
```
