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
如果是在 AI 中验证 `run_heap_profile.sh`，不要传 duration 参数，也不要用固定时长截断采集；必须等 logcat 依次出现 `登录场景完成` 和 `RegistForGameStart.LoadOtherTable.End`、登录后 GM RPC 成功并继续稳定采集 120 秒，再让脚本自动收尾。需要调整采样参数时使用 `00wann/run_heap_profile.sh <interval_bytes> <shmem_size>`。
评估 Native heap profile 对启动耗时影响时，使用 `run_heap_startup_eval.sh`，启动完成点必须按 logcat 出现 `LAN 更新流程开始` 判断；不能用 `am start -W` 的 Activity 可见时间替代业务启动完成时间。
查Bug时要以perfetto源码为依据

# 说明
- PerfData 性能数据，无源代码
- adb 端口转发使用 12346
- 真机测试**只能使用**手机：1C111FDF600AW5 (pixel 6)
- apk签名：D:\dr2\Trunk_LocalBuild\Tools\PublishClient\wann.keystore，密码 123456
## demo
- 00wann/heapprofd_malloc_apk_demo

## fs app（封神应用）
这是要进行Native内存分析的正式应用
- 使用 unity2022.3.62 构建
- 应用的 il2cpp 运行时源码路径(称之为 `AppIl2cpp`)：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP`
- 应用的 unity3d 工程(称之为 `FS客户端工程`)：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj`
- 应用导出安卓工程(称之为 `FS安卓工程`)：`D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler`
- unity3d 工程打包时使用的il2cpp(称之为 `OriginIl2cpp`)：
- unity3d 引擎源代码：`D:\wann\u3d2019`，因为不是构建应用使用的 unity2022.3.62，所以只作为研究引擎实现的**参考**
- gradle: `D:\bin\gradle-7.5.1`
- unity2022.3.62安装路径：`D:\Program Files\Unity 2022.3.62f3`，sdk,ndk,java都使用这里的
- 真机测试必须等 `RegistForGameStart.LoadOtherTable.End` 后再触发登录后 GM；GM RPC 成功后必须稳定采集 120 秒再收尾
- 安装新编译的apk时不要卸载真机上旧的apk
- 做优化时，如果涉及il2cpp，则要创建分支，确保修改隔离得比较干净。合并主干时要和我确认。
- 确认优化有效且ecma335测试都通过后，写验收报告，把appil2cpp同步到originil2cpp，并提交appil2cpp和originil2cpp
- 编译FS客户端工程后，清空`FS安卓工程`（保留.git），同步`$FS安卓工程/BuildCache\Published\Android\DreamRivakes2.apk`到`FS安卓工程`。
### ECMA335单元测试工程
- 每次对AppIl2cpp代码进行优化后，都要进行ecma335单元测试
- D:\dr2\Misc\Ecma335UnitTest\AGENTS.md
#### 测试流程
1. 将AppIl2cpp中il2cpp源码的修改同步到D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher，并提交fspatcher
2. 按D:\dr2\Misc\Ecma335UnitTest\AGENTS.md指导单元测试
