---
id: mmap-perf-callstack-health
type: knowledge
title: mmap perf 调用栈健康验收
status: active
created: 2026-08-12
updated: 2026-08-13
tags: [pensieve, knowledge, perfetto, mmap]
---

# mmap perf 调用栈健康验收

## Source

- `collect_mmap_phys_data.py` 的 `query_target_perf_callstacks()` 和
  `finish_collection()`。
- `PerfData/mmap_phys/2026-08-12_17-07-59/mmap_trace.perfetto-trace`，目标 PID
  32096。
- Perfetto `src/profiling/perf/event_config.cc`、
  `src/profiling/perf/perf_producer.cc`。
- 设备日志 `proc_descriptors.cc:61` 的 `/proc/<pid>/maps Permission denied`。

## Summary

mmap 主功能不能只检查 perf 样本数和 buffer 丢数；必须确认目标进程至少有一个
非空 `perf_sample.callsite_id`，否则空归因文件应判失败。

## Content

`UNWIND_DWARF` 会请求用户栈和寄存器，但只有 traced_perf 成功取得进程描述信息并
完成展开后，trace 中才会写入 callstack IID，trace processor 才能生成
`perf_sample.callsite_id`、`stack_profile_callsite` 和 frame。

2026-08-12 的真机 trace 中，正式 App PID 32096 有 21154 个 perf samples，但
callsites 为 0；设备同时报告 traced_perf 无权读取 `/proc/32096/maps`。因此
`perf_data_loss=0` 和 `perf_samples_skipped_dataloss=0` 不能证明调用栈归因有效。

2026-08-13 的后续真机对照确认还有两个独立时序条件：

1. Android init 会在 `linux.perf` session启动时按 lazy属性拉起 nobody producer；只
   额外启动 root producer会让同一数据源同时分配给两者。采集期间需要抑制 lazy条件并
   验证设备上只有一个 root `traced_perf`，收尾恢复原始属性和 service状态。
2. Perfetto `perf_producer.cc` 第一次看到 PID时会请求进程描述符；若超时，状态进入
   `kFdsTimedOut`，后续样本继续跳过且不自动重试。唯一 root producer下，perf session
   先于 App时为 `21829/0`，App PID后启动 perf短探针时为 `766/766`。

主功能因此拆成生命周期 session和调用栈 session：前者在 App前启动，保证启动期 mmap
syscall不漏；后者在 PID出现后启动，避免 descriptor lookup过早。分析器分别加载
`mmap_trace.perfetto-trace` 与 `mmap_callstack_trace.perfetto-trace`，使用 Linux全局 tid
和 BOOTTIME时间戳关联。两个 protobuf stream不能直接拼接，真机验证会产生
`invalid_clock_snapshots` 与 `sorter_push_event_out_of_order`。

完整默认流程的独立原始 trace验证结果：调用栈目标 PID
`perf_samples=21356, perf_callsites=21356`，生命周期关联出 `21333` 个 mmap事件；两份
trace的 Perfetto/ftrace/perf loss均为 0。

验收规则：

```text
目标 PID perf_samples > 0 且 perf_callsites > 0
  -> 调用栈输入可用，再继续判断丢样和归因结果。

查询明确得到 perf_callsites = 0
  -> perf_callstacks_missing，主功能失败。

查询工具缺失或 SQL 执行失败
  -> 标记未检查，不得伪造为已确认的 0；应单独处理检查链路故障。
```

## When to Use

修改 mmap 调用栈采集、Perfetto producer 启动方式、设备权限或主功能验收逻辑时，
先用目标 PID 查询 `perf_sample` 的 samples/callsites，再判断 JSON 和 pprof 是否可信。
若 App需要重启，不能只做“已运行 App”的短探针；必须同时验证 App前启动生命周期
session、PID后启动调用栈 session的完整路径。

## 上下文链接

- 相关：[[knowledge/native-heap-profile-gm-and-session-health]]
