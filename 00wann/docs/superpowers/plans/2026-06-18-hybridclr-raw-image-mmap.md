# HybridCLR Raw Image Mmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 FS 安卓低内存模式下，让 HybridCLR Hotfix/AOT 原始 DLL 字节优先从真实文件路径 mmap 懒读，避免长期 `CopyBytes` 常驻拷贝。

**Architecture:** C# 层只在低内存模式尝试文件路径入口；AOT 补元 metadata 复用 `Catalogue` / `MapFileLoader` 取得校验后的 `data` 路径；native 层新增文件加载 internal call，并让 `RawImageBase` 按 backing 类型释放 `HYBRIDCLR_FREE` 或 `FSNative::SysCallWrapper::MunmapFile`。

**Tech Stack:** Unity 2022.3、HybridCLR、IL2CPP FSPatcher、FSNative mmap、PowerShell 静态验证脚本。

---

## 文件结构

- 新增：`00wann/scripts/verify_hybridclr_mmap_static.ps1`
  - 负责静态检查 C#、native、打包脚本和 ECMA335 FSPatcher 同步结果。
- 修改：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Assets\Scripts\Main\GameAppHybridCLRManager.cs`
  - 低内存模式下接入 Hotfix 文件 mmap 和 AOT Catalogue mmap。
- 修改：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Packages\com.code-philosophy.hybridclr\Runtime\RuntimeApi.cs`
  - 增加文件路径 internal call。
- 修改：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\libil2cpp\hybridclr\RuntimeApi.*`
  - 注册并实现文件路径 internal call。
- 修改：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\libil2cpp\hybridclr\metadata\Assembly.*`
  - 新增 Hotfix/AOT 从 mmap 文件加载入口。
- 修改：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\libil2cpp\hybridclr\metadata\RawImageBase.*`
  - 增加 RawImage backing store 类型和析构释放分支。
- 修改：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\libil2cpp\fs-native\SysCallWrapper.*`
  - 抽出 `MmapFileFromPath`，供 HybridCLR native 复用现有 mmap/munmap 跟踪表。
- 修改：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Assets\Editor\BuildPackage.HybridCLR.cs`
  - AOTAssemblies 拷贝完成后生成 `Catalogue.bytes`。
- 修改：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Assets\Editor\BuildPackage.cs`
  - 增加手动菜单和总构建阶段的 AOT metadata catalogue 兜底。
- 同步：`D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher`
  - 同步 native FSPatcher 修改。

## 任务

### Task 1: RED 静态验证

- [ ] 新增 `00wann/scripts/verify_hybridclr_mmap_static.ps1`。
- [ ] 检查项覆盖：
  - `RuntimeApi.cs` 存在 `LoadMetadataForAOTAssemblyFromFile` 和 `LoadHotfixAssemblyFromFile`。
  - `GameAppHybridCLRManager.cs` 引用 `DeviceQualityUtil.IsLowMemory`、`MapFileLoader`、`CanUseMemoryMappedFile`。
  - `RawImageBase` 存在 mmap backing owner，并在析构时调用 `FSNative::SysCallWrapper::MunmapFile`。
  - `RuntimeApi.cpp` 注册两个文件路径 internal call。
  - `Assembly.cpp` 新增文件路径加载入口，且 AOT 文件入口不调用 `CopyBytes`。
  - `BuildPackage.HybridCLR.cs` 为 `AOTAssemblies` 生成 `Catalogue.bytes`。
  - ECMA335 FSPatcher 同步同名 native 变更。
- [ ] 运行脚本，预期当前失败，证明测试能捕获缺失实现。

### Task 2: native mmap 生命周期

- [ ] 在 `SysCallWrapper` 中抽出 `MmapFileFromPath(const char*, int32_t*)`。
- [ ] 在 `RawImageBase` 中增加 `RawImageDataOwner`，默认 `HybridClrMalloc`。
- [ ] 将析构从 header 移到 cpp，按 owner 释放。
- [ ] 让 `InterpreterImage::Load`、`AOTHomologousImage::Load` 透传 owner。
- [ ] 保持现有 byte[] 路径仍使用 `CopyBytes + HybridClrMalloc`。

### Task 3: native 文件入口

- [ ] `Assembly` 增加 `LoadFromFile(const char*)` 和 `LoadMetadataForAOTAssemblyFromFile(const char*, HomologousImageMode)`。
- [ ] `RuntimeApi` 增加 `LoadHotfixAssemblyFromFile(System.String)` 和 `LoadMetadataForAOTAssemblyFromFile(System.String, HomologousImageMode)` internal call。
- [ ] 文件 mmap 失败返回可定位错误或抛异常；一旦 mmap 入口已尝试，不回退到完整 `CopyBytes`。

### Task 4: C# 低内存接线

- [ ] `GameAppHybridCLRManager` 增加 `ShouldUseHybridCLRMmap()`，仅 Android 非 Editor 且 `DeviceQualityUtil.IsLowMemory()` 时启用。
- [ ] Hotfix：仅在 `DEBUG/CHEAT` persistent 路径、`GCLOUD_DOLPHIN` persistent Hotfix 路径，或 `Application.streamingAssetsPath` 下存在真实文件时调用 mmap 文件入口。
- [ ] AOT：低内存时初始化 `MapFileLoader(Application.persistentDataPath + "/AOTAssemblies", ...)`，用 `CanUseMemoryMappedFile(assemblyName, out path, out size)` 成功后调用 AOT 文件入口。
- [ ] 非低内存、无真实文件路径、Catalogue 校验未通过时保留原 `byte[]` 路径。

### Task 5: 打包 Catalogue

- [ ] `CopyAOTAssembliesToAssets` 拷贝完成后对目标 `AOTAssemblies` 目录运行 `BuildPackageCatalogueProcess.GenerateCatalogue()`。
- [ ] `BuildPackage` 增加 `GenerateAOTMetadataCatalogue()` 菜单，便于本地手动重建。
- [ ] 总构建阶段在字体/表格 catalogue 之后调用 AOT catalogue 兜底；如果目录不存在只输出日志，不阻断非 HybridCLR 构建。

### Task 6: 同步与验证

- [ ] 同步 AppIl2cpp FSPatcher native 文件到 ECMA335 FSPatcher。
- [ ] 运行 `00wann/scripts/verify_hybridclr_mmap_static.ps1`，预期通过。
- [ ] 修改 `.cs` 后按项目规则运行 `.claude\skills\fs-compile-client-dll\compile_check.ps1`。
- [ ] 如环境允许，再运行 ECMA335 测试；若本机依赖不足，在最终回复中明确未执行原因。

## 验收标准

- 低内存模式下，AOT metadata 通过 Catalogue 校验后的真实 `data` 文件路径进入 native mmap 文件入口。
- 低内存模式下，Hotfix 只有真实可读文件路径时进入 mmap 文件入口。
- 非低内存模式保持 `byte[] -> CopyBytes` 原路径。
- mmap backing 的 RawImage 析构走 `MunmapFile`，byte[] backing 仍走 `HYBRIDCLR_FREE`。
- AOT metadata 打包产物包含 `AOTAssemblies/Catalogue.bytes`。
- AppIl2cpp FSPatcher 与 ECMA335 FSPatcher native 修改保持同步。
