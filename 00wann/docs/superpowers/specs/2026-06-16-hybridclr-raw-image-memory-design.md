# HybridCLR 原始字节常驻内存优化设计

日期：2026-06-16

## 背景

FS 安卓版在进入登录场景后，`il2cpp_meta + hybridclr` 运行时常驻内存是本次优化验收对象。验收目标是这两类合计常驻内存降低 50%，不是整进程 PSS 降低 50%。

当前已确认 FS 客户端在启动热更时一次性加载 Hotfix DLL 和 AOT 元数据 DLL 字节：

```text
StreamingAssets/Hotfix/*.bytes
StreamingAssets/AOTAssemblies/*.bytes
        |
        v
FileHelper.ReadFileFromStreamingPath -> 托管 byte[]
        |
        +-- 热更 DLL -> Assembly.Load(bytes)
        |      -> Assembly::Create -> CopyBytes -> RawImageBase::_imageData
        |
        +-- AOT 元数据 -> RuntimeApi.LoadMetadataForAOTAssembly(bytes, SuperSet)
               -> LoadMetadataForAOTAssembly -> CopyBytes -> RawImageBase::_imageData
```

`RawImageBase::_imageData` 当前在 `RawImageBase` 析构时才释放，而热更 `InterpreterImage` 和 AOT `AOTHomologousImage` 会注册到运行时并长期存在，因此这份 native 拷贝是常驻内存。

## 真实证据

基线采集目录：

- `D:\dr2\Misc\perfetto\00wann\PerfData\mem\2026-06-16_13-52-01`

本次采集覆盖到登录场景完成：

- `logcat.txt` 在 `2026-06-16 13:52:12` 输出 `登录场景完成`

分类口径：

- `il2cpp_meta` 约 `148.17 MiB`
- `hybridclr` 约 `121.53 MiB`
- 合计约 `269.70 MiB`
- 50% 验收线：合计不高于约 `134.85 MiB`

HybridCLR 原始字节证据：

- pprof 中 `CopyBytes` 约 `57.19 MiB`
- Android 工程 assets 中文件尺寸闭合：
  - `Hotfix/*.bytes` 合计约 `44.09 MiB`
  - `AOTAssemblies/*.bytes` 合计约 `13.09 MiB`

注意：这份基线的 `heap_meminfo_validation.txt` 仍为 `FAIL`，原因是 heapprofd live bytes 与 `dumpsys meminfo` Native Heap Alloc 差异约 `139 MiB`。它可作为优化方向证据，不能直接作为最终验收基线。最终验收前需要让采集工具按“登录完成 + 稳定期 + 对账通过”生成基线和优化后数据。

## 目标

第一阶段目标是消除或压缩 HybridCLR 中 Hotfix/AOT 原始 DLL 字节的 native 常驻拷贝，优先降低 `hybridclr/runtime` 的常驻内存。

完整验收目标仍是：

- `il2cpp_meta + hybridclr` 合计常驻内存降低 50%
- 真机设备 `1C111FDF600AW5`
- 游戏启动后自动进入登录场景，以 logcat 输出 `登录场景完成` 为场景结束标志
- AppIl2cpp 修改同步到 ECMA335 FSPatcher
- ECMA335 单元测试通过

## 方案选项

### 方案 A：只在 C# 层缩短 byte[] 生命周期

做法：

- `LoadHotfix` 每次加载一个 DLL 后立刻清理局部引用。
- 必要时在热更加载阶段后触发托管 GC。

收益：

- 可能降低托管堆峰值和短期残留。

不足：

- 无法解决 native `CopyBytes` 后的 `_imageData` 常驻。
- 不能覆盖已证实的 `57.19 MiB` native 常驻问题。
- 不足以支撑 50% 验收目标。

结论：不作为主方案，只能作为辅助优化。

### 方案 B：Native RawImage compact，保留懒读所需最小字节

做法：

- 在 `RawImageBase::Load` 完成 PE/metadata 解析后，构建一个 compact buffer。
- compact buffer 只包含后续运行时仍可能懒读的字节区段。
- 将 `_streamStringHeap`、`_streamUS`、`_streamBlobHeap`、`_streamGuidHeap`、`_streamTables`、`_tables[*].data`、method body、FieldRVA 数据等指针重定向到 compact buffer。
- 释放完整原始 PE buffer。

收益：

- 直接命中 `CopyBytes` 常驻问题。
- 保持 HybridCLR 对 `RawImageBase` 的读取模型，不需要重写解释器、签名解析、反射读取主流程。
- 可同时覆盖 Hotfix DLL 和 AOT 元数据 DLL。

风险：

- 热更 DLL 的 method body 不在 metadata stream 中，`MethodBodyCache` 会按需回到 raw image 读取 IL body，不能只保留 metadata streams。
- FieldRVA 默认值也可能通过 image offset 懒读，必须纳入 compact 区段。
- AOT `SuperSet` 模式也存在 `GetMethodBody` 路径，需要确认补元数据 DLL 的 method body 是否会被实际读取。
- 指针重定向必须覆盖所有从 `_imageData` 派生的长期指针。

结论：推荐主方案。它收益明确，风险可通过单元测试、ECMA335 和真机登录验证覆盖。

### 方案 C：运行时从文件或 APK asset 懒加载，不做 native CopyBytes

做法：

- C# 侧传入可定位文件路径或 asset 描述。
- native 侧按需映射或读取 DLL 区段。

收益：

- 理论上可以进一步降低 native 私有内存。

风险：

- Android APK asset 可能压缩，StreamingAssets、persistentDataPath、Dolphin 更新包路径不同。
- `Assembly.Load(byte[])` 的标准接口不直接暴露文件生命周期。
- 需要跨 C#、Android asset、HybridCLR native 接口设计，侵入更大。

结论：不作为第一阶段方案。只有当方案 B 收益不足或验证失败时再评估。

## 推荐设计

采用方案 B，并分两步实施：

1. Hotfix/AOT 共用 RawImage compact 基础能力。
2. 根据真机数据决定是否继续做 `InterpreterImage::InitRuntimeMetadatas` 结构压缩或按需化。

第一步不试图一次性解决 `il2cpp_meta/ClassInit`、泛型缓存和反射缓存，因为这些区域行为复杂，ECMA335 风险更高。

## 组件设计

### RawImageBase

新增职责：

- 记录原始 PE 中各类长期使用区段。
- 在 load 完成后构建 compact buffer。
- 释放完整 PE 原始 buffer。
- 将内部 stream/table/section 读取所需指针转为 compact buffer 指针。

需要重点覆盖的长期读取入口：

- `GetStringFromRawIndex`
- `GetBlobFromRawIndex`
- `GetBlobReaderByRawIndex`
- `GetDataPtrByImageOffset`
- `GetFieldOrParameterDefalutValueByRawIndex`
- `Read*` metadata table 方法族

### InterpreterImage

不改变解释器执行模型。它继续通过 `RawImageBase` 读签名、字符串、UserString、method body、custom attribute、P/Invoke 信息等。

需要验证：

- `MethodBodyCache::GetMethodBody` 首次读取和缓存收缩后再次读取都能正常工作。
- `HiTransform::Transform` 读取 IL body 正常。
- 默认值、FieldRVA、CustomAttributeDataRange、P/Invoke 名称读取正常。

### AOTHomologousImage / SuperSetAOTHomologousImage

不改变 AOT 补充元数据的匹配逻辑。`SuperSet` 仍按类型、方法、字段签名建立映射。

需要额外验证：

- `GetMethodBody` 在 AOT 泛型补充解释路径中仍能读取补元数据 method body。
- AOT 元数据 compact 后不影响泛型方法、字段签名、类型解析。

## 数据流

优化后目标数据流：

```text
托管 byte[]
   |
   v
native CopyBytes 原始 PE
   |
   v
RawImageBase::Load 解析 PE/metadata
   |
   v
收集长期懒读区段 -> 构建 compact buffer -> 重定向内部指针
   |
   v
释放完整 PE buffer
   |
   v
运行时继续通过 RawImageBase 读取 compact buffer
```

## 错误处理

- compact 失败时返回加载错误，不静默回退到错误状态。
- debug 构建中增加边界断言，确保重定向后的指针都落在 compact buffer 内。
- 如果某个 RVA 或 blob offset 无法映射到 compact buffer，直接触发可定位的错误日志。
- 失败日志需要包含 image 名称、区段类型、原始 offset、长度。

## 测试计划

### 单元测试

- 在 ECMA335 工程中新增或复用覆盖：
  - method body 读取和解释执行
  - 泛型方法和泛型类
  - 异常处理表
  - 字符串和 UserString
  - const 默认值
  - FieldRVA 静态数据
  - custom attribute
  - P/Invoke ImplMap 元数据
  - AOT SuperSet 补充元数据

### 集成测试

- 同步 AppIl2cpp 修改到：
  - `D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher`
- 按 `D:\dr2\Misc\Ecma335UnitTest\AGENTS.md` 跑 ECMA335 单元测试。
- 构建 FS 安卓包并在 `1C111FDF600AW5` 真机启动。
- 等待 logcat 输出 `登录场景完成`。
- 采集并分析 native heap，保存采集目录。

### 性能验证

- 采集工具需要调整为：
  - Perfetto/heapprofd 先于 App 启动。
  - 重启目标 App。
  - 等待 `登录场景完成`。
  - 登录完成后保留稳定期再结束。
  - 保存 logcat。
  - `heap_meminfo_validation` 通过，或明确修正验证口径并在报告中说明。

## 验收标准

必须同时满足：

1. 优化前有真实证据，包含 trace、logcat、heap 分类、文件尺寸闭合。
2. 优化后同口径采集显示 `il2cpp_meta + hybridclr` 合计常驻内存降低 50%。
3. `hybridclr/runtime` 中 `CopyBytes` 对应原始 DLL/AOT 常驻显著下降。
4. 真机 `1C111FDF600AW5` 启动并输出 `登录场景完成`。
5. ECMA335 单元测试通过。
6. 修改同步到 FSPatcher，并保留同步记录。
7. 文档更新完成，说明实现逻辑、使用方式、验证要求和风险边界。

## 暂不纳入范围

- 不优化整进程 PSS。
- 不把 heapprofd malloc 验证加回无栈 mmap 验证。
- 不使用 `adb monkey` 触发随机业务操作。
- 不在第一阶段改 `il2cpp_meta/ClassInit`、泛型共享策略或大规模反射缓存。
- 不优先改 Android asset 文件映射加载接口。

## 待用户复审

本设计确认后，下一步进入实施计划编写。实施计划需要拆分为：

1. RawImage compact 最小可验证实现。
2. ECMA335 覆盖和 FSPatcher 同步。
3. FS 安卓构建与真机验收采集。
4. 根据结果决定是否进入第二阶段 runtime metadata 结构优化。
