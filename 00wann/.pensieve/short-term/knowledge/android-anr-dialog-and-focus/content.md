---
id: android-anr-dialog-and-focus
type: knowledge
title: Android ANR 对话框与焦点事件
status: active
created: 2026-08-12
updated: 2026-08-12
tags: [android, anr, perfetto]
---

# Android ANR 对话框与焦点事件

## Source

- `PerfData/mem/2026-08-12_13-57-33/logcat.txt`
- `doc/方案/采集期间隐藏ANR对话框方案.md`

## Summary

Android 不会因 heapprofd 采集放宽输入分发 ANR 阈值；`hide_error_dialogs=1`
只隐藏对话框，不会禁用 ANR 检测和记录。

## Content

- 窗口切到前台或旋转完成时，`WindowManager/InputDispatcher` 会自动发送
  `FocusEvent(hasFocus=true)`，不需要用户点击。
- 主窗口超过 5 秒未处理焦点/输入事件时，系统仍按正常规则记录
  `Input dispatching timed out` ANR。
- ANR 对话框本身会改变目标应用焦点，可能干扰性能采集的目标场景。
- `settings put global hide_error_dialogs 1` 可隐藏对话框，但必须保存并恢复原值。

## When to Use

排查性能采集期间的 ANR、焦点变化或自动流程受系统对话框干扰时先读。

## 上下文链接

- 导致：[[decisions/2026-08-12-hide-anr-dialogs-during-profiling]]
