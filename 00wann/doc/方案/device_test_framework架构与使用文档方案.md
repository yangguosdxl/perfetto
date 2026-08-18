# device_test_framework 架构与使用文档方案

## 背景

`device_test_framework` 已具备配置加载、显式注册、固定阶段执行、Android 平台适配、
后端功能插件、测试流程、动作 Session 和统一报告能力，但当前 `README.md` 仅提供简要接入说明。
父项目中的 `docs/device_test_framework.md` 还混合了 00wann 宿主插件、FS 业务流程和历史迁移信息，
不能代替独立框架自身的架构与使用文档。

本次文档以 `device_test_framework` 当前代码和单元测试为事实来源，不把历史方案中的目标设计
写成已经实现的能力，也不把 00wann、malloc、mmap 或 FS 业务规则下沉到通用框架文档。

## 目标产物

计划在 `device_test_framework` 内新增以下文件：

```text
device_test_framework/
  AGENTS.md                         整个框架目录的 AI 工作约束
  README.md                         更新文档导航，不重写现有简介
  docs/
    architecture.md                当前实现的架构文档
    quick-start-guide.md           面向开发者的人类可读快速入门指南
    ai-quick-start-guide.md        面向 AI 的结构化快速入门与修改指南
```

对“使用指南（两份，一份是人类可读，一份是 AI 可读）”的理解是：架构文档独立一份，
使用指南分别提供人类版和 AI 版，共三份正文文档；`AGENTS.md` 另计。

## 文档职责

### architecture.md

只描述当前已经落地的系统：

- 仓库边界与依赖方向；
- `cli -> config -> registry -> engine -> platform/feature/flow -> reporting` 调用链；
- `PlatformAdapter`、`FeaturePlugin`、`FlowSpec`、`ProfileActionSession` 的职责边界；
- 固定阶段、短路条件、异常结果化、无条件清理和最终返回码；
- 配置、运行上下文、阶段记录、产物清单和报告的数据流；
- Android 适配器与通用核心的边界；
- 当前限制，例如每轮单平台、单功能、单流程，以及专业后端仍由宿主提供。

文档使用 Mermaid 组件图和时序图展示依赖与运行过程，同时用表格列出模块所有权和扩展契约。

### quick-start-guide.md

面向首次接入和日常维护的开发者，按可执行任务组织：

- 宿主入口与 `Registry` 注入；
- INI 配置结构、环境变量和配置优先级；
- CLI 参数与 `--` 后端参数透传；
- 新增平台、功能插件和流程的最小示例；
- `async run_profile_action(session)` 脚本契约；
- ADB、目标 PID 日志等待、Poco RPC 和资源清理的正确用法；
- 统一输出目录、四个框架产物和结构化日志；
- 常见失败的定位路径；
- 单元测试命令和接入后的验证建议。

示例只使用 `sample`、`com.example.app` 等中性名称，避免框架文档依赖 00wann 业务。

### ai-quick-start-guide.md

面向需要分析或修改该框架的 AI，使用稳定标题、清单、契约表和决策树，减少自由解释空间：

- 权威源码索引及每个文件负责的事实；
- 不变量、允许的依赖方向和禁止引入的业务知识；
- 公共协议的输入、输出、失败语义与清理责任；
- 修改配置、阶段、平台、功能、流程、Action API、报告时应检查的文件；
- 按变更类型选择测试的矩阵；
- 文档同步规则和完成判定；
- 容易误判的事实，例如 `FlowSpec` 本身不执行动作、`BackendFeature` 才管理后端子进程、
  `ProfileActionSession.close()` 的清理失败不会改写动作竞速结果。

该文件是事实型操作手册，不包含对话风格、角色设定或临时任务指令。

### AGENTS.md

作为 `device_test_framework` 目录及其子目录的 AI 工作约束，内容保持短而明确：

- 通用框架不得引用宿主业务模块、包名、GM、malloc/mmap 专业后端；
- 以源码和测试为依据，历史方案不能作为当前实现证据；
- 保持显式注册、结构化结果、固定清理和可诊断输出；
- 修改公共协议时同步实现、测试、架构文档、人类指南和 AI 指南；
- 代码与注释使用中文，并为关键逻辑保留必要注释；
- 给出框架单元测试命令和按风险补充宿主验证的要求；
- 指向三份正文文档，避免在 `AGENTS.md` 重复大段架构说明。

`AGENTS.md` 不复制当前父项目中设备型号、FS 登录、mmap 45 秒或 Native heap 120 秒等
宿主业务规则，避免独立框架被错误绑定到单一项目；在父仓库工作时，上层规则仍继续生效。

## 一致性规则

文档中的签名、阶段名、配置优先级、环境变量、产物名和测试命令都从当前源码与测试提取。
重点核对以下事实源：

```text
cli.py / config.py                 命令行与配置
registry.py / engine.py            组合与生命周期
models.py / reporting.py           结果、阶段、产物与报告
platforms/*.py                     平台协议和 Android 实现
features/base.py / flows/base.py   功能与流程契约
actions/api.py / actions/runner.py Action SDK 和竞速语义
tests/*.py                         对外行为与失败路径
```

`README.md` 仅增加三份文档及 `AGENTS.md` 的入口，不复制正文，避免多处维护相同内容。

## 验证标准

实施后进行以下验证：

1. 逐项搜索文档中的文件路径、类名、方法名、阶段名和环境变量，确认源码存在且拼写一致。
2. 检查 Mermaid 代码块、内部相对链接、Markdown 标题层级和代码块闭合。
3. 检查人类指南的示例可导入、配置字段与当前加载器一致。
4. 检查 AI 指南与 `AGENTS.md` 不包含 00wann/FS 专属实现要求，也不声明未实现的 iOS、
   Windows 或细粒度生命周期能力。
5. 使用真实命令运行框架单元测试：

   ```bash
   python -m unittest discover -s device_test_framework/tests -t . -v
   ```

本次只修改 Markdown 文档，不修改 Python 调用栈或运行行为，因此不触发 mmap 真机验证。

## 待确认

请确认以下范围后再实施：

1. 接受“三份正文文档”的拆分：一份架构文档，加人类版和 AI 版两份使用指南。
2. 架构文档和两份使用指南存放在 `device_test_framework/docs/`，目录级约束存放在
   `device_test_framework/AGENTS.md`。
3. 接受同步更新 `device_test_framework/README.md`，只添加文档导航和职责说明。

## 执行计划

- [x] 1. 核对当前源码、测试、宿主接入点和已有文档，确定事实来源与文档边界。
- [x] 2. 创建 `docs/architecture.md`，描述当前架构、运行时序、数据流和扩展契约。
- [x] 3. 创建 `docs/quick-start-guide.md`，提供面向开发者的接入、使用和排障步骤。
- [x] 4. 创建 `docs/ai-quick-start-guide.md`，提供面向 AI 的源码索引、不变量和修改检查表。
- [x] 5. 创建根目录 `AGENTS.md`，并为现有 `README.md` 补充文档导航。
- [x] 6. 核验文档中的路径、符号、配置、链接、示例和敏感信息。
- [x] 7. 运行框架单元测试，记录结果并完成计划状态。

## 验证结果

- 文档相对链接、源码符号、阶段名、环境变量和 Markdown 围栏检查通过。
- 常见密钥、令牌和私钥模式扫描无命中。
- `python -m unittest discover -s device_test_framework/tests -t . -v`：32 项测试全部通过。
- 本次仅修改 Markdown，不涉及调用栈或运行行为，按项目规则不执行 mmap 真机验证。
