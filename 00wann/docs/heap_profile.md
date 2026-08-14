# Native heap profile 采集脚本

`run_heap_profile.sh` 用于启动 Perfetto Native heap profile 采集，目标包名统一读取同目录 `config.sh` 中的 `MMAP_PHYS_APP`，采集结果保存到 `00wann/PerfData/mem/<日期时间>/`。它现在是通用真机测试框架的兼容入口：`run_device_test.sh` 通过 `device_test_framework` 子模块执行配置、Android 连接、运行级清理和统一报告，`device_test_plugins` 中的 malloc 插件再调用 `run_heap_profile.py` 执行专业采集时序。入口会自动切换到自身所在目录并加载配置，因此可以从仓库根目录执行 `00wann/run_heap_profile.sh`，也可以在 `00wann` 目录内执行 `./run_heap_profile.sh`。

通用配置、插件和报告结构见 `docs/device_test_framework.md`。每轮除原有 Native heap
产物外还会生成 `run_config.json`、`run_manifest.json`、`run_summary.txt` 和
`report.md`；专业后端的非零退出码和框架清理阶段都会记录在统一报告中。

入口在采集前保存设备的 `global.hide_error_dialogs` 原值并临时设为 `1`，避免
高开销采样期间的“应用未响应”对话框反复改变窗口焦点。正常、失败和中断退出
都会恢复原值。该设置只隐藏对话框，不修改 Android 的 ANR 阈值，系统仍会记录 ANR。

默认执行时不向 Perfetto 传固定 duration。脚本会在 heapprofd 就绪后启动目标 App，并依次等待 logcat 出现 `登录场景完成` 和 `RegistForGameStart.LoadOtherTable.End`；随后加载 `config.sh` 配置的 Python 测试模块。测试模块协程完成或模块声明的最长等待时间到期后，脚本请求 `heap_profile.py` 进入 `Waiting for profiler shutdown...` 收尾流程：

```bash
00wann/run_heap_profile.sh
```

人工按 Ctrl+C 时，Python 主脚本也会请求 `heap_profile.py` 停止采集。Linux 下直接转发 `SIGINT`；Windows 下 `subprocess` 不支持对子进程发送 `SIGINT`，脚本会用新进程组和 Ctrl-Break bridge 把控制台事件转换为 `heap_profile.py` 内部的 `SIGINT` 处理。主脚本不会直接 130 退出；它会继续等待 `heap_profile.py` 把 `raw-trace`、`symbolized-trace` 和 `heap_dump.*.pb` 或 `heap_dump.*.pb.gz` 拉回本地并完成处理，然后保存 `heap_profile.log`、抓取 `dumpsys meminfo`，并执行后续 malloc live 与 `Native Heap Alloc` 验证。

如需指定采样 interval，可把 interval 作为第一个参数传入，单位为 bytes。不传时脚本当前使用 1024。2026-08-11 的真实数据中，1024 的启动到登录耗时为 113.485 秒，`diff_bytes=114905353`，因此需要在保持 64 MiB 验收阈值的同时继续调试更低开销的采样间隔：

```bash
00wann/run_heap_profile.sh 1024
```

如需指定 heapprofd 共享缓冲区大小，可把 `shmem-size` 作为第二个参数传入，单位为 bytes。该值必须是 4096 的 2 的幂倍数且至少 8192：

```bash
00wann/run_heap_profile.sh 1024 67108864
```

## 启动目标应用

入口首先加载：

```bash
source 00wann/config.sh
```

`MMAP_PHYS_APP` 缺失时脚本会输出 `app_config_missing` 并在操作真机前退出。

为了让 heapprofd 的 malloc live 总量和采集后 `dumpsys meminfo` 的 `Native Heap / Heap Alloc` 具备同口径可比性，脚本会在采集前重启目标应用：

```bash
adb shell am force-stop "$MMAP_PHYS_APP"
```

随后脚本以 `--no-running` 启动 `heap_profile.py`，等待日志出现 `Profiling active`，再执行一次：

```bash
adb shell am start -n "$MMAP_PHYS_APP/com.dhplugin.unity.MainActivity"
```

该命令只用于拉起固定 Activity，不使用 `adb monkey` 随机触发事件。需要测试数据或目标场景交互时，仍应在手机上手动操作。

这个顺序保证目标进程启动后的 native malloc/free 会被 heapprofd 观察到；如果附加到已经运行很久的进程，heapprofd 无法还原采集开始前已经发生的 native 分配，`malloc_live_bytes` 会明显低于 `meminfo Native Heap Alloc`。

## 关键配置与登录后测试模块

每轮开始时，脚本输出 `HEAP_PROFILE_CONFIG` 和 `HEAP_PROFILE_TOOLS`，内容包括目标 App、Activity、设备序列号、采样间隔、共享缓冲区、Perfetto trace 缓冲区、测试模块路径、RPC 端口、Perfetto 工具和符号目录。同样的信息保存到：

```text
PerfData/mem/<日期时间>/heap_profile_config.txt
PerfData/mem/<日期时间>/run_summary.txt
```

登录完成后，脚本先等待延迟表加载完成日志：

```text
RegistForGameStart.LoadOtherTable.End
```

这个门槛用于避免测试操作在 `MatchAiImageConfig` 等配置尚未加载时开始。采集器随后加载：

```bash
PERF_PROFILE_ACTION_SCRIPT=profile_actions/send_battle_record_gm.py
```

相对路径以 `00wann` 为基准。模块必须实现：

```python
def get_collection_wait_seconds(session) -> float | None:
  """返回最长采集秒数；None 表示不设置超时。"""


async def run_profile_action(session) -> None:
  """执行测试操作；协程结束时结束采集。"""
```

采集器先调用 `get_collection_wait_seconds()`，再启动 `run_profile_action()`。等待时间先到时会取消协程并等待其 `finally` 清理完成，本轮正常结束；协程先完成时提前结束；协程抛异常或 App 原 PID 死亡时保存 trace 但本轮失败；`None` 表示只等待协程、人工中断或 App 死亡。

默认模块 `profile_actions/send_battle_record_gm.py` 返回 120 秒，并从本轮 `logcat.txt` 中查找目标 PID 的：

```text
Tcp server started and listening at <5001..5005>
```

随后把本机固定端口 `12346` 转发到实际手机端口，并按 Poco 的“4 字节小端正文长度 + UTF-8 JSON”协议调用：

```json
{
  "jsonrpc": "2.0",
  "method": "DoRecordCheat",
  "params": ["CheatFunc_BatchCheatOption.战斗录像:@40011@@|"],
  "id": 1
}
```

公共 Session 默认要求响应 `result=true` 且没有 `error`，成功时输出 `PROFILE_ACTION_RPC=PASS`。默认模块当前没有战斗完成信号，因此 RPC 成功后通过 `session.wait_forever()` 保持协程挂起，由 120 秒最长等待触发取消。RPC 失败时公共接口返回 `success=False` 并保存诊断，默认脚本选择抛异常使本轮失败。本轮 RPC 详情默认保存到 `gm_rpc.txt`；公共输出保存到 `profile_action.log` 并同步追加到 `run_summary.txt`。

执行器为每轮测试创建一次 `ProfileActionSession`。公共 API 和协程 runner 位于
`device_test_framework/actions/` 子模块，具体测试目的位于独立 `profile_actions/`
子模块。测试模块直接调用 Session 方法：

```python
async def run_profile_action(session):
  rpc = await session.invoke_rpc(
    "DoRecordCheat",
    ["CheatFunc_BatchCheatOption.战斗录像:@40011@@|"],
  )
  if not rpc.success:
    raise RuntimeError(rpc.error)

  completed = await session.wait_for_app_log(
      "战斗录像播放完成", timeout_seconds=90)
  if not completed.success:
    raise RuntimeError(completed.error)
```

`session.invoke_rpc()` 首次调用时只扫描一次 Poco 端口并建立本轮复用的转发，执行器在所有结束路径统一清理。默认 `expected_result=True`、`detail_file="gm_rpc.txt"`；只检查协议时传 `check_result=False`。ADB、端口发现、转发、协议和日志记录等运行失败均返回 `success=False`，是否抛异常由测试脚本决定。

`session.run_adb()` 自动选择配置设备并返回 `CommandResult`。`session.wait_for_app_log()` 的字符串按字面量匹配，编译后的 `re.Pattern` 按正则匹配；只匹配本轮 App PID，默认只检查调用后新增日志。日志等待超时返回失败结果，协程取消仍直接传播。

Python 主脚本会导出 `PYTHONPATH="$PerfettoRoot/python"`，确保直接执行 `python/tools/heap_profile.py` 时可以导入仓库内的 `perfetto` Python 包。

Python 主脚本同时导出 `PYTHONUNBUFFERED=1`，避免 `heap_profile.py` 的 `Profiling active` 输出因为管道缓冲而延迟。profiler 原始日志会在采集结束后保存到：

```text
PerfData/mem/<日期时间>/heap_profile.log
```

Python 主脚本还会显式传入本地构建产物：

```text
Linux 优先：
  --traceconv-binary "$PerfettoRoot/out/linux_clang_release/traceconv"
  --trace-processor-binary "$PerfettoRoot/out/linux_clang_release/trace_processor_shell"

Windows Git Bash 优先：
  --traceconv-binary "$PerfettoRoot/out/win_clang/traceconv.exe"
  --traceconv-binary "$PerfettoRoot/out/android_arm64/msvc/traceconv.exe"
  --trace-processor-binary "$PerfettoRoot/out/win_clang/trace_processor_shell.exe"
  --trace-processor-binary "$PerfettoRoot/out/win/trace_processor_shell.exe"
```

实际路径由 `common_tools.sh` 和 `run_heap_profile.py` 自动探测；也可以用
`TRACECONV`、`TRACE_PROCESSOR` 覆盖。这样可以避免 `heap_profile.py`
下载或复用 `~/.local/share/perfetto/prebuilts` 中与当前宿主机不兼容的预构建二进制。
Windows 下脚本还会把 `PerfettoRoot/buildtools/win/clang/bin` 加入 `PATH`，
确保 `traceconv.exe` 符号化时可以找到 `llvm-symbolizer.exe`。

符号化路径由 `PERFETTO_BINARY_PATH` 控制。若外部已设置该变量，脚本会原样保留，便于临时指定一组完整符号目录。未设置时，脚本优先使用当前 FS 打包产物符号目录：

```text
D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\Published\Android\DreamRivakes2.apk\unityLibrary\symbols\arm64-v8a
```

如果需要分析其它包或临时产物，可以设置 `RUN_HEAP_PROFILE_SYMBOLS_DIR=<符号目录>` 覆盖 FS 打包产物目录。`00wann/workspace/allsymbols/arm64-v8a` 仍会作为补充目录追加，用于解析该目录中独有的 `libBattleLogic.so`、`libprotobuf.so` 等符号。

Windows Git Bash 中如果没有 `python3`，入口会回退到 `python` 或 `py`；
测试和手工运行也可以用 `PYTHON=python` 或 `RUN_HEAP_PROFILE_PYTHON=python`
显式指定解释器。

AI 做真机验证时不要传入 duration 参数。采集结束必须由目标 App logcat 输出 `登录场景完成`、延迟表就绪并执行配置测试模块后触发；默认 GM 模块会等待 120 秒。下探采样 interval 时使用 `00wann/run_heap_profile.sh <interval_bytes>`；对比缓冲区时使用 `00wann/run_heap_profile.sh <interval_bytes> <shmem_size>`。历史命令中的 `45000` 只做兼容忽略，不再作为推荐用法。

默认不限制等待登录场景的时间。只有显式设置 `HEAP_PROFILE_LOGIN_TIMEOUT_S=<秒>` 时，脚本才会在未等到 `登录场景完成` 时超时退出；真机验收不要设置这个变量。`HEAP_PROFILE_GM_READY_TIMEOUT_S` 默认是 180，用于等待延迟表加载完成。`HEAP_PROFILE_RPC_LOCAL_PORT` 默认是 12346，`HEAP_PROFILE_RPC_TIMEOUT_S` 默认是 10。`HEAP_PROFILE_LOGIN_STABLE_S` 只由默认 GM 测试模块作为兼容覆盖读取，默认 120；新测试模块应在自身的 `get_collection_wait_seconds()` 中直接声明等待时间。

## 2026-08-11 三轮采样间隔结果

三轮均固定 `shmem_size_bytes=8388608`，使用同一手机 `1C111FDF600AW5`，并完整执行登录、延迟表就绪、GM RPC 和 120 秒稳定采集：

| interval | 登录耗时 | 登录后表就绪 | malloc live | Native Heap Alloc | diff | health | heap dump | 目录 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 2048 | 81.031 秒 | 24.235 秒 | 1478145382 | 1660163072 | 182017690 | 0 | 1 | `2026-08-11_21-12-05` |
| 4096 | 46.156 秒 | 17.563 秒 | 1476392274 | 1659099136 | 182706862 | 0 | 1 | `2026-08-11_21-24-05` |
| 1536 | 85.734 秒 | 28.203 秒 | 1478808228 | 1658610688 | 179802460 | 0 | 1 | `2026-08-11_21-28-04` |

`4096` 的启动开销最低，但三轮 `diff_bytes` 都约为 171.5 至 174.2 MiB，未达到 64 MiB 阈值，且没有随采样间隔单调变化。三轮 `health_sum=0`、`heap_dump_count=1`，因此当前证据更符合 `Native Heap Alloc` 含有 heapprofd 未覆盖的固定来源，而不是采样丢包或 interval 过粗。暂不修改默认 `1024`，也不放宽 64 MiB 阈值；后续应继续归因约 180 MB 的固定口径差异。

`4096` 首次尝试目录 `2026-08-11_21-18-11` 只有约 14 KB trace 和零 heap dump，App 日志出现 heapprofd 挂钩状态竞态。确认设备侧没有残留 heapprofd 后重跑成功，所以该目录只保留作故障证据，不计入三轮结果。

## 验证

每次 `heap_profile.py` 输出 `Waiting for profiler shutdown...` 后，脚本会在 host 侧 trace 转换、符号化和 pprof 生成完成前立刻执行：

```bash
adb shell dumpsys meminfo "$MMAP_PHYS_APP"
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

验证通过时输出 `HEAP_MEMINFO_VALIDATION=PASS`。如果不相当，脚本输出 `HEAP_MEMINFO_VALIDATION=FAIL` 并返回失败。百 MB 级差异不能通过百分比阈值放行，必须继续定位是否存在 heapprofd 丢包、trace 缺失、并发 profiling 残留导致的 `heapprofd_rejected_concurrent`、采样间隔过粗、启动前分配未覆盖、meminfo 抓取晚于采集窗口，或 `Native Heap Alloc` 中存在 heapprofd 未统计来源等根因。报告中会保留 `health_sum`、`heap_dump_count`、trace 路径和 meminfo 路径。

修改 `run_heap_profile.sh` 后可运行：

```bash
bash 00wann/test_run_heap_profile.sh
bash -n 00wann/run_heap_profile.sh 00wann/test_run_heap_profile.sh
python -m py_compile 00wann/run_heap_profile.py
```

测试会用假的 `adb` 模拟“第一次 `pidof` 为空、第二次返回 PID”的状态，验证脚本在应用未启动时会先拉起应用，并且随后继续执行 Native heap profile 采集。测试中的本地 RPC 服务会校验 Poco 小端长度头、`DoRecordCheat` 方法和完整 GM 参数，也会覆盖 RPC 失败后的 Perfetto 正常收尾。Linux 环境会模拟人工 Ctrl+C，确认中断会传递给 `heap_profile.py`，并在 `heap_profile.py` 完成 trace/heap dump 本地收尾后继续完成 meminfo 抓取和 SQL 验证；Windows 环境会额外验证 Ctrl-Break bridge 能触发 `heap_profile.py` 的 `SIGINT` 收尾逻辑。

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
HEAP_DUMP_COUNT -> heap_dump.*.pb 或 heap_dump.*.pb.gz 文件数量
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
