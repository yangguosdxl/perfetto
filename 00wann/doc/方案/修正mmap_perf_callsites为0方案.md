# 修正 mmap `perf_callsites=0` 方案

## 问题与依据

正式 App 采集曾出现 `perf_samples > 0`、`perf_callsites = 0`。Perfetto 源码表明：

1. `linux.perf` 数据源在 session 启动时分配给已注册的 `traced_perf` producer。
2. `perf_producer.cc` 在首次看到 PID 时请求进程描述符；请求超时后状态进入
   `kFdsTimedOut`，后续样本继续跳过，不会自动重试。
3. Android init 会按 lazy 属性另行拉起 nobody producer，若不抑制会与 root producer
   同时接收同一 `linux.perf` 数据源。

真机对照：

```text
唯一 root producer，perf session 先于 App：21829 samples / 0 callsites
唯一 root producer，App PID 后启动 perf：766 samples / 766 callsites
双 session 完整默认流程：208 target samples / 208 callsites
```

## 实现方案

```text
抑制 init lazy producer
  -> 启动并校验唯一 root traced_perf
  -> 启动生命周期 session（ftrace/process_stats）
  -> 重启 App，等待 PID
  -> 启动调用栈 session（linux.perf）
  -> 登录、测试模块、稳定采集
  -> 停止两个 session
  -> 恢复 traced_perf 系统状态
  -> 分别加载两份 trace，以 Linux tid + BOOTTIME 时间关联
```

两份 protobuf trace不能直接字节拼接。真机验证显示直接拼接会使 clock snapshot倒序，
产生 `invalid_clock_snapshots` 和 `sorter_push_event_out_of_order`。因此生命周期 trace和
调用栈 trace独立保存、独立送入 trace processor。

## 执行计划

- [x] 基于 Perfetto源码确认 producer分配和 `kFdsTimedOut` 状态机。
- [x] 真机验证唯一 root producer并抑制 init lazy producer。
- [x] 真机完成 App前/App后启动 perf session对照。
- [x] 实现生命周期/调用栈双 session采集与异常恢复。
- [x] 分析器增加 `--callstack-trace`，跨 session按 Linux tid和时间戳关联。
- [x] 拒绝二进制直接拼接，分别检查两份 trace健康状态。
- [x] 增加跨 session单元测试并运行全量测试。
- [x] 使用完整默认流程原始 trace验证：208 个有效 perf调用栈、208 个归因生命周期。
- [x] 同步 README、分析器文档和 Pensieve知识。
