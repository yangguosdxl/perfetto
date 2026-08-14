---
id: 2026-08-12-hide-anr-dialogs-during-profiling
type: decision
title: 采集期间隐藏 ANR 对话框并恢复设备原设置
status: active
created: 2026-08-12
updated: 2026-08-12
tags: [android, anr, perfetto, collection]
---

# 采集期间隐藏 ANR 对话框并恢复设备原设置

## 一句话结论

> `run_heap_profile.sh` 和 `run_mmap_phys_profile.sh` 在采集前保存
> `hide_error_dialogs`，临时设为 `1`，并在所有退出路径恢复原值。

## 上下文链接

- 基于：[[knowledge/android-anr-dialog-and-focus]]

## Context

高开销采样可使应用超过 Android 输入分发 ANR 阈值。ANR 记录是有价值的
诊断信息，但对话框会反复改变焦点并干扰采集流程。

## Problem

既要保留系统 ANR 证据，又要避免系统对话框改变目标场景，且不能把
设备全局设置永久留在修改状态。

## Alternatives Considered

- 提高 ANR 阈值：非普通采集工具可靠控制，也会改变系统判定语义。
- 提高采样间隔：能降低开销，但不适用于必须保留高密度样本的轮次。
- 永久设置 `hide_error_dialogs=1`：会污染设备后续行为。

## Decision

入口层统一使用 `EXIT` trap 恢复原值。恢复失败只告警，不覆盖采集的
原始退出码。文档必须明确“只隐藏对话框，不禁用 ANR”。

## Consequence

- 采集期间不再因 ANR 对话框反复失焦。
- `logcat`/DropBox 仍保留 ANR 证据。
- 入口不能用会绕过 Shell `EXIT` trap 的进程替换方式。

## 探索减负

- 下次可以少问什么：是否要禁用 ANR；决策是保留检测，只隐藏对话框。
- 下次可以少查什么：入口的设置生命周期已在 `common_tools.sh` 统一实现。
- 失效条件：Android 移除该 global setting，或采集迁移到不共享 Shell 退出生命周期的启动器。
