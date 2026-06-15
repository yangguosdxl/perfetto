# mmap 真实物理内存归因
mmap_phys_analyzer.md
当修改了代码后，要运行**无栈 mmap** 验证，但如果涉及调用栈的修改，就不用验证了，而是使用主功能验证。
需要触发测试数据或采集样本时，不要使用 `adb monkey` 触发随机事件；应提醒用户在手机上按目标场景手动操作。
无栈 mmap 验证只检查 mmap syscall events + smaps 健康状态，不启用 heapprofd malloc，也不做 malloc/native heap 对比。
无栈 mmap 验证必须先重启目标 App，并让 Perfetto 先于 App 启动，避免启动期 mmap 漏采。
无栈 mmap 验证需要跑 45 秒；如果出现告警或验证失败，必须分析原因并尝试修复，不能只记录失败结果。
需要验证 Perfetto malloc 数据统计功能时，改用独立 demo，不要把 heapprofd malloc 验证加回无栈 mmap 验证。
独立 malloc 分配量 demo 不要叠加超深调用栈验证，避免调用栈展开成本干扰 malloc 分配量口径；超深栈需要单独场景再做。
修改代码后必须同步更新相关文档，确保实现逻辑、使用方式、验证要求和文档说明保持一致。
查Bug时要以perfetto源码为依据

# native heap
run_heap_profile.sh
如果是在 AI 中验证 `run_heap_profile.sh`，必须加上 duration 参数运行：`00wann/run_heap_profile.sh 45000`。采集时长固定为 45 秒，确保命令自动退出，不依赖人工 Ctrl+C 中断。人工默认采集不加时长限制。
评估 Native heap profile 对启动耗时影响时，使用 `run_heap_startup_eval.sh`，启动完成点必须按 logcat 出现 `LAN 更新流程开始` 判断；不能用 `am start -W` 的 Activity 可见时间替代业务启动完成时间。
查Bug时要以perfetto源码为依据

# 目录说明
- PerfData 性能数据，无源代码

## demo
- 00wann/heapprofd_malloc_apk_demo

### fs app（封神应用）
这是要进行Native内存分析的正式应用
- 使用 unity2022.3.62 构建
- 应用的 il2cpp 运行时源码路径(称之为 `AppIl2cpp`)：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP`
- 应用的 unity3d 工程(称之为 `FS客户端工程`)：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj`
- 应用导出安卓工程(称之为 `FS安卓工程`)：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler`
- unity3d 工程打包时使用的il2cpp(称之为 `OriginIl2cpp`)：
- unity3d 引擎源代码：`D:\wann\u3d2019`，因为不是构建应用使用的 unity2022.3.62，所以只作为研究引擎实现的**参考**
- gradle: `D:\bin\gradle-7.5.1`
- unity2022.3.62安装路径：`D:\Program Files\Unity 2022.3.62f3`，sdk,ndk,java都使用这里的
- 真机
