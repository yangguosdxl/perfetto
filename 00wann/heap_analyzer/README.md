# Perfetto Native Heap SQL 查询说明

本目录保存用于分析 Perfetto heap profile trace 的 SQL 查询脚本。当前脚本主要用于在命令行里查询“调用栈中包含某个函数符号”的 Native heap 分配栈和分配量。

## 文件列表

| 文件 | 作用 |
| --- | --- |
| `query_heap_alloc_stacks_by_symbol.py` | 推荐入口。用 `trace_processor query` 导出基础表，在 Python 内存中建调用栈图并汇总分配，避免 SQL 递归超时。 |
| `heap_alloc_stacks_by_symbol.sql` | SQL 语义参考。完整递归展开调用栈，在大 trace 上可能超时。 |

## 使用方式

推荐在 `/home/dianhun/disk2/work/fsprofiler` 目录执行优化脚本：

```bash
python3 -B heap_analyzer/query_heap_alloc_stacks_by_symbol.py \
  --symbol 'il2cpp::vm::Class::Init' \
  --limit 10
```

输出较长时可以重定向到文件：

```bash
python3 -B heap_analyzer/query_heap_alloc_stacks_by_symbol.py \
  --symbol 'il2cpp::vm::Class::Init' \
  --limit 50 \
  > /tmp/heap_alloc_stacks_by_symbol.txt
```

也可以直接输出 speedscope JSON：

```bash
python3 -B heap_analyzer/query_heap_alloc_stacks_by_symbol.py \
  --symbol 'il2cpp::vm::Class::Init' \
  --limit 0 \
  --speedscope-out heap_alloc_stacks_by_symbol.speedscope.json
```

生成后可在 https://www.speedscope.app/ 中打开该 JSON 文件。`--limit 0` 表示不在终端打印长调用栈，只输出摘要和 speedscope 文件。相对输出路径会自动写入 `symbolized-trace` 同级的 `heap_analyze/` 目录。

脚本默认输出 pprof profile，可用 `go tool pprof` 同时查看火焰图、调用树、top 和调用图：

```bash
python3 -B heap_analyzer/query_heap_alloc_stacks_by_symbol.py \
  --symbol 'il2cpp::vm::Class::Init' \
  --limit 0
```

默认 pprof 输出：

```text
<symbolized-trace 所在目录>/heap_analyze/native_heap.pprof.pb.gz
```

常用查看方式：

```bash
go tool pprof -http=:0 <symbolized-trace 所在目录>/heap_analyze/native_heap.pprof.pb.gz
go tool pprof -tree <symbolized-trace 所在目录>/heap_analyze/native_heap.pprof.pb.gz
go tool pprof -sample_index=absolute_net_alloc_bytes -http=:0 <symbolized-trace 所在目录>/heap_analyze/native_heap.pprof.pb.gz
```

pprof 默认 sample 口径是 `positive_net_alloc_bytes`，也可以通过 `-sample_index=absolute_net_alloc_bytes` 查看净变化绝对值。speedscope 默认使用 `positive-net` 权重，只写入 `net_alloc_bytes > 0` 的分配栈，单位为 bytes。Native heap 的 `size` 是净变化，可能有负值；如果想把负值也按绝对值展示，可以使用：

```bash
python3 -B heap_analyzer/query_heap_alloc_stacks_by_symbol.py \
  --symbol 'il2cpp::vm::Class::Init' \
  --limit 0 \
  --speedscope-weight absolute-net \
  --speedscope-out heap_alloc_stacks_by_symbol.abs.speedscope.json
```

脚本默认 trace 路径为：

```text
/home/dianhun/disk2/work/fsprofiler/PerfData/mem/2026-06-01_18-57-13/symbolized-trace
```

如果 trace 文件在其他位置，传入 `--trace`：

```bash
python3 -B heap_analyzer/query_heap_alloc_stacks_by_symbol.py \
  --trace /path/to/symbolized-trace \
  --symbol 'il2cpp::vm::Class::Init'
```

原始 SQL 也可以直接运行，但在调用栈节点很多的 trace 上可能超时：

```bash
$PerfettoRoot/out/linux_clang_release/trace_processor_shell query \
  -f heap_analyzer/heap_alloc_stacks_by_symbol.sql \
  /home/dianhun/disk2/work/fsprofiler/PerfData/mem/2026-06-01_18-57-13/symbolized-trace
```

## 修改目标函数

优化脚本通过 `--symbol` 指定目标函数：

```bash
python3 -B heap_analyzer/query_heap_alloc_stacks_by_symbol.py --symbol 'malloc'
```

如果使用 SQL 文件，则打开 `heap_alloc_stacks_by_symbol.sql`，修改 `target(symbol)` 里的字符串：

```sql
target(symbol) AS (
  VALUES('il2cpp::vm::Class::Init')
),
```

例如要查询 `malloc`：

```sql
target(symbol) AS (
  VALUES('malloc')
),
```

SQL 使用 `GLOB '*' || symbol || '*'` 做子串匹配；优化脚本使用 Python 子串匹配。二者都可以匹配带参数、模板参数或符号化后更完整的函数名，例如：

```text
il2cpp::vm::Class::Init(Il2CppClass*)
```

## 查询结果字段

| 字段 | 含义 |
| --- | --- |
| `upid` | Perfetto 内部进程 ID。 |
| `pid` | 系统进程 ID。 |
| `process_name` | 进程名。 |
| `heap_name` | heap 名称。 |
| `callsite_id` | 分配记录关联的叶子调用栈节点。 |
| `net_alloc_count` | `SUM(count)`，净分配次数；可能为负，表示释放多于分配。 |
| `net_alloc_bytes` | `SUM(size)`，净分配字节数；可能为负。 |
| `net_alloc_mib` | `net_alloc_bytes` 换算为 MiB。 |
| `stack` | 展开的完整调用栈，优先显示 `stack_profile_symbol.name`。 |

## `net_alloc_bytes` 为负数时的统计口径

Native heap profile 中的 `heap_profile_allocation.size` 表示一段采样或快照里的净变化，不是历史累计分配量。分配会贡献正数，释放会贡献负数；脚本按 `upid / heap_name / callsite_id` 做：

```sql
SUM(size) AS net_alloc_bytes
```

因此 `net_alloc_bytes < 0` 表示这个调用栈在统计窗口内释放多于分配。脚本不会在基础统计里把负数改成 0，也不会默认取绝对值。

各类输出的处理方式如下：

- 终端 `summary`：直接累加带符号的 `net_alloc_bytes`，正负会互相抵消，用来表示最终净增长或净释放。
- 终端调用栈明细：每条记录保留原始正负号；排序使用 `abs(net_alloc_bytes)`，所以释放量很大的负值也会排在前面。
- `summary.xlsx`：`Summary` 和 `Tree` sheet 都保留带符号净值；父分类是子分类的带符号聚合，可能因为正负抵消而小于任一子项的绝对值。
- 分类统计：每个分配栈仍按 `fs.ini` 顺序只归入第一个命中的分类；负值不会影响归类规则，只影响该分类的净值合计。
- 单个分类的 speedscope 文件：默认 `--speedscope-weight positive-net` 只写入 `net_alloc_bytes > 0` 的 callsite，`net_alloc_bytes <= 0` 会被跳过，因为 speedscope 的 sample weight 不能表达负贡献。
- `summary.speedscope.json`：默认只写入净值为正的叶子分类或 `remaining`；净值为 0 或负数的分类不会出现在该 summary 视图中。

如果需要把释放量也作为“变化规模”观察，可以使用 `--speedscope-weight absolute-net`：

```bash
python3 -B heap_analyzer/query_heap_alloc_stacks_by_symbol.py \
  --all-allocations \
  --limit 0 \
  --classify-config heap_analyzer/fs.ini \
  --classify-speedscope-dir fs_speedscope_abs \
  --speedscope-weight absolute-net
```

`absolute-net` 只影响 speedscope 输出权重，不改变终端统计和 `summary.xlsx` 的带符号净值。单个分类 speedscope 会对每个 callsite 使用 `abs(net_alloc_bytes)`；`summary.speedscope.json` 的每个 sample 对应分类汇总，因此使用该分类净值的绝对值。

按用途选择口径：

- 看“最终净增长”：使用终端 summary、明细和 `summary.xlsx` 的带符号 `net_alloc_bytes`。
- 看“释放或分配变化规模”：使用 `--speedscope-weight absolute-net`，同时回看 `summary.xlsx` 判断方向是净增长还是净释放。
- 看“当前正向净分配热点”：使用默认 `positive-net` speedscope。

## 表关系

Native heap profile 的分配记录和符号表关系如下：

```text
heap_profile_allocation.callsite_id
        |
        v
stack_profile_callsite.id
        |
        | frame_id
        v
stack_profile_frame.id
        |
        | symbol_set_id
        v
stack_profile_symbol.name
```

注意：Perfetto UI 的 Native heap profile 搜索通常会命中 `stack_profile_symbol.name`。有些 trace 中 `stack_profile_frame.name` 可能为空，或者只保存未反混淆名称；如果 SQL 只查 `stack_profile_frame.name/deobfuscated_name`，会出现 CLI 查不到但 UI 能搜到的情况。

可以用下面的快速查询验证某个符号是否存在于符号表：

```bash
$PerfettoRoot/out/linux_clang_release/trace_processor_shell query \
  /home/dianhun/disk2/work/fsprofiler/PerfData/mem/2026-06-01_18-57-13/symbolized-trace \
  "select f.id, f.name, f.deobfuscated_name, f.symbol_set_id, s.name as symbol_name
   from stack_profile_frame f
   join stack_profile_symbol s using(symbol_set_id)
   where s.name glob '*il2cpp::vm::Class::Init*'
   limit 20"
```

## 关键 SQL 逻辑

匹配目标函数时同时检查三类名称：

```sql
WHERE IFNULL(f.deobfuscated_name, '') GLOB '*' || t.symbol || '*'
   OR IFNULL(f.name, '') GLOB '*' || t.symbol || '*'
   OR EXISTS (
     SELECT 1
     FROM stack_profile_symbol AS s
     WHERE s.symbol_set_id = f.symbol_set_id
       AND s.name GLOB '*' || t.symbol || '*'
   )
```

这样可以覆盖：

- `stack_profile_frame.deobfuscated_name`
- `stack_profile_frame.name`
- `stack_profile_symbol.name`

## 性能注意事项

`heap_alloc_stacks_by_symbol.sql` 会使用递归 CTE 展开调用栈。对于调用栈节点很多、分配 callsite 很多的 trace，完整展开可能比较慢，当前测试 trace 上 120 秒未返回。

`query_heap_alloc_stacks_by_symbol.py` 的优化方式是：

```text
trace_processor query 导出基础表
        |
        v
Python 建 parent_id -> children 索引
        |
        v
从目标 callsite 向下遍历子树
        |
        v
关联 heap_profile_allocation 聚合结果并输出完整栈
```

在当前测试 trace 上，优化脚本验证结果约 42 秒返回：

```text
target_frames: 16
target_callsites: 23913
matched_callsites: 326726
matched_allocation_callsites: 30480
net_alloc_count: 4869
net_alloc_bytes: 152159428
net_alloc_mib: 145.111
```

同一份数据输出为 speedscope sampled profile 时，profile 的 `unit` 为 `bytes`，每条 sample 对应一个分配 callsite 的完整调用栈，`weight` 对应该 callsite 的分配字节数。

## 按 fs.ini 分类统计

`fs.ini` 的格式是：

```ini
# 分类显示名称
关键字1
关键字2

# 下一个分类
关键字3
```

分类规则按文件顺序执行。一个分配栈命中某个分类后，会从后续分类中移除；没有命中的分配栈会进入 `remaining`。

使用 `--classify-config` 时，脚本默认分析全部 `heap_profile_allocation`，不会被默认 `--symbol` 值过滤。只有显式传入 `--symbol ...` 时，才会先按该符号筛选调用栈，再对筛选后的分配栈分类。

对全部 Native heap 分配栈分类，默认输出 pprof 分类结果；如需 speedscope 明细，再额外传入 `--classify-speedscope-dir`：

```bash
python3 -B heap_analyzer/query_heap_alloc_stacks_by_symbol.py \
  --all-allocations \
  --limit 0 \
  --classify-config heap_analyzer/fs.ini
```

输出内容：

```text
<symbolized-trace 所在目录>/heap_analyze/native_heap.pprof.pb.gz
<symbolized-trace 所在目录>/heap_analyze/category_summary.pprof.pb.gz
<symbolized-trace 所在目录>/heap_analyze/pprof_categories/01_il2cpp_meta.pprof.pb.gz
<symbolized-trace 所在目录>/heap_analyze/pprof_categories/05_il2cpp_meta_ClassInit.pprof.pb.gz
<symbolized-trace 所在目录>/heap_analyze/summary.xlsx
<symbolized-trace 所在目录>/heap_analyze/summary.speedscope.json
...
```

默认 pprof 输出说明：

- `native_heap.pprof.pb.gz`：全部匹配分配栈的明细 profile；分类模式下每个 sample 会带 `category` 和 `category_type` 标签，可用 pprof 的 `tagfocus` / `tagshow` 过滤。
- `category_summary.pprof.pb.gz`：分类汇总 profile；每个 sample 对应一个叶子分类或 `remaining`，调用栈形态为 `Native heap summary / classified / 大分类 / 子分类`。
- `pprof_categories/*.pprof.pb.gz`：每个父分类、叶子分类和 `remaining` 的明细 profile；父分类文件聚合所有子分类，叶子分类文件对应具体规则。

常用 pprof 查看方式：

```bash
go tool pprof -http=:0 <symbolized-trace 所在目录>/heap_analyze/category_summary.pprof.pb.gz
go tool pprof -tagfocus=category=il2cpp_meta/ClassInit -http=:0 <symbolized-trace 所在目录>/heap_analyze/native_heap.pprof.pb.gz
go tool pprof -sample_index=absolute_net_alloc_bytes -http=:0 <symbolized-trace 所在目录>/heap_analyze/pprof_categories/05_il2cpp_meta_ClassInit.pprof.pb.gz
```

其中 `summary.xlsx` 包含总量、按 `/` 拆分后的树状分类统计，以及未命中的 `remaining`。工作簿包含两个 sheet：

- `Summary`：总计、已分类总计、未分类 remaining。
- `Tree`：按 `level_1 / level_2 / ...` 展开层级；父节点聚合所有子分类，叶子节点对应 `fs.ini` 中的完整分类。

speedscope 文件也按同一棵分类树输出：父节点文件聚合所有子分类，叶子节点文件对应具体规则。比如 `unity3d.speedscope.json` 是所有 `unity3d/*` 的聚合，`unity3d_gpu.speedscope.json` 是 `unity3d/gpu` 的明细。

`summary.speedscope.json` 是 summary 的分类树视图：每个 sample 对应一个叶子分类或 `remaining`，调用栈形态为 `Native heap summary / classified / 大分类 / 子分类`。父分类由 speedscope 聚合显示，因此不会重复计入父子节点的字节数。

如果只想先确认符号是否存在，优先运行上面的快速查询；如果快速查询能命中，再运行完整分配栈查询。

如果完整查询超时，可以先降低输出规模：

```sql
LIMIT 50;
```

或者先只查聚合量，再按需要展开具体 callsite。

## 常见问题

### 为什么 Perfetto UI 能搜到，但 SQL 查不到？

通常是 SQL 只查了 `stack_profile_frame.name`。UI Native heap profile 展示和搜索的名称可能来自 `stack_profile_symbol.name`，需要通过 `stack_profile_frame.symbol_set_id` 关联。

### `net_alloc_bytes` 为什么可能是负数？

`heap_profile_allocation.size` 表示一段采样或快照中的净变化，释放记录会体现为负数。脚本使用 `SUM(size)`，因此结果是净分配量，不是历史累计分配总量。

### 调用栈里的 `[inline]` 是什么？

同一个 `symbol_set_id` 可能对应多个 `stack_profile_symbol` 记录，通常表示内联栈。脚本会把这些符号合并到同一个 frame 展示，并用 `[inline]` 标记。
