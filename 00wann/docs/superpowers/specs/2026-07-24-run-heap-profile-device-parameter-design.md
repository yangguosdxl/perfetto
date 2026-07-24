# run_heap_profile 设备参数设计

## 目标

为 `run_heap_profile.py` 和 `run_heap_profile.sh` 增加命名参数
`--device <serial>`，确保一次 Native heap profile 流程中的所有 adb 命令都选择
同一台设备，同时保持现有 `interval_bytes`、`shmem_size` 位置参数兼容。

## 命令行接口

```bash
./run_heap_profile.sh --device 1C111FDF600AW5
./run_heap_profile.sh --device 1C111FDF600AW5 1024 67108864
```

`--device` 可省略；省略时继续使用 adb 当前的默认选机行为。缺少设备序列号、位置参数
超过两个或参数无法识别时，脚本打印用法并返回非零状态。历史首参数 `45000` 的兼容逻辑
继续保留。

## 实现

`run_heap_profile.sh` 保持透明包装，只把参数传给 Python。`run_heap_profile.py` 使用标准
命令行解析器解析 `--device` 和两个位置参数。指定设备后，把序列号写入本次流程使用的
环境变量 `ANDROID_SERIAL`，并同步到 Python 当前进程环境。

该环境会传给脚本自身启动的 adb、`fsbootcmd_push_to_phone.sh` 和 Perfetto
`heap_profile.py`。Perfetto 源码中的 `heap_profile.py` 使用裸 `adb` 命令，因此继承
`ANDROID_SERIAL` 可以覆盖采集、停止和 trace 拉取等完整流程，避免只给外层 adb 加
`-s` 后内外层选择不同设备。

## 测试与文档

先扩展 `test_run_heap_profile.sh`，验证指定设备后假的 adb、fsboot 子脚本和假的
Perfetto profiler 都能读到相同的 `ANDROID_SERIAL`，并验证默认值及错误参数行为；确认
测试因功能缺失而失败后再修改实现。完成后运行脚本测试、shell 语法检查和 Python 编译
检查。

同步更新 `AGENTS.md`、`README.md` 和 `docs/heap_profile.md` 的命令示例及参数说明。
按照项目约定，代码修改后执行 45 秒无栈 mmap 验证；验证只检查 mmap syscall events
和 smaps 健康状态，不启用 heapprofd malloc，并确保 Perfetto 先于重启后的目标 App
启动。
