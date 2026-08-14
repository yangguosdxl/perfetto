# 通用真机性能测试框架

`device_test_framework` 子模块把 Native malloc 和 mmap 真实物理内存采集的公共外层流程统一为：

```text
配置加载
  -> 平台连接与运行级设置
  -> 功能插件校验
  -> 专业采集后端
  -> 功能与平台清理
  -> 统一报告
```

v1 每轮只运行一个主采集功能。这样不会把 heapprofd 和 mmap 调用栈采样任意叠加，
避免框架本身改变被测 App 的性能和内存口径。

## 仓库边界

```text
device_test_framework/                 独立 Git 子模块
  config / engine / reporting         通用配置、阶段引擎和报告
  platforms/android.py                Android 连接与运行级资源管理
  features/base.py / flows/base.py    通用扩展协议

device_test_plugins/                   00wann 项目代码
  malloc.py / mmap.py                 专业后端参数和 FS 文件准备
  environment.py                      旧变量兼容与项目后端环境
  perfetto_tools.py                    Perfetto 工具定位
  registry.py                          Android、malloc/mmap、FS 流程注册
```

独立仓库远端为：

```text
https://git.idianhun.com/fs/device-test-framework.git
```

父仓库固定经过验收的子模块 commit，不自动跟随远端 `main`。首次检出父仓库需使用
`git clone --recurse-submodules`，已有工作树使用 `git submodule update --init --recursive`。
通用核心不能引用 `run_heap_profile.py`、`collect_mmap_phys_data.py`、FS 包名或业务日志。

## 入口

推荐继续使用已有兼容入口：

```bash
./run_heap_profile.sh
./run_heap_profile.sh 2048 16777216
./run_mmap_phys_profile.sh
./run_mmap_phys_profile.sh --no-mmap-callstacks
```

两个脚本现在都是 `run_device_test.sh` 的薄包装。需要显式选择框架配置时可直接运行：

```bash
./run_device_test.sh malloc
./run_device_test.sh mmap --flow none -- --no-mmap-callstacks
./run_device_test.sh mmap --config device_test.ini -- --top-n 25
```

第一个 `--` 之后的参数原样交给专业后端。未写 `--` 的未知参数也会兼容转交，
但新增自动化命令建议显式分隔框架参数和后端参数。

## 配置

通用配置文件是 `device_test.ini`，包含：

```text
[run]                  平台、功能、流程和输出根目录
[device]               设备标识
[target]               包名和启动 Activity
[tools]                Perfetto、ADB、Python 等宿主机工具
[rpc]                  Poco RPC 本机端口和超时
[flow.fs_login_battle] 登录后测试模块
[feature.malloc]       malloc 采样和缓冲区参数
[feature.mmap]         mmap buffer、smaps 和 perf ring 参数
```

配置优先级为：

```text
命令行框架参数
  > DEVICE_TEST_* 环境变量
  > 旧环境变量兼容映射
  > device_test.ini
  > 代码默认值
```

旧变量 `ANDROID_SERIAL`、`MMAP_PHYS_APP`、`PerfettoRoot`、
`PERF_PROFILE_ACTION_SCRIPT`、`HEAP_PROFILE_RPC_LOCAL_PORT` 继续有效。
旧 Shell 入口会先加载 `config.sh`，再由 Python 配置层归一化。当前 `config.sh`
无条件设置正式包名，框架保持其最终值，不自行覆盖。

若环境变量覆盖了包名但没有显式覆盖 Activity，框架会从最终包名派生
`<包名>/com.dhplugin.unity.MainActivity`，避免包名和 Activity 指向不同应用。

## 三类扩展点

```text
PlatformAdapter
  AndroidAdapter：设备校验、adb -s、文件推送、运行期间隐藏错误对话框及恢复
  iOS/Windows：保留协议和注册位置，尚未实现，不能声明为可用平台

FeaturePlugin
  malloc：调用 run_heap_profile.py
  mmap：准备 FS 文件并调用 collect_mmap_phys_data.py

FlowSpec
  fs_login_battle：向后端声明登录、表加载和测试模块环境
  none：禁用登录后流程，例如无栈 mmap 验证
```

项目注册表位于 `device_test_plugins/registry.py`，扩展项必须显式注册，不扫描目录或动态加载。
插件通过 `required_capabilities` 声明平台能力，缺少能力时在启动专业后端前失败。

当前是渐进迁移：AndroidAdapter 管理公共外层连接、FS 文件推送和系统错误对话框；
`run_heap_profile.py` 与 `collect_mmap_phys_data.py` 仍负责各自内部的 App、PID、logcat、
Perfetto、smaps 和测试模块时序。后端通过 `ANDROID_SERIAL` 限定同一设备。只有这些专业
时序完成迁移并通过真机等价验证后，才能从后端删除对应逻辑。

## 阶段与错误处理

每轮固定记录以下阶段：

```text
connect
  -> platform_scope
  -> feature_validate
  -> feature_run
  -> feature_close
  -> platform_close
```

所有平台和功能公共接口使用 `OperationResult` 返回成功或失败。框架会捕获未处理异常，
记录阶段错误，并始终运行 `feature_close` 和 `platform_close`。专业后端的非零退出码会
保留为整轮失败，不会被清理或报告阶段覆盖。

## 输出

输出目录保持兼容：

```text
malloc -> PerfData/mem/<时间戳>/
mmap   -> PerfData/mmap_phys/<时间戳>/
连接或前置校验失败 -> PerfData/framework_failed/<功能>/<时间戳>/
```

每轮新增四个统一产物：

```text
run_config.json    归一化配置快照
run_manifest.json  状态、阶段、错误、后端命令和全部产物索引
run_summary.txt    便于脚本检查的结构化单行结果
report.md          人工阅读总报告和专业产物链接
```

malloc 和 mmap 的 trace、meminfo、健康报告、归因 JSON、pprof 等专业产物保持原名。
框架只建立索引，不把 malloc live、Native Heap Alloc、mmap PSS 等不同口径合并成一个值。

## 测试

```bash
python -m unittest discover -s device_test_framework/tests -t . -v
python -m unittest -v test_device_test_framework.py
python -m unittest discover -v
bash -n run_device_test.sh run_heap_profile.sh run_mmap_phys_profile.sh
./test_run_heap_profile.sh
./test_run_mmap_phys_profile.sh
```

Shell 集成测试默认删除临时目录。定位失败时可设置 `KEEP_TEST_TMP=1` 保留本轮夹具、
命令日志和统一报告。

升级通用框架时，先在独立仓库完成测试和推送，再在父仓库更新子模块指针：

```bash
cd device_test_framework
git fetch origin
git checkout <已验收 commit>
cd ..
git add device_test_framework
```
