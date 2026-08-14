---
id: 2026-08-13-use-explicit-device-test-extension-points
type: decision
title: 真机性能框架采用显式平台功能流程扩展点并渐进复用专业后端
status: active
created: 2026-08-13
updated: 2026-08-14
tags: [pensieve, perfetto, framework, android, malloc, mmap]
---

# 真机性能框架采用显式平台功能流程扩展点并渐进复用专业后端

## 一句话结论

> 通用真机框架固定拆成 `PlatformAdapter`、`FeaturePlugin` 和 `FlowSpec` 三个显式
> 注册扩展点；v1 每轮只启用一个采集功能，并继续复用已验证的 malloc/mmap 专业后端。

## 上下文链接

- 基于：[[knowledge/mmap-perf-callstack-health]]
- 相关：[[knowledge/native-heap-profile-gm-and-session-health]]
- 相关：[[decisions/2026-08-12-hide-anr-dialogs-during-profiling]]

## Context

`run_heap_profile.sh` 与 `run_mmap_phys_profile.sh` 共享设备、配置、测试动作和报告需求，
但 heapprofd 与 mmap 的 Perfetto 启停、App 重启、smaps、meminfo 和健康验证时序不同。
真实设备验证还证明，宿主机工具选择必须复用专业后端已经验证过的二进制命名规则。

## Problem

既要消除两个入口的公共重复，又不能用一个巨大控制器或任意多插件组合改变采样负载，
也不能在缺少真机等价证据时重写已经稳定的专业采集时序。

## Alternatives Considered

- 动态插件扫描或事件总线：扩展灵活，但增加定位和加载顺序复杂度，当前功能数量不需要。
- 一次性把 App/PID/logcat/Perfetto 全部迁入公共引擎：理论边界完整，但回归面过大，
  容易破坏 Perfetto 先于 App、双会话和 profiler shutdown 等已验证顺序。
- 同轮组合 malloc 与 mmap 插件：能获得更多指标，但额外采样开销会改变性能和内存口径。

## Decision

1. 注册表只显式注册 Android、malloc、mmap、`fs_login_battle` 和 `none`。
2. 公共引擎管理连接、运行级设置、能力校验、插件进程、清理和统一报告。
3. v1 的 `FeaturePlugin` 启动 `run_heap_profile.py` 或 `collect_mmap_phys_data.py`，专业后端
   继续持有 App/PID/logcat/Perfetto/分析时序，并通过 `ANDROID_SERIAL` 绑定同一设备。
4. 配置键与工具二进制名显式映射；`trace_processor` 必须解析为
   `trace_processor_shell(.exe)`，不能机械拼接名字。
5. iOS/Windows 只保留协议方向，在真实设备和功能实现出现前不注册虚假适配器。
6. 通用核心独立维护在 `fs/device-test-framework` 仓库，父仓库通过
   `00wann/device_test_framework` 子模块固定已验收 commit；malloc/mmap、FS 流程、
   Perfetto 工具定位和旧项目变量保留在 `device_test_plugins`，禁止反向依赖。
7. ADB、目标 App 日志、Poco RPC、流程脚本契约和协程竞速属于通用框架的 `actions`
   SDK；具体测试目的独立维护在 `fs/device-test-profile-actions`，父仓库通过
   `00wann/profile_actions` 子模块固定验收 commit，禁止在宿主或脚本仓库复制公共实现。

## Consequence

- 旧入口和 `PerfData/mem`、`PerfData/mmap_phys` 路径保持兼容。
- 每轮统一生成配置、清单、文本摘要和 Markdown 报告。
- 后续迁移专业阶段时必须逐项用现有集成测试和真实主功能证明行为等价。
- AndroidAdapter 当前不是所有 ADB 操作的唯一所有者；文档必须保持这一渐进边界。

## 探索减负

- 下次可以少问什么：是否需要动态插件发现或同时运行多个采集插件；当前决策都是否。
- 下次可以少查什么：通用入口和报告骨架位于 `device_test_framework/` 子模块，项目
  注册表位于 `device_test_plugins/registry.py`，公共流程 SDK 位于框架 `actions/`，具体
  流程位于 `profile_actions/` 子模块，专业采集时序仍在两个现有后端。
- 失效条件：专业后端内部公共阶段全部完成等价迁移，或出现经过真实验证的 iOS/Windows
  平台，或明确要求并验证多采集器组合不会改变测试口径。
