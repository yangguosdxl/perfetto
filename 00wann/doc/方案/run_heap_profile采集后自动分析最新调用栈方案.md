# run_heap_profile 采集后自动分析最新调用栈方案

## 现状

`run_heap_profile.sh` 当前使用：

```bash
exec "$script_dir/run_device_test.sh" malloc -- "$@"
```

`exec` 会让 `run_device_test.sh` 替换当前 Shell 进程，因此脚本后面无法再执行
`run_heap_alloc_stacks_by_symbol_latest.sh`。

`run_heap_alloc_stacks_by_symbol_latest.sh` 已能自动选择 `PerfData/mem` 下最近一次 Native heap
trace，优先使用 `symbolized-trace`，并默认执行 `fs.ini` 全量分类。因此 wrapper 无需解析本轮
输出目录，也不应把采集参数继续传给分析脚本。

## 修改方案

修改 `run_heap_profile.sh` 的收尾控制流：

```text
run_device_test.sh malloc
  ├─ 失败：输出 SKIP，保留采集退出码，不分析旧 trace
  └─ 成功：运行 run_heap_alloc_stacks_by_symbol_latest.sh
               ├─ 成功：整轮成功
               └─ 失败：输出 FAIL，并以分析退出码结束
```

具体规则：

1. 去掉 `run_device_test.sh` 前的 `exec`，等待采集入口返回。
2. 采集失败时不执行最新 trace 分析，避免把上一次数据误认为本轮结果。
3. 采集成功后调用同目录的 `run_heap_alloc_stacks_by_symbol_latest.sh`，不传参数，让它使用默认
   的最新 trace 和全量分类行为。
4. 增加 `HEAP_PROFILE_POST_ANALYSIS=START/PASS/FAIL/SKIP` 结构化输出，记录执行阶段和退出码。
5. 分析失败时让 `run_heap_profile.sh` 返回失败，避免采集成功但自动分析失败仍误报整轮成功。

## 测试调整

更新 `test_run_heap_profile.sh`：

- 在临时夹具中创建假的 `run_heap_alloc_stacks_by_symbol_latest.sh`；
- 验证采集成功后会调用分析脚本；
- 验证调用发生在平台清理完成之后；
- 验证采集失败时跳过分析并保留采集失败码；
- 验证分析脚本失败时 wrapper 返回分析失败码并输出明确错误。

保留并运行独立分析 wrapper 的现有测试
`test_run_heap_alloc_stacks_by_symbol_latest.sh`，确认最新 trace 选择和默认参数没有回归。

## 文档同步

同步更新：

- `README.md` 的 `run_heap_profile.sh` 默认行为；
- `docs/heap_profile.md` 的采集收尾、自动分析和验证说明。

文档明确：自动分析只在采集与统一框架清理成功后执行，采集参数不会传给查询脚本。

## 验证方案

自动验证：

```bash
bash -n run_heap_profile.sh test_run_heap_profile.sh
bash test_run_heap_profile.sh
bash test_run_heap_alloc_stacks_by_symbol_latest.sh
```

修改涉及 Native heap 调用栈自动分析，按项目规则不跑无栈 mmap 验证，改用 Native heap 主功能
真机验证：

```bash
./run_heap_profile.sh
```

真机只使用 `1C111FDF600AW5`，不传 duration 或固定时长，不使用 monkey。等待 logcat 依次出现
`登录场景完成`、`RegistForGameStart.LoadOtherTable.End`，确认登录后 GM RPC 成功并继续稳定采集
120 秒，让脚本自动完成采集收尾和最新调用栈分析。失败或告警必须保留产物并分析根因。

## 待确认

请确认以下语义：

1. 只在采集和框架清理成功后执行最新 trace 分析。
2. 自动分析失败会让 `run_heap_profile.sh` 返回失败。
3. 自动分析使用自身默认参数，不复用 `run_heap_profile.sh` 的 interval 和 shmem 参数。

## 执行计划

- [x] 1. 核对两个 wrapper、现有测试夹具和相关文档，确认退出码与参数边界。
- [x] 2. 修改 `run_heap_profile.sh`，在采集成功后执行最新调用栈分析。
- [x] 3. 更新 `test_run_heap_profile.sh`，覆盖成功、跳过和分析失败语义。
- [x] 4. 同步更新 `README.md` 和 `docs/heap_profile.md`。
- [x] 5. 运行 Bash 语法检查和两个 Shell 集成测试。
- [x] 6. 使用指定真机运行不带参数的 Native heap 主功能，检查自动分析结果。

## 验证结果

### 自动测试

- `bash -n run_heap_profile.sh test_run_heap_profile.sh`：通过。
- `bash test_run_heap_profile.sh`：通过，覆盖采集成功后分析、采集失败跳过分析、分析失败码
  传播，以及分析发生在平台设置恢复之后。
- `bash test_run_heap_alloc_stacks_by_symbol_latest.sh`：通过。

测试期间发现两个既有夹具漂移并按当前实现修正：

- GM 参数断言改为读取动作模块当前 `GM_COMMAND`，并显式以 UTF-8 输出，避免 Windows Python
  控制台编码干扰；
- 最新 trace wrapper 测试补充复制 `common_tools.sh` 当前依赖的 `config.sh`。

### 真机主功能

指定设备 `1C111FDF600AW5` 不带参数运行 `./run_heap_profile.sh`：

- `登录场景完成`、`RegistForGameStart.LoadOtherTable.End`、GM RPC PASS 和稳定采集 120 秒均完成；
- Perfetto `health_sum=0`、`heap_dump_count=1`；
- `malloc_live_bytes=1365186846`，`meminfo_native_heap_alloc_bytes=1558148096`，差值
  `192961250` bytes，超过 `67108864` bytes 阈值，主功能按既有规则失败；
- wrapper 正确输出
  `HEAP_PROFILE_POST_ANALYSIS=SKIP|reason=profile_failed|rc=1`，没有误分析旧 trace。

历史多次 interval 1024 采集也存在约 114 MB 至 193 MB 的同类差值，说明该失败不是本次 wrapper
修改引入。依据 Perfetto `src/profiling/memory/sampler.h`，小于 sampling interval 的分配使用
泊松采样估算，大于等于 interval 的分配记录真实大小。尝试 interval 128、shmem 256 MiB 后，
heapprofd CPU 约 148%、unwind 线程约 97%，目标 App 被 SIGKILL，因此该参数不可用于验收，未再
继续提高采样压力。

### 真实调用栈分析

对完整业务采集的 `PerfData/mem/2026-08-17_17-55-18/symbolized-trace` 显式运行
`run_heap_alloc_stacks_by_symbol_latest.sh` 成功：

- `matched_allocation_callsites=108062`；
- `net_alloc_bytes=1365186846`；
- 生成 `heap_analyze/native_heap.pprof.pb.gz`、分类 pprof、`summary.xlsx` 和
  `summary.speedscope.json`。

自动成功分支由 Shell 集成测试验证；真实主功能因既有 malloc live 与 meminfo 口径校验失败，
只能验证自动跳过分支，不能把百 MB 差异通过放宽阈值伪装成成功。
