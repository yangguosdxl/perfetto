# Wann 常用工具编译指令

本文记录当前仓库里常用 Perfetto 工具的编译命令。所有命令默认在 Perfetto 源码根目录执行：

```bash
cd /home/dianhun/disk2/perfetto
```

## Linux 本机工具

### trace_processor_shell

```bash
tools/ninja -C out/linux_clang_release trace_processor_shell
```

产物路径：

```text
out/linux_clang_release/trace_processor_shell
```

### traceconv

```bash
tools/ninja -C out/linux_clang_release traceconv
```

产物路径：

```text
out/linux_clang_release/traceconv
```

### tracebox

```bash
tools/ninja -C out/linux_clang_release tracebox
```

产物路径：

```text
out/linux_clang_release/tracebox
```

### 一次性编译常用 Linux 工具

```bash
tools/ninja -C out/linux_clang_release trace_processor_shell traceconv tracebox
```

## Android arm64 工具

### trace_processor_shell

```bash
tools/ninja -C out/android_release_arm64 trace_processor_shell
```

产物路径：

```text
out/android_release_arm64/trace_processor_shell
```

### traceconv

```bash
tools/ninja -C out/android_release_arm64 traceconv
```

产物路径：

```text
out/android_release_arm64/traceconv
```

### tracebox

```bash
tools/ninja -C out/android_release_arm64 tracebox
```

产物路径：

```text
out/android_release_arm64/tracebox
```

### 一次性编译常用 Android arm64 工具

```bash
tools/ninja -C out/android_release_arm64 trace_processor_shell traceconv tracebox
```

## Android arm 工具

### trace_processor_shell

```bash
tools/ninja -C out/android_release_arm trace_processor_shell
```

产物路径：

```text
out/android_release_arm/trace_processor_shell
```

### traceconv

```bash
tools/ninja -C out/android_release_arm traceconv
```

产物路径：

```text
out/android_release_arm/traceconv
```

### tracebox

```bash
tools/ninja -C out/android_release_arm tracebox
```

产物路径：

```text
out/android_release_arm/tracebox
```

### 一次性编译常用 Android arm 工具

```bash
tools/ninja -C out/android_release_arm trace_processor_shell traceconv tracebox
```

## 产物检查

检查文件架构和调试信息：

```bash
file out/linux_clang_release/trace_processor_shell
file out/android_release_arm64/trace_processor_shell
file out/android_release_arm/trace_processor_shell
```

检查 Linux 本机 trace_processor_shell 版本：

```bash
out/linux_clang_release/trace_processor_shell --version
```

## 符号化注意事项

本仓库的符号化逻辑会使用 `llvm-symbolizer --output-style=JSON`。如果系统 `/usr/bin/llvm-symbolizer` 版本过旧，可能报错：

```text
Cannot find option named 'JSON'
```

优先使用仓库自带 LLVM 工具：

```bash
export PATH="$PWD/buildtools/linux64/clang/bin:$PATH"
```
