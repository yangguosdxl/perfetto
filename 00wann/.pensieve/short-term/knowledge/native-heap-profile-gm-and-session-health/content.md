---
id: native-heap-profile-gm-and-session-health
type: knowledge
title: FS Native heap 的 GM 就绪与会话健康信号
status: active
created: 2026-08-11
updated: 2026-08-13
tags: [pensieve, perfetto, heapprofd, poco, fs]
---

# FS Native heap 的 GM 就绪与会话健康信号

## Source

- `run_heap_profile.py`
- `docs/heap_profile.md`
- `PerfData/mem/2026-08-11_21-12-05`
- `PerfData/mem/2026-08-11_21-18-11`
- `PerfData/mem/2026-08-11_21-24-05`
- `PerfData/mem/2026-08-11_21-28-04`

## Summary

FS 登录日志不代表战斗配置已经可用；Native heap 真机采集必须同时验证 GM 就绪、原 PID 存活和 heap dump 健康状态。

## Content

1. `登录场景完成` 后仍需等待 `RegistForGameStart.LoadOtherTable.End`，再调用 `DoRecordCheat`。过早执行战斗录像 GM 会在 `MatchAiImageConfig` 尚未加载时进入空引用链，并可能导致 UnityMain `SIGSEGV`。
2. GM RPC 成功后的稳定期应检查启动时记录的原 PID。只重新查询包名会把 App 重启后的新进程误当作原采集进程继续等待。
3. 连续启动 heapprofd 会话时，如果 trace 只有约 14 KB、`heap_dump_count=0`，同时 App 日志出现 heapprofd hook 状态竞态，应先确认设备侧没有残留 heapprofd，再重跑；该轮不能进入参数对比。
4. `health_sum=0` 且 `heap_dump_count>0` 只能证明本轮没有已知丢包并成功生成 heap dump，不能证明 malloc live 与 `Native Heap Alloc` 口径相同。多个 interval 的固定差值不随采样间隔单调变化时，应优先调查 heapprofd 未覆盖内存来源。
5. `heap_profile_allocation.size` 是 heapprofd 对调用者请求大小做 Poisson 采样后的估算值。Perfetto 的 `wrap_malloc`、`wrap_calloc` 和 `wrap_memalign` 直接把请求 `size` 传给 `AHeapProfile_reportAllocation`，没有调用 `malloc_usable_size`；最终 `HeapSample.self_allocated/self_freed` 也累计 `sample_size`，不会输出协议中保留的真实 `alloc_size`。
6. Android 15 Pixel 6 上 FS 使用 Scudo。`dumpsys meminfo --logstats <pid>` 的 size class `块大小 × inuse` 加 secondary 活跃映射、扣除 secondary fragmentation，与同一时刻 `Native Heap / Heap Alloc` 只差数 MiB。因此 `Heap Alloc` 是 Scudo 实际块和映射的 allocator 口径，包含 size class 向上取整、chunk header、对齐和 secondary 映射开销，不能与 heapprofd 请求字节使用固定绝对差值验收。
7. 2026-08-11 四轮 FS 数据中，`Heap Alloc - heapprofd live` 占 `Heap Alloc` 的 10.32% 至 11.01%；业务 live 从约 0.998 GB 增到约 1.478 GB 时，差值从 109.6 MiB 增到约 171 至 174 MiB，说明它不是固定 180 MiB 漏采。FS 当前约有 263 万个 Scudo primary 活跃块，仅每块 16 字节 header 就约 40 MiB，小对象 size class 取整会继续放大差值。
8. 次要因素包括进程创建到 heapprofd 初始化之间约 47 ms 的早期分配、概率采样误差和 profiler 自身分配；三轮有效 trace 没有 buffer overrun、client error、missing packet、采样间隔自动调整或 spinlock timeout，且有 397 万至 1105 万 unwind samples，所以这些因素没有证据能解释稳定的约 11% 主差值。
9. Native heap 与 mmap 测试模块共享 `profile_action_api.py`：`run_adb()` 自动使用上下文设备序列号，`find_poco_port()` 只从本轮 PID 日志中取最后一个合法端口，`invoke_rpc()` 统一负责 ADB 转发和 Poco 长度帧 JSON-RPC，并在失败路径清理转发。`invoke_rpc()` 返回完整响应和本地/远端端口，但不解释业务 `result`；具体 GM 参数、成功条件、日志和产物仍归测试模块所有。
10. 公共接口现由执行器每轮创建一个 `ProfileActionSession`：首次 RPC 延迟扫描并缓存 Poco 端口，建立本轮复用的 ADB 转发，所有结束路径由执行器统一 `close()`。ADB、RPC、日志等待和详情写入等运行失败统一记录并返回 `success=False`，只有调用契约错误和协程取消继续抛出；是否把失败转成异常并终止本轮由测试脚本决定。RPC 默认要求 `result=True` 并写 `gm_rpc.txt`，只检查协议时显式传 `check_result=False`。

## When to Use

执行 `run_heap_profile.sh`、比较采样间隔、诊断 GM 后崩溃、零 heap dump 或 malloc/meminfo 固定差值时先读本条目。

## 上下文链接

- 相关：[[pipelines/run-when-reviewing-code]]
