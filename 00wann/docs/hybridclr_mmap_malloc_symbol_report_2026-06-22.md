# HybridCLR mmap malloc 符号化与验证报告

日期：2026-06-22

## 结论

本次 Native heap 分类最初没有解出 `libil2cpp.so` 符号，根因是 `run_heap_profile.py` 使用了旧符号目录：

```text
00wann/workspace/allsymbols/arm64-v8a
```

该目录中的 `libil2cpp.so` Build ID 为 `fee64214b8aaecdd70045fc27094c593e2ddaaef`，而本次 trace 中加载的 `libil2cpp.so` Build ID 为 `32a6522647310c61ba62fcd92d722c302334f649`，与当前 FS 打包产物符号目录一致：

```text
D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\Published\Android\DreamRivakes2.apk\unityLibrary\symbols\arm64-v8a
```

因此，之前基于旧 `symbolized-trace` 得出的 `il2cpp_meta/hybridclr` 归零结论无效；必须以重新符号化后的结果为准。

## 根因链路

```text
raw-trace
  libil2cpp.so Build ID = 32a6522647310c61ba62fcd92d722c302334f649
    |
    +-- 旧符号目录 workspace/allsymbols/arm64-v8a
    |     libil2cpp.so Build ID = fee64214b8aaecdd70045fc27094c593e2ddaaef
    |     结果：12193 个 il2cpp frame，0 个符号
    |
    +-- 当前 FS 打包产物符号目录
          libil2cpp.so Build ID = 32a6522647310c61ba62fcd92d722c302334f649
          结果：12193 个 il2cpp frame 全部有符号，7389 个不同符号
```

Perfetto 源码侧依据：

- `src/traceconv/traceconv.cc`：`traceconv symbolize` 没有额外符号路径参数。
- `src/traceconv/symbolize_profile.cc`：符号化读取 `PERFETTO_BINARY_PATH`。
- `src/trace_processor/util/symbolizer/symbolize_database.cc`：Windows 下 `PERFETTO_BINARY_PATH` 使用分号分隔。
- `src/trace_processor/util/symbolizer/local_symbolizer.cc`：Windows 下默认调用 `llvm-symbolizer.exe`，因此需要把 `PerfettoRoot/buildtools/win/clang/bin` 加入 `PATH`。

## 脚本修复

已修改 `00wann/run_heap_profile.py`：

```python
DEFAULT_FS_SYMBOLS_DIR = Path(
    r"D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj"
    r"\BuildCache\Published\Android\DreamRivakes2.apk"
    r"\unityLibrary\symbols\arm64-v8a")
LEGACY_SYMBOLS_RELATIVE_DIR = Path("workspace/allsymbols/arm64-v8a")
```

默认行为：

```text
1. 如果外部设置了 PERFETTO_BINARY_PATH，脚本原样保留。
2. 否则优先使用当前 FS 打包产物 unityLibrary/symbols/arm64-v8a。
3. 继续追加 00wann/workspace/allsymbols/arm64-v8a 作为补充符号目录，用于解析 libBattleLogic.so、libprotobuf.so 等旧目录独有符号。
4. 如需临时覆盖优先符号目录，使用 RUN_HEAP_PROFILE_SYMBOLS_DIR=<符号目录>。
```

当前机器实际生成的路径：

```text
D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\Published\Android\DreamRivakes2.apk\unityLibrary\symbols\arm64-v8a;D:\dr2\Misc\perfetto\00wann\workspace\allsymbols\arm64-v8a
```

## 重新符号化输出

未覆盖原始 `symbolized-trace`，新文件如下：

```text
D:\dr2\Misc\perfetto\00wann\PerfData\mem\2026-06-22_15-37-06\symbolized-trace.correct-path
D:\dr2\Misc\perfetto\00wann\PerfData\mem\2026-06-22_15-37-06\symbols.correct-path
D:\dr2\Misc\perfetto\00wann\PerfData\mem\2026-06-22_15-37-06\symbolize.correct-path.stderr.log
```

`libil2cpp.so` 符号化验证：

```text
frames=12193
frames_with_symbols=12193
symbols=7389
```

## malloc 分类输出

重新分类输出：

```text
D:\dr2\Misc\perfetto\00wann\PerfData\mem\2026-06-22_15-37-06\heap_analyze\summary.correct-path.xlsx
D:\dr2\Misc\perfetto\00wann\PerfData\mem\2026-06-22_15-37-06\heap_analyze\summary.correct-path.speedscope.json
```

同口径基线：

```text
D:\dr2\Misc\perfetto\00wann\PerfData\mem\2026-06-16_21-12-05\heap_analyze\summary.xlsx
```

关键对比：

| 分类 | 基线 MiB | 最新 MiB | 变化 MiB | 变化 |
| --- | ---: | ---: | ---: | ---: |
| TOTAL | 643.640 | 709.207 | +65.567 | +10.2% |
| classified_total | 614.652 | 678.927 | +64.274 | +10.5% |
| remaining | 28.988 | 30.281 | +1.293 | +4.5% |
| il2cpp_meta | 144.463 | 178.232 | +33.769 | +23.4% |
| il2cpp_meta/Init | 23.238 | 23.201 | -0.036 | -0.2% |
| il2cpp_meta/il2cpp_codegen_initialize_runtime_metadata | 22.748 | 30.138 | +7.391 | +32.5% |
| il2cpp_meta/GlobalMetadata | 16.563 | 16.427 | -0.136 | -0.8% |
| il2cpp_meta/ClassInit | 65.836 | 78.997 | +13.162 | +20.0% |
| il2cpp_meta/ClassSetupMethods | 16.079 | 29.467 | +13.388 | +83.3% |
| hybridclr | 124.134 | 71.217 | -52.917 | -42.6% |
| hybridclr/trasform | 4.646 | 10.887 | +6.241 | +134.3% |
| hybridclr/runtime | 111.428 | 51.208 | -60.220 | -54.0% |
| hybridclr/other | 8.059 | 9.122 | +1.062 | +13.2% |
| unity3d | 322.405 | 370.032 | +47.627 | +14.8% |
| unity3d/gpu | 211.112 | 214.305 | +3.192 | +1.5% |

## 优化判断

HybridCLR 加载链路 malloc 有下降，主要体现在：

```text
hybridclr/runtime: 111.428 MiB -> 51.208 MiB，减少 60.220 MiB
hybridclr 总组:    124.134 MiB -> 71.217 MiB，减少 52.917 MiB
```

这说明低内存 mmap 优化对 HybridCLR runtime 加载链路的 malloc 净分配有收益。

但总 malloc live 没有下降：

```text
TOTAL: 643.640 MiB -> 709.207 MiB，增加 65.567 MiB
```

同时，`il2cpp_meta` 和 `unity3d` 分类上升，抵消并超过了 HybridCLR runtime 的下降。因此本次不能宣称整体 Native heap 或总 malloc live 已优化，只能确认 HybridCLR 相关 runtime 加载链路下降。

## 对账状态

基线和最新采样的 `heap_meminfo_validation.txt` 都是 FAIL：

```text
基线 2026-06-16_21-12-05:
malloc_live_bytes=674905903
meminfo_native_heap_alloc_bytes=747557888
diff_bytes=72651985
allowed_diff_bytes=67108864
HEAP_MEMINFO_VALIDATION=FAIL

最新 2026-06-22_15-37-06:
malloc_live_bytes=743657950
meminfo_native_heap_alloc_bytes=832317440
diff_bytes=88659490
allowed_diff_bytes=67108864
HEAP_MEMINFO_VALIDATION=FAIL
```

因此报告结论只能使用分类趋势，不能宣称 `malloc_live_bytes` 与 `Native Heap Alloc` 总量对账通过。

## 验证命令

已执行并通过：

```bash
bash -lc './test_run_heap_profile.sh'
python -m py_compile D:\dr2\Misc\perfetto\00wann\run_heap_profile.py
bash -n D:\dr2\Misc\perfetto\00wann\run_heap_profile.sh D:\dr2\Misc\perfetto\00wann\test_run_heap_profile.sh
```

关键输出：

```text
LOGIN_SCENE_DONE|pattern=登录场景完成|stable_seconds=30
```

## 后续建议

1. 后续 Native heap 采集直接使用修复后的 `run_heap_profile.sh`，不要手工设置旧 `PERFETTO_BINARY_PATH`。
2. 若切换 APK 或临时打包目录，优先设置 `RUN_HEAP_PROFILE_SYMBOLS_DIR`，不要改脚本常量。
3. 如需给出整体 Native heap 优化结论，需要先解决 `HEAP_MEMINFO_VALIDATION=FAIL` 的对账差异，或明确报告只讨论 heapprofd 分类趋势。
