# HybridCLR RawImage 文件 mmap 优化上下文

日期：2026-06-18

## 目标域

本上下文用于指导后续研究与实施计划：把 HybridCLR Hotfix/AOT 原始 DLL 字节的 native 常驻拷贝优化方向，从旧设计推荐的 RawImage compact 改为文件 mmap 懒读方案。

该优化只服务于 FS 安卓低内存模式下的 HybridCLR 原始字节常驻内存问题，不扩展为通用 APK asset、压缩 asset、任意流式输入或全局 PSS 优化。

## 已锁定决策

### 主方案

- 采用旧规格中的“方案 C：运行时从文件懒加载，不做 native CopyBytes”作为当前实现方向。
- 旧规格中推荐的“方案 B：Native RawImage compact”不再作为当前实施计划的主方案。
- 已存在的旧计划 `00wann/docs/superpowers/plans/2026-06-16-hybridclr-raw-image-memory.md` 是方案 B 计划，后续规划必须重写，不能直接沿用其中的 RawImage compact 任务。

### 输入边界

- 只支持可直接读取的文件系统路径。
- 不支持 APK asset 描述。
- 不支持压缩 asset。
- 不支持只存在于 `Assembly.Load(byte[])` 入参中的匿名托管字节。
- 不支持需要 Android asset manager 解压或拷贝后才能定位的路径。
- 路径生命周期必须由上层保证：native 侧持有映射期间，文件不得被删除、替换或截断。

### 读取方式

- native 侧使用 mmap 读取文件。
- mmap 应替代原始 DLL/AOT 字节的 native `CopyBytes` 长期常驻拷贝。
- RawImage 后续读取仍按逻辑 image offset、RVA、metadata stream、metadata table、method body、FieldRVA 等路径工作。
- 实现不得静默回退为完整 native heap 拷贝；如果 mmap 建立失败，应返回可定位错误。

### 启用条件

- 该优化只在低内存模式下启用。
- 非低内存模式保持现有 `byte[] -> CopyBytes -> RawImageBase::_imageData` 行为，降低行为变更风险。
- 低内存模式开关来源优先复用 FS/HybridCLR 现有配置；如果现有代码没有统一开关，计划阶段需要明确新增开关的位置、默认值和日志输出。

### 适用对象

- Hotfix DLL：覆盖热更程序集加载路径。
- AOT 元数据 DLL：覆盖 `RuntimeApi.LoadMetadataForAOTAssembly(bytes, SuperSet)` 对应的补充元数据加载路径。
- 二者都必须能从上层拿到可直接读取的真实文件路径后才进入 mmap 优化路径。

### Catalogue 复用

- AOT 补元用的 metadata 文件必须复用 FS 客户端现有 `Catalogue` / `MapFileLoader` 机制。
- 后续计划不应为 AOT metadata 另起一套文件校验、落盘和版本判断机制。
- 打包流程必须为 AOT metadata 目录生成对应 `Catalogue.bytes`，并确保它随包进入可被 `MapFileLoader.Initialize` 读取的位置。
- `Catalogue` 中的 `DataHash`、`FileSize`、`DisableMemoryMap`、`Version` 语义继续沿用现有实现；运行时只有在 `verified` 文件存在且 `data` 文件大小匹配时才允许 mmap。
- 计划阶段必须检查当前打包入口，因为现有 `BuildPackage` 只显式生成 Font、Table、TableCPP 的 catalogue，AOT metadata catalogue 需要补充到相关打包流程。

## 不纳入范围

- 不实现 APK asset mmap 或 asset manager 读取。
- 不处理压缩 StreamingAssets。
- 不把文件先复制到临时目录再 mmap，除非后续用户明确批准新增该能力。
- 不优化整进程 PSS。
- 不修改 `il2cpp_meta/ClassInit`、泛型缓存、大规模反射缓存。
- 不把 heapprofd malloc 验证加回无栈 mmap 验证。
- 不使用 `adb monkey` 触发随机业务操作。

## 关键实现约束

1. C# 层必须新增或改造加载入口，让 Hotfix DLL 与 AOT 元数据 DLL 在低内存模式下传递真实文件路径。
2. native 层必须新增面向文件路径的 RawImage 加载入口，避免依赖 `Assembly.Load(byte[])` 标准接口承载文件生命周期。
3. mmap buffer 的地址访问必须兼容 RawImage 当前懒读入口，包括 metadata streams、metadata tables、method body、FieldRVA、custom attribute、P/Invoke ImplMap。
4. mmap 生命周期必须与 `InterpreterImage`、`AOTHomologousImage`、`SuperSetAOTHomologousImage` 对齐，不能早于运行时 image 注销。
5. 错误日志必须包含 image 名称、文件路径、失败阶段、errno 或平台错误码、offset、长度。
6. debug 构建需要保留边界断言，确保 RawImage 访问不越过 mmap 文件长度。

## 需要研究的代码路径

### FS 客户端路径

- `FileHelper.ReadFileFromStreamingPath`
- `FSNative.MapFileLoader`
- `FSNative.SysCallWrapperApi.MmapFile`
- Hotfix DLL 加载入口
- AOT 元数据 DLL 加载入口
- 低内存模式配置或启动参数来源

### 打包路径

- `BuildPackage.GenerateFontCatalogue`
- `BuildPackage.GenerateTableCatalogue`
- `BuildPackageCatalogueProcess.GenerateCatalogue`
- AOT metadata / `AOTAssemblies` 产物生成与打包入口

### AppIl2cpp / HybridCLR 路径

- `Assembly.Load(byte[])`
- `Assembly::Create`
- `RuntimeApi.LoadMetadataForAOTAssembly`
- `LoadMetadataForAOTAssembly`
- `RawImageBase`
- `InterpreterImage`
- `AOTHomologousImage`
- `SuperSetAOTHomologousImage`

## 验收标准

必须同时满足：

1. 低内存模式下 Hotfix/AOT 走文件 mmap 路径。
2. 非低内存模式保持原行为。
3. AOT 补元 metadata 使用现有 `Catalogue` 机制完成版本、hash、大小和 `verified` 校验。
4. 打包产物包含 AOT metadata 的 `Catalogue.bytes`，且运行时能按 catalogue 将补元文件落盘到可 mmap 的 `data` 文件。
5. 只接受真实可读文件路径；传入 asset 描述、压缩 asset、缺失文件或不可读路径时输出可定位错误。
6. `hybridclr/runtime` 中 `CopyBytes` 对应 Hotfix/AOT 原始字节常驻显著下降。
7. `il2cpp_meta + hybridclr` 合计常驻内存目标仍按旧规格验收：优化后同口径采集应降低 50%，目标线约 `134.85 MiB`。
8. 真机 `1C111FDF600AW5` 启动 FS 安卓包并输出 `登录场景完成`。
9. ECMA335 单元测试通过。
10. AppIl2cpp 修改同步到 ECMA335 FSPatcher，并保留同步记录。
11. 相关文档更新完成，说明低内存模式开关、文件路径要求、mmap 生命周期、Catalogue 打包要求、验证方式和失败日志。

## 测试要求

### 单元测试

- 路径模式与字节模式分支选择。
- 低内存模式开启时使用 mmap 路径。
- 非低内存模式保留 `byte[]` 路径。
- AOT metadata catalogue 缺失、`DisableMemoryMap=true`、hash/大小不匹配、`verified` 缺失时不进入 mmap 路径。
- 文件不存在、不可读、长度为 0、映射失败、读取越界时返回可定位错误。
- method body、异常处理表、UserString、const 默认值、FieldRVA、custom attribute、P/Invoke ImplMap 在 mmap 后仍可读取。
- AOT `SuperSet` 补充元数据路径仍可解析泛型方法、字段签名、类型引用。

### 集成测试

- 按 `D:\dr2\Misc\Ecma335UnitTest\AGENTS.md` 跑 ECMA335 单元测试。
- 将 AppIl2cpp 修改同步到 `D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher`。
- 构建 FS 安卓包，安装到 `1C111FDF600AW5`。
- 检查打包产物中 AOT metadata catalogue 存在，并确认运行时落盘后的 `data` / `verified` 文件满足 `MapFileLoader` 校验。
- 等待 logcat 输出 `登录场景完成` 后稳定采集 30 秒。
- 采集 native heap 并分析 `il2cpp_meta + hybridclr`、`hybridclr/runtime`、`CopyBytes`。
- 对 mmap 相关改动按 `00wann/AGENTS.md` 运行无栈 mmap 验证；如果修改涉及调用栈采集本身，则改用主功能验证。

## 规范引用

- `00wann/docs/superpowers/specs/2026-06-16-hybridclr-raw-image-memory-design.md`：原始证据、旧方案对比、验收口径。
- `00wann/docs/superpowers/plans/2026-06-16-hybridclr-raw-image-memory.md`：旧方案 B 计划，仅作为反例和历史参考，不可直接执行。
- `00wann/AGENTS.md`：mmap 验证、native heap 验证、FS 真机与 ECMA335 约束。
- `00wann/README.md`：perfetto、mmap、native heap 工具说明。

## 后续计划输入

后续实施计划应拆分为：

1. 调研 FS 低内存模式与 Hotfix/AOT 真实文件路径来源。
2. 调研并复用现有 `Catalogue` / `MapFileLoader` 机制，明确 AOT metadata 的 persistent 落盘目录、catalogue 路径和打包生成入口。
3. 修改打包流程，为 AOT metadata 生成并打入 `Catalogue.bytes`。
4. 设计 C# 到 native 的文件路径加载 API，并保留非低内存模式原路径。
5. 实现 RawImageBase mmap backing store 与生命周期管理。
6. 覆盖 Hotfix 与 AOT 元数据两条加载路径，其中 AOT 元数据必须走 catalogue 校验后的 mmap 文件路径。
7. 增加 ECMA335、catalogue 打包、catalogue 失效和路径错误测试。
8. 同步 FSPatcher。
9. 构建 FS 安卓包并完成真机登录场景采集。
10. 若最终 `il2cpp_meta + hybridclr` 未达到 `134.85 MiB`，只报告 mmap 优化收益，不声明 50% 总目标完成，并另立第二阶段分析剩余大项。
