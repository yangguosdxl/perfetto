# 测试脚本通用 ADB 与 Poco RPC 方案

## 背景

`profile_actions/send_battle_record_gm.py` 当前同时包含三类逻辑：

- 通过指定设备执行 ADB 命令；
- 从本轮目标 App 的 logcat 中查找 Poco 监听端口；
- 建立 ADB 端口转发并按 Poco 长度帧协议调用 JSON-RPC；
- 发送战斗录像 GM，并判断该 GM 是否执行成功。

前三项与具体测试目的无关。后续测试脚本也可能需要调用 Poco RPC，因此应提升到 `profile_action_api.py`，战斗录像 GM 脚本只保留业务参数、业务结果判断和业务日志。

## 职责划分

```text
测试脚本 send_battle_record_gm.py
  |
  |-- 指定 DoRecordCheat 和 GM 参数
  |-- 判断 result 是否为 true
  |-- 输出 HEAP_PROFILE_GM_RPC 日志
  `-- 写入 gm_rpc.txt
          |
          v
profile_action_api.py
  |-- run_adb(context, ...)
  |     `-- 统一使用 context.adb 和 context.android_serial
  |-- find_poco_port(context)
  |     `-- 只读取本轮 PID 日志，返回最后一个合法端口
  `-- invoke_rpc(context, method, params, ...)
        |-- 建立并清理 adb forward
        |-- 发送 4 字节小端长度 + JSON 请求体
        |-- 精确接收长度帧响应并解析 JSON
        `-- 返回 RpcResult(response, local_port, remote_port)
```

通用层负责命令执行、端口发现、传输协议和 JSON-RPC 标准错误；业务层负责具体 method、params、`result` 的含义以及业务日志和产物。通用层不写死 GM 指令，也不写 `gm_rpc.txt`。

## 公共接口

### `run_adb`

```python
def run_adb(
    context: ProfileActionContext,
    *args: str,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
```

- 使用 `context.adb` 作为可执行文件。
- `context.android_serial` 非空时自动添加 `-s <serial>`，确保只操作配置的真机。
- 捕获标准输出和错误输出，使用文本模式，不因非零返回码自动抛出异常，调用者可以结合返回码和错误文本给出阶段化错误。
- 可选超时直接交给 `subprocess.run` 处理。

### `find_poco_port`

```python
def find_poco_port(
    context: ProfileActionContext,
    *,
    min_port: int = 5001,
    max_port: int = 5005,
) -> int:
```

- 从 `context.logcat_path` 查找 `Tcp server started and listening at <port>`。
- 只接受包含 `context.pid` 的日志行，避免使用旧进程或其他进程的端口。
- 多次命中时使用最后一次端口。
- 找不到端口时抛出包含 `poco_port_missing` 的错误；端口超出范围时抛出包含 `poco_port_out_of_range` 的错误。

### `invoke_rpc`

```python
@dataclass(frozen=True)
class RpcResult:
  response: dict
  local_port: int
  remote_port: int


def invoke_rpc(
    context: ProfileActionContext,
    method: str,
    params: list | dict | None = None,
    *,
    request_id: int | str = 1,
    remote_port: int | None = None,
    timeout_seconds: float | None = None,
    max_response_bytes: int = 1024 * 1024,
) -> RpcResult:
```

- 未传 `remote_port` 时调用 `find_poco_port`。
- 使用 `context.rpc_local_port` 建立 `adb forward`；无论调用成功或失败，均在 `finally` 中移除该转发。
- 请求和响应均采用 Poco 的“4 字节小端长度头 + UTF-8 JSON”协议。
- 检查响应长度、响应截断、JSON 格式、响应对象类型和 JSON-RPC `error`。
- 不要求 `response["result"] is True`。不同 RPC 的返回值语义可能不同，由测试脚本判断。
- 返回 `RpcResult`，便于业务脚本记录本地端口、远端端口和完整响应。

为控制代码量，首版继续使用带稳定错误标识的标准异常，不额外建立多组自定义异常类。

## 兼容性

重构后 `send_battle_record_gm.py` 的外部行为保持不变：

- 仍调用 `DoRecordCheat`；
- 仍发送 `CheatFunc_BatchCheatOption.战斗录像:@40011@@|`；
- 仍要求业务结果为 `true`；
- 仍输出 `HEAP_PROFILE_GM_RPC=PASS/FAIL`；
- 仍生成 `gm_rpc.txt`；
- 仍通过 `asyncio.to_thread` 避免阻塞采集协程。

## 验证范围

新增或补充单元测试验证：

- ADB 命令在有设备序列号时带 `-s`，无序列号时不带；
- Poco 端口只匹配本轮 PID、取最后一次命中，并检查合法范围；
- RPC 请求长度头和 JSON 内容正确；
- 分段响应可被完整接收；
- RPC 错误、非法响应大小、截断响应能给出明确错误；
- 成功和失败路径都会删除 ADB 端口转发；
- 战斗录像 GM 的现有方法名、参数、日志和产物格式保持不变。

实施后运行 Python 单元测试、Python 语法检查和现有 Shell 集成测试，并同步更新公共测试模块 API 相关文档。

## 执行计划

- [x] 1. 在 `profile_action_api.py` 增加 `RpcResult`、`run_adb`、`find_poco_port` 和 `invoke_rpc`。
- [x] 2. 精简 `send_battle_record_gm.py`，并保持现有 GM 业务行为和诊断产物不变。
- [x] 3. 增加公共 ADB、Poco 端口发现和 RPC 协议单元测试。
- [x] 4. 更新相关使用文档，说明测试脚本可以直接复用公共 API。
- [x] 5. 运行 Python 检查、单元测试和 Shell 集成测试，并处理发现的问题。
- [x] 6. 将验证结果和实现结论记录到方案与 Pensieve。

## 执行结果

- `python -m unittest discover -v`：85 个测试全部通过。
- `python -m py_compile profile_action_api.py profile_action_runner.py profile_actions/send_battle_record_gm.py`：通过。
- `bash -n test_run_heap_profile.sh`：通过。
- `./test_run_heap_profile.sh`：通过；验证了 `adb -s FAKE_HEAP_DEVICE forward`、Poco 小端长度帧、`DoRecordCheat`、完整战斗录像 GM 参数、RPC 失败清理和采集收尾。
