# Unity Shader 内存分析（2026-07-07）

## 结论

样本：

`D:\dr2\Misc\perfetto\00wann\PerfData\mmap_phys\2026-07-07_11-38-30\pprof_categories\19_unity3d_Shader.pprof.pb.gz`

`unity3d/Shader` 这一类在 pprof 中合计 **253.49 MiB PSS**。这不是一个单纯的“GPU 显存”数字：其中绝大多数是 App 进程里的 native heap / scudo 匿名映射，属于系统内存；另有一小部分是 OpenGL shader cache 文件映射；`/dev/mali0` 在这个 category 中为 0。

同时，`dumpsys_meminfo.txt` 里还有 memtrack 口径的图形内存：

| 项 | 数值 |
| --- | ---: |
| GL mtrack | 181,604 KB |
| EGL mtrack | 60,492 KB |
| Graphics 合计 | 242,096 KB |

这部分是 Android/memtrack 报告的 graphics memory，通常不以普通 smaps VMA 的形式出现在本次 mmap pprof 调用栈里。因此当前证据应拆成两层看：

1. **pprof 253.49 MiB**：可按 mmap 路径和调用栈归因，主要是 Unity shader 对象、shader blob、GLES/Mali 驱动 malloc 堆、OpenGL shader cache 文件页。
2. **Graphics 242,096 KB**：GL/EGL memtrack 图形内存，和 shader/program/纹理/渲染资源相关，但这份 pprof 不能把它精确拆到 shader 调用栈。

## pprof 路径拆分

命令：

```powershell
go tool pprof -top D:\dr2\Misc\perfetto\00wann\PerfData\mmap_phys\2026-07-07_11-38-30\pprof_categories\19_unity3d_Shader.pprof.pb.gz
go tool pprof -tags D:\dr2\Misc\perfetto\00wann\PerfData\mmap_phys\2026-07-07_11-38-30\pprof_categories\19_unity3d_Shader.pprof.pb.gz
```

按 `paths` 标签：

| 路径 | PSS | 占比 | 归属判断 |
| --- | ---: | ---: | --- |
| `[anon:scudo:primary]` | 190.23 MiB | 75.04% | App native heap，系统 RAM |
| `[anon:scudo:secondary]` | 43.94 MiB | 17.33% | App native heap，大块 malloc，系统 RAM |
| `com.android.opengl.shaders_cache.multifile/*` | 12.65 MiB | 4.99% | OpenGL shader binary cache 文件映射，文件页/系统 RAM page cache |
| `/dmabuf:system; [anon:scudo:secondary]` | 3.89 MiB | 1.53% | 混合路径，含 dmabuf system；Pixel 6 这类 UMA 设备上仍是系统内存承载 |
| 其他 shader cache / ashmem / scudo 混合 | 2.00 MiB | 0.79% | 小额混合映射 |
| 其他文件 / dalvik / scudo 混合 | 424.20 KiB | 0.16% | 小额混合映射 |
| `libPixUI_Unity.so` | 380 KiB | 0.15% | so 文件页 |
| `/dev/mali0` | 0 | 0% | 本 category 未归到 Mali device VMA |

所以，这 253.49 MiB 里最主要的不是 GPU device mapping，而是进程内 native heap。即使调用栈里有 `libGLES_mali.so`，路径也多落在 scudo heap，表示 Mali/GLES 驱动通过进程 malloc/scudo 保留了大量 driver-side program/link/cache 结构。

## 按调用栈粗分

按 `pprof -raw` 的 sample 栈做互斥归类，优先识别 OpenGL cache 文件、dmabuf、Mali/GLES 驱动栈，再识别 Unity shader blob 和 Unity serialized shader 对象。这个分组是分析口径，不是 pprof 原生标签，几 MiB 级别会随分类优先级变化。

| 粗分类 | PSS | 说明 |
| --- | ---: | --- |
| Mali/GLES driver heap | 约 107.5 MiB | `libGLES_mali.so`、`gles2_*`、`cpom*`、`gfx::*` 等驱动编译/link/program binary 结构，通过 scudo heap 承载 |
| Unity serialized shader runtime objects | 约 70.5 MiB | `Shader::CreateFromParsedForm`、`ShaderLab::Program::CreateFromSerializedProgram`、`ShaderLab::SubProgram::*`、SRP batcher shader 数据等 |
| Unity shader blob decompress/prepare | 约 37.0 MiB | `ShaderBinaryData::Decompress/SetData/GetBlobData/PrepareChunk`，对应 Unity 2022/fork 中 shader blob 的解压、准备和读取 |
| OpenGL shader cache file mappings | 约 15.1 MiB | `com.android.opengl.shaders_cache.multifile/*` 文件映射 |
| AssetBundle/PersistentManager/Remapper/loading | 约 12.5 MiB | 资源加载、InstanceID remap、读取结构等和 shader asset 加载绑定的开销 |
| dmabuf/devmali mixed | 约 3.9 MiB | 小额 `/dmabuf:system` 混合路径 |
| Other / mixed | 约 7.1 MiB | Mesh/UI/字符串/其他混合栈，仍被分类进 shader category |

pprof top 里几个关键累计栈：

| 栈节点 | 累计 PSS | 含义 |
| --- | ---: | --- |
| `GlslGpuProgramGLES::CompileProgramImpl` | 121.40 MiB | GLES program 创建、cache 加载、fallback compile/link |
| `GlslGpuProgramGLES::LoadFromBinaryShaderCache` | 107.76 MiB | 从二进制 shader cache 加载后仍触发大量驱动侧 program/link 结构 |
| `CreateGpuProgram` | 89.83 MiB | Unity 创建 GPU program 的入口 |
| `Shader::CreateFromParsedForm` | 73.47 MiB | 从 serialized shader 构造运行时 shader 树 |
| `ShaderBinaryData::Decompress` | 27.26 MiB | shader blob 解压 |
| `ShaderBinaryData::GetBlobData` / `PrepareChunk` | 20.84 MiB | 运行时按 blob 取 variant/subprogram 数据 |
| `ShaderVariantCollection::WarmupShaders*` | 12.35 MiB | warmup dummy draw 触发 variant 编译/加载 |

## Unity 2019 源码对应关系

参考源码根目录：`D:\wann\u3d2019`。正式 App 是 Unity 2022.3.62，2019 源码只能作为实现链路参考；本样本中的 `ShaderBinaryData::*` 符号在 2019 源码中没有同名实现，应视为 2022/fork 中对应 `m_SubProgramBlob` / blob 读取准备逻辑的演进版本。

关键链路：

1. `Runtime/Shaders/Shader.h:252-275`
   - `Shader` 持有 `m_Shader`、`m_SubProgramBlob`、`m_ParsedForm`。
   - `m_SubProgramBlob` 注释说明其保存 GPU program data 的 binary blobs。
2. `Runtime/Shaders/Shader.cpp:1231-1265`
   - `Shader::CreateFromParsedForm()` 调 `ShaderFromSerializedShader(...)` 创建运行时 `IntShader`。
   - Player 分支注释写明理论上可删除 parsed form、清理 subprogram blob，但实际 `UNITY_DELETE(m_ParsedForm, ...)` 和 `m_SubProgramBlob.clear_dealloc()` 两行是注释状态。
3. `Runtime/Shaders/Shader.cpp:1515-1528`
   - 读取平台、offset、压缩长度、解压长度和 compressed blob 后，调用 `DecompressSubprogramBlob(..., m_SubProgramBlob, ...)`。
4. `Runtime/Shaders/Shader.cpp:1680-1710`
   - `Shader::GetBlobData()` 从 `m_SubProgramBlob` 的 index table 取 blob offset/length。
5. `Runtime/Shaders/SerializedShaderData.h:267,590,847,1011,1252`
   - runtime serialized tree 是 `SerializedShader -> SerializedSubShader -> SerializedPass -> SerializedProgram -> SerializedSubProgram`，每层都有 vector/string/参数表。
6. `Runtime/Shaders/ShaderImpl/ShaderProgram.cpp:117-213`
   - `SubProgram::EnsureCompiled()` 未编译时调用 `Compile()`，复制 program code 和参数，再调用 `device.CreateGpuProgram(...)`。
7. `Runtime/GfxDevice/GpuProgram.cpp:823-850`
   - GLES/GLCore 类型走 `new GlslGpuProgramGLES(sourceCode, output)`。
8. `Runtime/GfxDevice/opengles/GlslGpuProgramGLES.cpp:556-735`
   - 先尝试 binary shader cache：`LoadFromBinaryShaderCache()`。
   - cache miss 或上传失败时 fallback 到 shader 编译、program link，然后 `StoreInBinaryShaderCache()`。
9. `Runtime/GfxDevice/opengles/GlslGpuProgramGLES.cpp:358-367`
   - 析构时 `Clear()` 删除 GL programs 并释放 `m_GLPrograms`。
10. `Runtime/GfxDevice/opengles/GlslGpuProgramGLES.cpp:493-548`
    - instancing array size 变化会 `SwitchProgram()`，patch shader source 并再编译一个 GL program，增加 `m_GLPrograms`。
11. `Runtime/Shaders/GpuPrograms/ShaderVariantCollection.cpp:669-699`
    - warmup 通过 dummy draw 触发 pass/subprogram 选择和编译。

这条链路解释了 pprof：加载 shader asset 时先膨胀 Unity runtime shader 对象和 blob；warmup 或首次 draw 时再触发 GPU program 创建；GLES/Mali 驱动为了 program binary、linker、uniform/location、pipeline cache 等继续在 native heap 和 memtrack 中占用内存。

## 系统内存、GPU 内存、是否常驻

### 系统内存

`[anon:scudo:primary]` 和 `[anon:scudo:secondary]` 占 **234.17 MiB**，这是明确的进程 native heap PSS，属于系统内存。

其中包括：

- Unity 自己的 `Shader` / `ShaderLab` runtime 对象。
- `m_ParsedForm`、`m_SubProgramBlob` 或 Unity 2022/fork 中的 `ShaderBinaryData`。
- `GpuProgramParameters`、uniform/texture/vector 参数表、SRP batcher shader 数据。
- Mali/GLES 驱动通过 malloc/scudo 分配的 program/link/cache 内部结构。

这部分是否常驻取决于对象生命周期。只要 Shader asset、Material、ShaderVariantCollection、global manager 或 AssetBundle 还引用这些 shader，对应 Unity 对象和 GL program 基本会继续存在。即使对象释放，scudo 也可能把页留在 allocator cache/free list，一段时间内 PSS 未必立刻下降。

### 文件页 / page cache

`com.android.opengl.shaders_cache.multifile/*` 约 **15.1 MiB** 是 OpenGL shader binary cache 文件映射。它消耗进程 PSS/RSS，但属于文件页；内核在压力下可以回收干净页，文件本体仍在 app cache 目录，之后再访问会重新 fault 进来。

这部分不应简单理解为“永久常驻显存”。禁用或清理 cache 可能降低 file-backed PSS，但会增加运行时编译/link 成本和卡顿风险。

### GPU / graphics memory

本 pprof 中 `/dev/mali0` 为 0；也就是说，这 253.49 MiB 里没有可直接归到 Mali device VMA 的 PSS。

但 `dumpsys meminfo` 报告：

- `GL mtrack = 181,604 KB`
- `EGL mtrack = 60,492 KB`

这部分是 graphics/memtrack 口径，在 Pixel 6 这类统一内存架构设备上通常仍由系统内存承载，只是归 Android graphics/memtrack，而不是普通 native heap VMA。它可能包含 GL program、shader compiler/driver resource、纹理、buffer、EGL context/surface 等，当前数据不能把它全部归给 shader。

## 为什么 shader 占这么多

主要原因不是单个 shader 对象很大，而是三类因素叠加：

1. **加载的 shader/variant 数量大**：每个 shader 反序列化后都有 subshader/pass/program/subprogram 层级，variant、keyword、pass 越多，native 对象越多。
2. **blob 和 parsed/runtime form 同时保留**：Unity 2019 源码显示 Player 中清 `m_ParsedForm`、`m_SubProgramBlob` 的代码被注释。Unity 2022/fork 的 `ShaderBinaryData::*` 栈也显示 blob 解压和准备占了明显内存。
3. **warmup/首次渲染触发驱动 program 创建**：`ShaderVariantCollection.WarmUp()` 或 dummy draw 会让 subprogram 编译/加载为 GLES program。Mali 驱动在 `glProgramBinary`、compile、link、uniform/location、pipeline cache 上通过 malloc/scudo 和 memtrack 保留大量内部状态。

## 优化建议

### 优先级 1：减少登录阶段加载和 warmup 的 variant

这是优先级最高、风险最低的方向。

- 盘点登录阶段加载了哪些 ShaderVariantCollection、材质和 shader asset。
- 不要在登录阶段 warmup 全局大集合；按场景/系统拆分 SVC，只 warm 登录首屏必需 variant。
- 非首屏、战斗、活动、特效、角色高阶材质等 variant 延后到对应系统打开前或空闲帧 progressive warmup。
- 对比同一路径的 pprof：重点看 `ShaderVariantCollection::WarmupShaders*`、`CreateGpuProgram`、`GlslGpuProgramGLES::*` 和 `GL/EGL mtrack` 是否下降。

### 优先级 2：减少 shader keyword 和 pass 组合

variant 数量是乘法问题。

- 把不需要运行时切换的 `multi_compile` 改成可被剔除的 `shader_feature`。
- 删除登录阶段不可能用到的 keyword、pass、renderer feature。
- 统一材质关键字，避免同一视觉效果因为开关组合差异生成大量 program。
- 检查 URP renderer feature、post process、UI shader、特效 shader 的登录阶段依赖。

### 优先级 3：检查 AssetBundle shader 重复和依赖打包

如果多个 AssetBundle 各自带同一批 shader 或 SVC，Unity 可能反复加载等价数据，造成 native heap 和驱动 program 增长。

- 把公共 shader、公共 SVC、公共材质依赖集中到 shared bundle。
- 让业务 bundle 依赖 shared bundle，避免重复内嵌 shader。
- 用同一登录场景做 A/B：只改打包依赖，比较 `unity3d/Shader` PSS、`Shader::CreateFromParsedForm`、`ShaderLab::*`。

### 优先级 4：控制 GLES program 数量和 instancing patch

`GlslGpuProgramGLES::SwitchProgram()` 会因 instancing array size 变化 patch shader source 并生成额外 GL program。

- 登录阶段尽量稳定 instancing batch size，避免同一个 shader 因多个 instancing array size 生成多份 GL program。
- 对 UI/登录场景材质，避免无必要开启 GPU instancing 或大数组 uniform。
- 简化 uniform arrays、texture 参数和常量缓冲布局，降低 `AddGpuProgramParameters`、`GpuProgramParameters::*` 这类 CPU 侧参数表开销。

### 优先级 5：谨慎评估释放 shader blob / parsed form

Unity 2019 源码里 Player 分支已经留下“可清理 parsed form 和 subprogram blob”的注释，但代码被注释，说明这个方向有历史风险。Unity 2022/fork 中需要先确认 `ShaderBinaryData::*` 的真实生命周期。

可行的实验方向：

- 在所有目标 variant 已经创建 `SubProgram/GpuProgram` 后，评估是否能释放对应 blob chunk。
- 为登录场景做 feature flag，只在确认不再 lazy-load 新 variant 时释放。
- 必须验证后续场景、fallback shader、热更资源、设备丢失/重建、graphics context 重建、SVC 二次 warmup 是否还需要原始 blob。

这个方向理论收益可能覆盖 `ShaderBinaryData::*` 的几十 MiB，但属于引擎改动，风险高于资源侧优化，不建议作为第一刀。

### 优先级 6：不要轻易禁用 binary shader cache

OpenGL shader cache 文件页只有约 15 MiB，且能减少编译/link 卡顿。禁用 cache 可能让 file-backed PSS 下降，但通常会让 `CompileProgramImpl`、driver compile/link 时间和启动卡顿恶化。

建议只做对照实验：

- 冷 cache、热 cache 分开测。
- 同时记录启动耗时、登录完成时间、卡顿和 `GL/EGL mtrack`。
- 除非确认 cache 对目标场景收益很低，否则不作为主要内存优化手段。

## 验证口径

资源或引擎优化后建议用同一口径对比：

1. 同一设备：Pixel 6 `1C111FDF600AW5`。
2. 同一场景路径：启动后自动进入登录场景，看到 `登录场景完成` 后稳定 30 秒再收尾。
3. 同一 pprof category：`pprof_categories/*unity3d_Shader.pprof.pb.gz`。
4. 同时记录 `dumpsys_meminfo.txt` 的 `Native Heap`、`GL mtrack`、`EGL mtrack`、`Graphics`。
5. 对比重点：
   - `unity3d/Shader` 总 PSS。
   - `[anon:scudo:*]` 是否下降。
   - `GlslGpuProgramGLES::*` / `CreateGpuProgram` 是否下降。
   - `Shader::CreateFromParsedForm` / `ShaderBinaryData::*` 是否下降。
   - `GL/EGL mtrack` 是否同步下降。

本次仅新增分析文档，没有修改采集或分析代码，因此不需要跑无栈 mmap 设备验证。
