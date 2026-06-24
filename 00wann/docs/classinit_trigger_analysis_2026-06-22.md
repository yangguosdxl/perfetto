# ClassInit 大量触发逻辑拆解

日期：2026-06-22

## 结论

本次 `il2cpp_meta/ClassInit` 大，不是 `Class::Init` 自身单点泄漏，而是启动期多个业务入口集中触达未初始化类型，导致 il2cpp 在 `Class::InitLocked` 内一次性 materialize 类型运行时元数据。

基线样本：

```text
D:\dr2\Misc\perfetto\00wann\PerfData\mem\2026-06-22_15-37-06
```

ClassInit 查询结果：

```text
symbol: il2cpp::vm::Class::Init
net_alloc_mib: 108.903
net_alloc_count: 8401
matched_allocation_callsites: 37359
```

已展开 Top80 调用栈合计 `43.138 MiB`。剩余约 `65.8 MiB` 是更长尾的类型初始化分配。

## 总体触发模型

```text
启动/资源初始化完成
  |
  +-- 热更初始化、模块 Init、表格加载、协议注册、UI 注册
        |
        +-- 反射/类型判断/解释器 token 解析/运行期 metadata 初始化
              |
              +-- il2cpp::vm::Class::Init
                    |
                    +-- InitLocked
                          |
                          +-- SetupInterfacesLocked
                          +-- 父类 Init
                          +-- SetupMethodsLocked
                          +-- SetupTypeHierarchyLocked
                          +-- SetupVTable
                          +-- SetupFieldsLocked
                          +-- SetupEventsLocked / SetupPropertiesLocked / SetupNestedTypesLocked
                                |
                                +-- MetadataCalloc / MemoryPool / sparse hash / 泛型 Inflate
                                      |
                                      +-- 常驻 malloc live 增长
```

分类口径需要特别注意：`00wann/heap_analyzer/fs.ini` 中 `il2cpp_meta/ClassInit` 先匹配 `il2cpp::vm::Class::Init`，排在 `ClassSetupMethods` 之前。因此栈上同时出现 `Class::Init` 和 `SetupMethodsLocked` 时，会先归到 `ClassInit`。也就是说，`ClassInit` 包含 `InitLocked` 内部触发的方法、字段、虚表、接口、GC descriptor 等子阶段分配。

## 子阶段构成

Top80 中按子阶段粗分如下。不同分类会重叠，因为同一条栈可能同时包含 `GetMethodInfoFromToken`、`SetupInterfacesLocked`、`GenericClass::SetupMethods`。

| 子阶段 | Top80 栈数 | Top80 MiB | 说明 |
| --- | ---: | ---: | --- |
| `SetupMethodsLocked` | 12 | 16.193 | 普通类型方法表 materialize，分配 `MethodInfo*`、`MethodInfo`、参数类型数组。 |
| `GenericMethod/CreateMethod` | 28 | 11.241 | 泛型方法实例创建和缓存扩容。 |
| HybridCLR `Transform/GetMethodInfoFromToken` | 32 | 10.398 | 解释器转换 IL 指令时解析 call/callvirt/ldftn token，并初始化目标方法所属类。 |
| `GenericClass::SetupMethods` | 16 | 7.035 | 泛型实例类的方法指针数组和方法 Inflate。 |
| `SetupVTable` | 11 | 4.882 | 虚表、接口调度相关结构。 |
| `SetupFieldsLocked` | 6 | 3.997 | 字段布局和字段信息。 |
| `SetupInterfacesLocked` | 13 | 3.007 | 接口列表、接口偏移、接口继承链。 |
| `SetupGCDescriptor` | 9 | 2.873 | GC 扫描描述符。 |
| `ArrayMetadata::GetBoundedArrayClass` | 5 | 1.686 | 数组类型元数据。 |

运行时依据：

- `Class::InitLocked` 会串行执行接口、父类、方法、类型层级、虚表、字段、事件、属性、嵌套类型初始化。
- `SetupMethodsLocked` 对每个类型分配方法指针数组、`MethodInfo` 数组，并为每个方法分配参数类型数组。
- `GenericClass::SetupMethods` 对泛型实例按方法数分配数组，并逐个执行 `GenericMetadata::Inflate`。
- `MemoryPool` 的 region 由 malloc/calloc 得到，元数据池生命周期基本常驻。

## 1. UI 注册扫描触发

入口：

```text
HotFixFacade.Init
  -> AppFacade.UI.AddWindowFromAssembly(assembly)
  -> foreach assembly.GetTypes()
  -> type.IsSubclassOf(typeof(UIWindow))
  -> type.IsSubclassOf(typeof(UIFragment))
  -> typeof(IUISpineLoader).IsAssignableFrom(type)
  -> il2cpp::vm::Class::IsAssignableFrom
  -> il2cpp::vm::Class::Init
```

代码证据：

- `Assets/Scripts/GameApp/GameApp/HotFix/Base/HotFixFacade.cs:45` 取得热更程序集。
- `Assets/Scripts/GameApp/GameApp/HotFix/Base/HotFixFacade.cs:46` 调用 `AppFacade.UI.AddWindowFromAssembly(assembly)`。
- `Assets/Scripts/GameApp/Funny.GameFramework.Client.UI/UIManager.cs:233` 进入 `AddWindowFromAssembly`。
- `UIManager.cs:235` 遍历 `assembly.GetTypes()`。
- `UIManager.cs:238`、`UIManager.cs:239` 对每个类型执行 `IsSubclassOf`。
- `UIManager.cs:265` 对每个类型执行 `IsAssignableFrom`。

Perfetto 证据：

```text
Top80 UI AddWindowFromAssembly / IsAssignableFrom:
count=21
sum_mib=17.172
Top callsites: #1=7.929 MiB, #4=2.810 MiB, #7=0.874 MiB, #8=0.874 MiB
```

为什么会大：

`assembly.GetTypes()` 先拿到整个热更程序集的类型集合，后续不是只判断 UI 类型，而是对程序集内所有类型执行多轮类型关系判断。`IsSubclassOf` / `IsAssignableFrom` 在 il2cpp 侧需要检查父类和接口关系；如果目标类型、父类、接口或泛型实例尚未初始化，就会触发 `Class::Init`。一个类型被初始化时，不只是记录一个 Type 对象，还会拉起方法表、字段、虚表、接口表、GC descriptor 等结构。

这个入口的性质：

这是启动期集中扫描导致的峰值，不是 UI 打开某个窗口才触发的小范围初始化。它的优化方向通常是把“运行期全程序集扫描”改为打包期生成 UI 类型索引，或至少避免对所有类型执行多次 `IsSubclassOf/IsAssignableFrom`。

## 2. 协议注册触发

入口：

```text
NetCall.RegisterXxx
  -> DefaultNetwork.Registry.Register<某协议枚举>("E")
  -> ProtoRegister.Register(Type enumType, string prefix)
  -> Enum.GetNames / Enum.GetValues
  -> enumType.Assembly.GetType(typeName)
  -> Assembly::InternalGetType
  -> Class::Init(klass)
  -> baseType.IsAssignableFrom(cmdType)
  -> Activator.CreateInstance(cmdType) factory
```

代码证据：

- `Assets/Scripts/GameApp/CitrusNetFS/CitrusNet/Utils/ProtoRegister.cs:109` 进入 `Register(Type enumType, string prefix)`。
- `ProtoRegister.cs:116`、`ProtoRegister.cs:117` 枚举所有协议名和值。
- `ProtoRegister.cs:130` 首次 `enumType.Assembly.GetType(typeName)`。
- `ProtoRegister.cs:134` 命名空间 fallback 再次 `Assembly.GetType`。
- `ProtoRegister.cs:139`、`ProtoRegister.cs:144` 继续 fallback 到 `Type.GetType`。
- `ProtoRegister.cs:156` 执行 `baseType.IsAssignableFrom(cmdType)`。
- `ProtoRegister.cs:175` 保存 `Activator.CreateInstance(cmdType)` 工厂。
- `Assets/Scripts/GameApp/GameApp/HotFix/Proto/NetCall/BattleNetCall.cs:66` 到 `:72` 是单个模块连续注册多个协议枚举的例子。

il2cpp 运行时证据：

- `libil2cpp/icalls/mscorlib/System.Reflection/Assembly.cpp:146` 通过名称找到 `klass`。
- `Assembly.cpp:150` 明确调用 `il2cpp::vm::Class::Init(klass)`。
- `Assembly.cpp:152` 再取 `Class::GetType(klass, info)` 返回反射 Type。

Perfetto 证据：

```text
Top80 ProtoRegister / Assembly.InternalGetType:
count=4
sum_mib=5.871
Top callsites: #2=4.747 MiB, #15=0.562 MiB, #24=0.375 MiB, #55=0.187 MiB
```

为什么会大：

协议注册按枚举名推导命令类名，并对每个枚举项做类型查找。`Assembly.GetType` 在 il2cpp 下不是轻量字符串查表；它找到类后会立即 `Class::Init(klass)`。如果协议枚举很多，每个枚举项对应一个命令类型，这条链会把大量命令类和相关泛型/基类/接口一次性初始化出来。

这个入口的性质：

这是协议系统的启动期批量反射注册。优化方向通常是生成协议 id 到类型/工厂的静态注册表，避免按枚举名运行期 `Assembly.GetType`；或者将注册拆成按模块/场景懒注册。

## 3. 表格反序列化触发

入口：

```text
TableLoader
  -> ChannelLink
  -> await foreach reader.ReadAllAsync(...)
  -> manager.DeserializeData(...)
  -> PetConfigManager.DeserializeData
  -> PetConfigArray.Deserialize(bytes)
  -> il2cpp_codegen_initialize_runtime_metadata
  -> GlobalMetadata::InitializeRuntimeMetadata
  -> GetTypeInfoFromTypeIndex / InitFromCodegenSlow
  -> Class::Init
```

代码证据：

- `Assets/Scripts/GameApp/GameApp/DesignTable/TableLoader.cs:442` 定义 `DeserializeHandle`。
- `TableLoader.cs:445` 调用 `manager.DeserializeData(item.data, item.savePath)`。
- `TableLoader.cs:520` 到 `:529` 通过 `ChannelLink` 消费 `reader.ReadAllAsync` 并执行处理函数。
- `TableLoader.cs:622` 多线程路径调用 `manager.DeserializeData(tempBytes, lazyCachePath)`。
- `TableLoader.cs:640` 非多线程路径调用 `manager.DeserializeData(textAsset.bytes, lazyCachePath)`。
- `Assets/Scripts/TableDR_CS/NotHotfix/Gen/PetConfigManager.cs:85` 是生成的 `DeserializeData`。
- `PetConfigManager.cs:104` 非 lazy 路径调用 `PetConfigArray.Deserialize(bytes)`。
- `PetConfigManager.cs:122` 构建 `Dictionary<Int64, int>` 索引。

Perfetto 证据：

```text
Top80 Table Deserialize / PetConfig / RuntimeMetadata:
count=9
sum_mib=4.302
Top callsites: #3=2.960 MiB, #26=0.333 MiB, #42=0.246 MiB
```

代表栈：

```text
GenericClass::SetupMethods
  <- GlobalMetadata::InitializeRuntimeMetadata
  <- il2cpp_codegen_initialize_runtime_metadata
  <- PetConfigArray_Deserialize
  <- PetConfigManager_DeserializeData
  <- ReadAllAsync MoveNext
  <- ThreadPoolWorkQueue_Dispatch
```

为什么会大：

表格生成代码和 protobuf 反序列化代码里存在大量类型 token、泛型容器、数组、列表、字典、委托和解析方法引用。第一次执行这些生成方法时，AOT/HybridCLR 运行时需要把代码里引用的 runtime metadata token 转成可用的 `Il2CppClass` / `MethodInfo`。如果 token 对应的类没初始化，就进入 `Class::Init`。表格加载又是启动期批量执行，所以会集中触发。

这个入口的性质：

`ReadAllAsync` 是调度/消费通道，不是根因；根因是表格反序列化第一次执行大量生成代码，触发 runtime metadata materialize。优化方向一般是延迟加载表格、减少启动必载表数量、复用/缓存表格索引，或在生成代码层减少不必要的泛型/反射形态。

## 4. HybridCLR 解释器 Transform/token 解析触发

入口：

```text
hybridclr::interpreter::Interpreter::Execute
  -> TransformContext::TransformBodyImpl
  -> 解析 CALL / CALLVIRT / LDFTN / LDVIRTFTN 等 IL 指令
  -> image->GetMethodInfoFromToken(...)
  -> ReadMethodInfoFromToken(...)
  -> Class::Init(method->klass)
```

源码证据：

- `Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/libil2cpp/hybridclr/transform/TransformContext.cpp:2683` 进入 `TransformBodyImpl`。
- `TransformContext.cpp:3194` 处理 `CALL` 时调用 `image->GetMethodInfoFromToken`。
- `TransformContext.cpp:3376` 处理 `CALLVIRT` 时调用 `image->GetMethodInfoFromToken`。
- `TransformContext.cpp:5664` 处理 `LDFTN` 时调用 `image->GetMethodInfoFromToken`。
- `TransformContext.cpp:5677` 处理 `LDVIRTFTN` 时调用 `image->GetMethodInfoFromToken`。
- `Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/libil2cpp/hybridclr/metadata/Image.cpp:1145` 进入 `Image::GetMethodInfoFromToken`。
- `Image.cpp:1158` 明确执行 `il2cpp::vm::Class::Init(method->klass)`。

Perfetto 证据：

```text
Top80 HybridCLR Transform / GetMethodInfoFromToken:
count=32
sum_mib=10.398
Top callsites: #6=1.624 MiB, #9=0.812 MiB, #11=0.687 MiB, #17=0.541 MiB
```

为什么会大：

HybridCLR 解释器第一次转换一个方法体时，会扫描 IL 指令并解析方法 token。每个方法 token 都要拿到目标 `MethodInfo`，当前实现还会初始化 `method->klass`。如果启动期执行了大量热更方法，尤其是表格、协议、模块初始化、UI 扫描这类方法，Transform 会把许多被调用方法所属类拉进 ClassInit。

这个入口的性质：

这是解释器首次执行/首次转换成本，不是单独业务 API 的内存泄漏。优化方向需要非常谨慎，因为 `GetMethodInfoFromToken` 的 `Class::Init(method->klass)` 可能承担运行时正确性保证；如果要改，必须做 ECMA335 和热更调用语义验证。

## 5. 线程池 `ReadAllAsync` 路径

入口：

```text
ThreadPoolWorkQueue_Dispatch
  -> MoveNextRunner_Run
  -> ReadAllAsync MoveNext
  -> 表格 Deserialize / HybridCLR Transform / runtime metadata
  -> Class::Init
```

Perfetto 证据：

```text
Top80 ThreadPool ReadAllAsync 上游:
count=24
sum_mib=13.583
```

为什么会出现：

表格加载使用异步 channel 和线程池消费任务，所以很多 ClassInit 栈尾部都能看到 `ReadAllAsync`、`ThreadPoolWorkQueue_Dispatch`、`worker_thread`。它说明 ClassInit 发生在线程池上的表格加载/反序列化阶段。

这个入口的性质：

它是上游调度形态，不是直接根因。不能简单把 `ReadAllAsync` 当作内存问题；真正触发 malloc 的仍是下游的反序列化代码、runtime metadata 初始化、HybridCLR token 解析、泛型方法 Inflate。

## 6. 资源初始化协程路径

入口：

```text
UnityWebRequest / ResourceIniter
  -> ResourceIniter_OnLoadPackageVersionListSuccess
  -> ResourceManager_OnIniterResourceInitComplete
  -> InitResourcesCompleteCallback
  -> HotFixFacade.Init / GameModuleManager.Init / UI 注册 / 协议注册
  -> Class::Init
```

Perfetto 证据：

```text
Top80 Resource init coroutine 上游:
count=41
sum_mib=27.283
```

为什么会出现：

资源初始化完成后，游戏进入热更初始化和模块初始化阶段。UI 扫描、协议注册、表格加载等都在这个阶段密集发生，所以很多 ClassInit 栈尾部都共享 `ResourceManager_OnIniterResourceInitComplete`、`ResourceIniter_OnLoadPackageVersionListSuccess`、`LoadBytesCo`、Unity PlayerLoop。

这个入口的性质：

这是启动流程上的汇聚点，不是单一优化点。它解释了为什么 ClassInit 在登录前后集中出现：资源准备完成后，业务系统一次性注册、加载、反序列化，导致未初始化类型集中 materialize。

## 7. GameModuleManager 初始化路径

入口：

```text
GameModuleManager.Init
  -> CallFuncInOrder
  -> 各 GameModule.Init / InitAfter
  -> NetCall.Register / 表格初始化 / UI 初始化等
  -> Class::Init
```

代码证据：

- `Assets/Scripts/Funny.GameFramework/Core/Moudle/GameModuleManager.cs:37` 定义 `CallFuncInOrder`。
- `GameModuleManager.cs:53` 进入 `Init`。
- `GameModuleManager.cs:57` 遍历所有模块。
- `GameModuleManager.cs:63` 读取 `Init` 方法的 `CallOrderAttribute`。
- `GameModuleManager.cs:45` 执行具体模块初始化函数。

Perfetto 证据：

```text
Top80 GameModuleManager init 上游:
count=6
sum_mib=6.162
```

为什么会出现：

模块管理器按顺序调用所有模块初始化。协议注册、表格相关初始化、管理器构造和回调订阅都可能在模块 `Init` 中发生。它本身只是调度框架，但会把多个模块的“第一次触达类型”集中在同一个启动窗口内。

这个入口的性质：

这是启动期集中化调度导致的 ClassInit 峰值放大器。优化方向不是删掉模块管理器，而是把模块 Init 内的重型注册和反序列化拆成按需或分阶段执行。

## 关键判断

1. `ClassInit` 大的直接原因是大量未初始化类型首次被反射、类型判断、token 解析、runtime metadata 初始化触达。
2. 最大的业务触发侧是 UI 全程序集扫描和协议枚举反射注册；表格反序列化和 HybridCLR Transform 是另一个主要触发簇。
3. `ReadAllAsync`、资源初始化协程、`GameModuleManager` 是上游调度路径，不应被误判为直接 malloc 根因。
4. 这些 malloc 多数来自 il2cpp metadata pool、泛型缓存、方法/字段/虚表结构，生命周期接近常驻，因此会体现在 malloc live 中。
5. 当前数据只能说明触发链和热点入口；如果要做优化，需要分别验证每个入口的行为正确性和启动阶段内存变化，不能用一个总量变化直接归因全部 ClassInit。

## 后续优化候选

优先级建议：

1. UI 注册：用打包期生成索引替换启动期 `assembly.GetTypes()` 全量扫描。
2. 协议注册：用生成表替换 `Enum -> Assembly.GetType -> IsAssignableFrom` 的运行期反射链。
3. 表格加载：减少启动必载表，或扩大 lazy table 覆盖范围，避免登录前集中反序列化。
4. HybridCLR token 初始化：作为运行时优化单独评估，必须配套 ECMA335、热更泛型、虚调用、委托、反射调用验证。
