# HybridCLR RawImage 常驻内存优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先消除 HybridCLR Hotfix/AOT 原始 DLL 字节的 native 常驻拷贝，并用 ECMA335 与 FS 真机登录场景验证 `il2cpp_meta + hybridclr` 常驻内存是否达到降低 50% 的最终验收线。

**Architecture:** 在 HybridCLR native 侧为 `RawImageBase` 增加 compact backing store：运行时元数据初始化完成后，只保留 metadata streams、metadata tables、方法体、FieldRVA 等后续懒读必需字节，并释放完整 PE 原始拷贝。为保持现有调用方稳定，所有原始 image offset 继续作为逻辑 offset，访问时通过 compact range 映射到新 buffer。

**Tech Stack:** Unity 2022.3.62f3、IL2CPP、HybridCLR、Android arm64、Perfetto heapprofd、ECMA335 Unity 真机测试、PowerShell、Git Bash。

---

## 真实证据与验收口径

当前证据目录：

```text
D:\dr2\Misc\perfetto\00wann\PerfData\mem\2026-06-16_13-52-01
```

已知数据：

```text
登录完成日志：2026-06-16 13:52:12 登录场景完成
il2cpp_meta：148.17 MiB
hybridclr：121.53 MiB
合计：269.70 MiB
50% 目标线：134.85 MiB
CopyBytes 原始 DLL/AOT 常驻：57.19 MiB
Hotfix assets：44.09 MiB
AOT assets：13.09 MiB
```

这份基线的 `heap_meminfo_validation.txt` 为 FAIL，只能作为方向证据。最终验收必须重新采集，并满足：

```text
真机：1C111FDF600AW5
包名：com.fs.t.prf
启动 Activity：com.fs.t.prf/com.dhplugin.unity.MainActivity
结束标志：logcat 出现 登录场景完成后稳定采集 30 秒
heap_meminfo_validation：PASS
il2cpp_meta + hybridclr <= 134.85 MiB
```

## 文件结构

### AppIl2cpp 修改

- 修改: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP\libil2cpp\hybridclr\metadata\RawImageBase.h`
  - 保存 compact range、stream 原始 offset、compact 状态。
  - 将 `GetDataPtrByImageOffset`、`GetFieldOrParameterDefalutValueByRawIndex`、`GetImageOffsetOfBlob` 改为支持 logical image offset。

- 修改: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP\libil2cpp\hybridclr\metadata\RawImageBase.cpp`
  - 收集保留区间，构建 compact buffer，重定向 streams/tables。
  - 计算方法体范围，保留 EH section 与 local var signature 懒读路径所需字节。

- 修改: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP\libil2cpp\hybridclr\metadata\Assembly.cpp`
  - Hotfix `InterpreterImage::InitRuntimeMetadatas()` 后 compact。
  - AOT homologous image `InitRuntimeMetadatas()` 后 compact，再注册。

### ECMA335 修改

- 创建: `D:\dr2\Misc\Ecma335UnitTest\Assets\App\Ecma335\Ecma335RawImageLazyReadTests.cs`
- 创建: `D:\dr2\Misc\Ecma335UnitTest\Assets\Hotfix\Ecma335\Ecma335RawImageLazyReadTests.cs`
- 修改: `D:\dr2\Misc\Ecma335UnitTest\Assets\App\Ecma335\Ecma335AotSuite.cs`
- 修改: `D:\dr2\Misc\Ecma335UnitTest\Assets\Hotfix\Ecma335\Ecma335HybridSuite.cs`
- 修改: `D:\dr2\Misc\Ecma335UnitTest\Docs\Ecma335CoverageMatrix.md`
- 修改: `D:\dr2\Misc\Ecma335UnitTest\Docs\SuperpowerEcma335BclTestPlan.md`

### FSPatcher 同步与文档

- 修改 mirrored files under: `D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\libil2cpp\hybridclr\metadata`
- 修改 mirrored files under: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\libil2cpp\hybridclr\metadata`
- 修改: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\docs\hybridclr_raw_image_resident_memory.md`
- 不修改 FS Wiki。

### 采集脚本修改

- 修改: `D:\dr2\Misc\perfetto\00wann\run_heap_profile.py`
  - 由 `adb shell monkey` 改为显式 `am start`。
  - heapprofd active 后启动 App。
  - 保存 logcat。
  - logcat 出现 `登录场景完成` 后继续稳定采集 30 秒，再请求 heapprofd shutdown。

- 修改: `D:\dr2\Misc\perfetto\00wann\test_run_heap_profile.sh`
  - fake adb 覆盖 `logcat`、`am start`、登录完成触发 shutdown。

---

### Task 1: 让 native heap 采集按登录完成结束

**Files:**
- 修改: `D:\dr2\Misc\perfetto\00wann\run_heap_profile.py`
- 修改: `D:\dr2\Misc\perfetto\00wann\test_run_heap_profile.sh`

- [ ] **Step 1: 写失败测试，禁止 monkey 启动**

在 `test_run_heap_profile.sh` 中把当前 monkey 断言改为显式启动断言：

```bash
if grep -Fq "adb shell monkey -p $expected_app 1" "$TEST_LOG"; then
  echo "FS 登录场景采集不能使用 monkey 启动"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "adb shell am start -n $expected_app/com.dhplugin.unity.MainActivity" "$TEST_LOG"; then
  echo "未使用明确 Activity 启动 FS"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "adb logcat -v time" "$TEST_LOG"; then
  echo "未保存登录场景 logcat"
  cat "$TEST_LOG"
  exit 1
fi
if ! grep -Fq "PYTHON_GOT_SIGINT" "$TEST_LOG"; then
  echo "登录完成后未请求 heap_profile.py 收尾"
  cat "$TEST_LOG"
  exit 1
fi
```

- [ ] **Step 2: 运行失败测试**

运行:

```powershell
bash D:\dr2\Misc\perfetto\00wann\test_run_heap_profile.sh
```

预期: FAIL，输出包含 `FS 登录场景采集不能使用 monkey 启动`。

- [ ] **Step 3: 实现显式启动与登录完成等待**

在 `run_heap_profile.py` 顶部常量区加入：

```python
APP = "com.fs.t.prf"
LAUNCH_ACTIVITY = "com.fs.t.prf/com.dhplugin.unity.MainActivity"
LOGIN_DONE_PATTERN = "登录场景完成"
LOGIN_STABLE_SECONDS = 5
LOGIN_TIMEOUT_SECONDS = 180
```

新增函数：

```python
def start_logcat_capture(out_dir: Path, env: dict[str, str]) -> subprocess.Popen[str]:
  """保存登录阶段日志，供验收确认登录场景完成。"""
  logcat_path = out_dir / "logcat.txt"
  err_path = out_dir / "logcat.err.txt"
  run([adb_binary(), "logcat", "-c"], env=env)
  logcat = subprocess.Popen(
      [adb_binary(), "logcat", "-v", "time"],
      stdout=logcat_path.open("w", encoding="utf-8", errors="replace"),
      stderr=err_path.open("w", encoding="utf-8", errors="replace"),
      text=True,
      env=env,
  )
  return logcat


def wait_for_login_done(out_dir: Path, shutdown_requested: callable) -> bool:
  """等待 FS 自动进入登录场景；登录完成日志出现后稳定采集 30 秒。"""
  logcat_path = out_dir / "logcat.txt"
  deadline = time.time() + LOGIN_TIMEOUT_SECONDS
  read_offset = 0
  while time.time() < deadline and not shutdown_requested():
    if logcat_path.exists():
      with logcat_path.open("r", encoding="utf-8", errors="replace") as logcat:
        logcat.seek(read_offset)
        chunk = logcat.read()
        read_offset = logcat.tell()
      if LOGIN_DONE_PATTERN in chunk:
        print(f"LOGIN_SCENE_DONE|pattern={LOGIN_DONE_PATTERN}")
        time.sleep(LOGIN_STABLE_SECONDS)
        return True
    time.sleep(0.5)
  print(f"LOGIN_SCENE_TIMEOUT|pattern={LOGIN_DONE_PATTERN}|timeout_s={LOGIN_TIMEOUT_SECONDS}")
  return False


def launch_target_app(env: dict[str, str]) -> None:
  """用确定 Activity 启动，避免 monkey 引入随机行为。"""
  print(f"\nheapprofd 已就绪，启动目标应用: {LAUNCH_ACTIVITY}")
  run([adb_binary(), "shell", "am", "start", "-n", LAUNCH_ACTIVITY], env=env)
```

替换原有启动块：

```python
  logcat_proc = start_logcat_capture(out_dir, env)
  try:
    if not shutdown_requested:
      launch_target_app(env)
      pid = wait_for_pid(APP, lambda: shutdown_requested)
      if pid:
        print(f"\r应用已启动: {APP} pid={pid}")
    if not shutdown_requested and wait_for_login_done(out_dir, lambda: shutdown_requested):
      shutdown_requested = True
      request_profiler_shutdown(profiler_proc)
  finally:
    if logcat_proc.poll() is None:
      logcat_proc.terminate()
      try:
        logcat_proc.wait(timeout=5)
      except subprocess.TimeoutExpired:
        logcat_proc.kill()
```

- [ ] **Step 4: 运行脚本测试通过**

运行:

```powershell
bash D:\dr2\Misc\perfetto\00wann\test_run_heap_profile.sh
```

预期: PASS。

- [ ] **Step 5: 提交采集脚本修改**

```powershell
git -C D:\dr2\Misc\perfetto add 00wann/run_heap_profile.py 00wann/test_run_heap_profile.sh
git -C D:\dr2\Misc\perfetto commit -m "让 FS heap 采集按登录完成收尾"
```

---

### Task 2: 增加 RawImage 懒读 ECMA335 覆盖

**Files:**
- 创建: `D:\dr2\Misc\Ecma335UnitTest\Assets\App\Ecma335\Ecma335RawImageLazyReadTests.cs`
- 创建: `D:\dr2\Misc\Ecma335UnitTest\Assets\Hotfix\Ecma335\Ecma335RawImageLazyReadTests.cs`
- 修改: `D:\dr2\Misc\Ecma335UnitTest\Assets\App\Ecma335\Ecma335AotSuite.cs`
- 修改: `D:\dr2\Misc\Ecma335UnitTest\Assets\Hotfix\Ecma335\Ecma335HybridSuite.cs`

- [ ] **Step 1: 先在 suite 注册新测试，让构建失败**

在 `Ecma335AotSuite.RunAll()` 的 `Ecma335AdvancedInstructionTests.Run(context);` 后加入：

```csharp
Ecma335RawImageLazyReadTests.Run(context);
```

在 `Ecma335HybridSuite.RunAll()` 的 `Ecma335AdvancedInstructionTests.Run(context);` 后加入同一行。

运行:

```powershell
cd D:\dr2\Misc\Ecma335UnitTest
.\00build.bat
```

预期: FAIL，C# 编译报 `Ecma335RawImageLazyReadTests` 未定义。

- [ ] **Step 2: 新增 AOT 测试文件**

创建 `D:\dr2\Misc\Ecma335UnitTest\Assets\App\Ecma335\Ecma335RawImageLazyReadTests.cs`:

```csharp
using System;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;

namespace Ecma335UnitTest.Aot
{
    [AttributeUsage(AttributeTargets.Class | AttributeTargets.Method, AllowMultiple = true)]
    sealed class RawImageMarkerAttribute : Attribute
    {
        public readonly string Name;
        public readonly int Number;

        public RawImageMarkerAttribute(string name, int number)
        {
            Name = name;
            Number = number;
        }
    }

    [RawImageMarker("类型标记-用户字符串", 37)]
    public static class Ecma335RawImageLazyReadTests
    {
        const int DefaultInt = 1234567;
        const string DefaultString = "RawImage 常驻字节释放后仍可读取";

        [DllImport("__Internal", EntryPoint = "hybridclr_raw_image_probe_not_called")]
        static extern int NativeProbeNotCalled();

        [RawImageMarker("方法标记-自定义属性", 41)]
        static int MethodWithExceptionSection(int value)
        {
            try
            {
                if (value < 0)
                {
                    throw new InvalidOperationException(DefaultString);
                }
                return value + DefaultInt;
            }
            catch (InvalidOperationException ex)
            {
                return ex.Message.Contains("RawImage") ? 17 : -1;
            }
            finally
            {
                value++;
            }
        }

        static T GenericEcho<T>(T value)
        {
            return value;
        }

        public static void Run(RegressionTestContext context)
        {
            context.Check("RawImage.MethodBody EH 懒读", () =>
            {
                RegressionAssert.Equal(DefaultInt + 5, MethodWithExceptionSection(5), "普通路径应读取方法体");
                RegressionAssert.Equal(17, MethodWithExceptionSection(-1), "异常处理表应在 compact 后可读");
            });

            context.Check("RawImage.UserString 懒读", () =>
            {
                string value = GenericEcho(DefaultString);
                RegressionAssert.True(value.Contains("常驻字节"), "用户字符串应保持可读");
            });

            context.Check("RawImage.CustomAttribute Blob 懒读", () =>
            {
                var attrs = typeof(Ecma335RawImageLazyReadTests).GetCustomAttributes(typeof(RawImageMarkerAttribute), false);
                var attr = (RawImageMarkerAttribute)attrs[0];
                RegressionAssert.Equal("类型标记-用户字符串", attr.Name, "类型自定义属性字符串应正确");
                RegressionAssert.Equal(37, attr.Number, "类型自定义属性数字应正确");
            });

            context.Check("RawImage.Method CustomAttribute Blob 懒读", () =>
            {
                var method = typeof(Ecma335RawImageLazyReadTests).GetMethod("MethodWithExceptionSection", BindingFlags.Static | BindingFlags.NonPublic);
                var attr = (RawImageMarkerAttribute)method.GetCustomAttributes(typeof(RawImageMarkerAttribute), false).Single();
                RegressionAssert.Equal("方法标记-自定义属性", attr.Name, "方法自定义属性字符串应正确");
                RegressionAssert.Equal(41, attr.Number, "方法自定义属性数字应正确");
            });

            context.Check("RawImage.Generic LocalVarSig 懒读", () =>
            {
                string value = GenericEcho("泛型局部签名");
                RegressionAssert.Equal("泛型局部签名", value, "泛型方法体和签名应正确");
            });

            context.Check("RawImage.ImplMap 元数据懒读", () =>
            {
                var method = typeof(Ecma335RawImageLazyReadTests).GetMethod("NativeProbeNotCalled", BindingFlags.Static | BindingFlags.NonPublic);
                var attr = (DllImportAttribute)method.GetCustomAttributes(typeof(DllImportAttribute), false).Single();
                RegressionAssert.Equal("__Internal", attr.Value, "P/Invoke 模块名应正确");
                RegressionAssert.Equal("hybridclr_raw_image_probe_not_called", attr.EntryPoint, "P/Invoke 入口名应正确");
            });
        }
    }
}
```

- [ ] **Step 3: 新增 Hotfix 测试文件**

创建 `D:\dr2\Misc\Ecma335UnitTest\Assets\Hotfix\Ecma335\Ecma335RawImageLazyReadTests.cs`，内容与 AOT 文件一致，仅命名空间改为：

```csharp
namespace Ecma335UnitTest.Hybrid
```

- [ ] **Step 4: 构建通过**

运行:

```powershell
cd D:\dr2\Misc\Ecma335UnitTest
.\00build.bat
```

预期: PASS。

- [ ] **Step 5: 提交 ECMA335 测试修改**

```powershell
git -C D:\dr2\Misc\Ecma335UnitTest add Assets/App/Ecma335 Assets/Hotfix/Ecma335
git -C D:\dr2\Misc\Ecma335UnitTest commit -m "增加 RawImage 懒读回归覆盖"
```

---

### Task 3: 增加 RawImage compact API 与 no-op 接线

**Files:**
- 修改: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP\libil2cpp\hybridclr\metadata\RawImageBase.h`
- 修改: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP\libil2cpp\hybridclr\metadata\RawImageBase.cpp`
- 修改: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP\libil2cpp\hybridclr\metadata\Assembly.cpp`

- [ ] **Step 1: 在 Assembly.cpp 先接入未定义 API，让编译失败**

在 Hotfix 路径：

```cpp
        image->InitRuntimeMetadatas();
        err = image->GetRawImage().CompactRawImageData();
        if (err != LoadImageErrorCode::OK)
        {
            TEMP_FORMAT(errMsg, "CompactRawImageData Error:%d", (int)err);
            il2cpp::vm::Exception::Raise(il2cpp::vm::Exception::GetBadImageFormatException(errMsg));
        }
```

在 AOT 路径：

```cpp
        image->InitRuntimeMetadatas();
        err = image->GetRawImage().CompactRawImageData();
        if (err != LoadImageErrorCode::OK)
        {
            delete image;
            return err;
        }
```

运行:

```powershell
cd D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler
$env:JAVA_HOME='D:\Program Files\Unity 2022.3.62f3\Editor\Data\PlaybackEngines\AndroidPlayer\OpenJDK'
& 'D:\bin\gradle-7.5.1\bin\gradle.bat' :launcher:assembleDebug --no-daemon --console=plain
```

预期: FAIL，提示 `CompactRawImageData` 未定义。

- [ ] **Step 2: 添加 no-op API**

在 `RawImageBase.h` public 区域加入：

```cpp
        LoadImageErrorCode CompactRawImageData();
        bool IsRawImageCompacted() const { return _isCompacted; }
        uint32_t GetCompactImageLength() const { return _compactImageLength; }
```

在 protected 成员区加入：

```cpp
        uint32_t _compactImageLength;
        bool _isCompacted;
```

构造函数初始化增加：

```cpp
            _compactImageLength(0), _isCompacted(false)
```

在 `RawImageBase.cpp` 加入 no-op 实现：

```cpp
    LoadImageErrorCode RawImageBase::CompactRawImageData()
    {
        _compactImageLength = _imageLength;
        _isCompacted = false;
        return LoadImageErrorCode::OK;
    }
```

- [ ] **Step 3: 编译通过**

运行:

```powershell
cd D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler
$env:JAVA_HOME='D:\Program Files\Unity 2022.3.62f3\Editor\Data\PlaybackEngines\AndroidPlayer\OpenJDK'
& 'D:\bin\gradle-7.5.1\bin\gradle.bat' :launcher:assembleDebug --no-daemon --console=plain
```

预期: PASS。

- [ ] **Step 4: 提交接线修改**

```powershell
git -C D:\dr2\Misc\perfetto status --short
```

FS 客户端工程不是 git 仓库，本步骤只记录本地变更，不在 FS 工程内提交。

---

### Task 4: 实现 RawImage compact range 映射

**Files:**
- 修改: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP\libil2cpp\hybridclr\metadata\RawImageBase.h`
- 修改: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP\libil2cpp\hybridclr\metadata\RawImageBase.cpp`

- [ ] **Step 1: 写失败验收，确认 no-op 仍不达标**

运行:

```powershell
$env:ANDROID_SERIAL='1C111FDF600AW5'
cd D:\dr2\Misc\perfetto\00wann
bash run_heap_profile.sh
```

预期: FAIL。`heap_meminfo_validation.txt` 可能 PASS 或 FAIL；无论对账状态如何，`CopyBytes` 仍约 57 MiB，`il2cpp_meta + hybridclr` 不会达到 134.85 MiB 目标线。

- [ ] **Step 2: 扩展 RawImageBase.h 数据结构**

在 `RawImageBase.h` 的 class 前加入：

```cpp
    struct RawImagePreservedRange
    {
        uint32_t sourceOffset;
        uint32_t size;
        uint32_t compactOffset;
        const char* reason;
    };
```

替换三个 accessor：

```cpp
        const uint8_t* GetFieldOrParameterDefalutValueByRawIndex(uint32_t index) const
        {
            return GetImageDataBySourceOffset(index);
        }

        uint32_t GetImageOffsetOfBlob(Il2CppTypeEnum type, uint32_t index) const
        {
            if (type != IL2CPP_TYPE_STRING)
            {
                uint32_t sizeLength;
                BlobReader::ReadCompressedUint32(_streamBlobHeap.data + index, sizeLength);
                return _streamBlobHeapSourceOffset + index + sizeLength;
            }
            return _streamBlobHeapSourceOffset + index;
        }

        const byte* GetDataPtrByImageOffset(uint32_t imageOffset) const
        {
            IL2CPP_ASSERT(imageOffset < _imageLength);
            return GetImageDataBySourceOffset(imageOffset);
        }
```

在 protected 区加入：

```cpp
        const byte* GetImageDataBySourceOffset(uint32_t sourceOffset) const;
        const byte* TryGetImageDataBySourceOffset(uint32_t sourceOffset) const;
        uint32_t ComputeSourceOffset(const byte* ptr) const;
        uint32_t GetSectionRawEndByImageOffset(uint32_t sourceOffset) const;
        void RecordPreservedRange(uint32_t sourceOffset, uint32_t size, const char* reason);
        void RecordCliStreamRange(CliStream& stream, uint32_t& streamSourceOffset, const char* reason);
        LoadImageErrorCode RecordMethodBodyRanges();
        LoadImageErrorCode RecordFieldRvaRanges();
        LoadImageErrorCode BuildCompactImageData();
        void RedirectCompactPointers();

        std::vector<RawImagePreservedRange> _preservedRanges;
        uint32_t _streamStringHeapSourceOffset;
        uint32_t _streamUSSourceOffset;
        uint32_t _streamBlobHeapSourceOffset;
        uint32_t _streamGuidHeapSourceOffset;
        uint32_t _streamTablesSourceOffset;
```

构造函数初始化增加：

```cpp
            _streamStringHeapSourceOffset(0), _streamUSSourceOffset(0), _streamBlobHeapSourceOffset(0),
            _streamGuidHeapSourceOffset(0), _streamTablesSourceOffset(0),
```

- [ ] **Step 3: 实现 source offset 映射**

在 `RawImageBase.cpp` 加入：

```cpp
    const byte* RawImageBase::TryGetImageDataBySourceOffset(uint32_t sourceOffset) const
    {
        if (!_isCompacted)
        {
            return sourceOffset < _imageLength ? _imageData + sourceOffset : nullptr;
        }
        for (const RawImagePreservedRange& range : _preservedRanges)
        {
            if (range.sourceOffset <= sourceOffset && sourceOffset < range.sourceOffset + range.size)
            {
                return _imageData + range.compactOffset + (sourceOffset - range.sourceOffset);
            }
        }
        return nullptr;
    }

    const byte* RawImageBase::GetImageDataBySourceOffset(uint32_t sourceOffset) const
    {
        const byte* data = TryGetImageDataBySourceOffset(sourceOffset);
        IL2CPP_ASSERT(data && "RawImage compact range missing");
        return data;
    }

    uint32_t RawImageBase::ComputeSourceOffset(const byte* ptr) const
    {
        IL2CPP_ASSERT(!_isCompacted);
        IL2CPP_ASSERT(ptr >= _imageData && ptr <= _imageData + _imageLength);
        return (uint32_t)(ptr - _imageData);
    }
```

- [ ] **Step 4: 实现 range 收集**

在 `RawImageBase.cpp` 加入：

```cpp
    void RawImageBase::RecordPreservedRange(uint32_t sourceOffset, uint32_t size, const char* reason)
    {
        if (size == 0)
        {
            return;
        }
        IL2CPP_ASSERT(sourceOffset < _imageLength);
        IL2CPP_ASSERT(size <= _imageLength - sourceOffset);
        _preservedRanges.push_back({ sourceOffset, size, 0, reason });
    }

        void RawImageBase::RecordCliStreamRange(CliStream& stream, uint32_t& streamSourceOffset, const char* reason)
        {
            if (!stream.data || stream.size == 0)
            {
                streamSourceOffset = 0;
                return;
            }
            streamSourceOffset = ComputeSourceOffset(stream.data);
            RecordPreservedRange(streamSourceOffset, stream.size, reason);
        }

    uint32_t RawImageBase::GetSectionRawEndByImageOffset(uint32_t sourceOffset) const
    {
        for (const SectionHeader& sh : _sections)
        {
            uint32_t rawBegin = sh.ptrRawDataRelatedToVirtualAddress + sh.virtualAddressBegin;
            uint32_t rawEnd = sh.ptrRawDataRelatedToVirtualAddress + sh.virtualAddressEnd;
            if (rawBegin <= sourceOffset && sourceOffset < rawEnd)
            {
                return std::min(rawEnd, _imageLength);
            }
        }
        return _imageLength;
    }
```

- [ ] **Step 5: 实现方法体范围计算**

在 `RawImageBase.cpp` 加入：

```cpp
    static uint32_t ComputeMethodBodyRangeSize(const byte* bodyStart)
    {
        byte bodyFlags = *bodyStart;
        byte smallFatFlags = bodyFlags & 0x3;
        if (smallFatFlags == (uint8_t)CorILMethodFormat::Tiny)
        {
            return 1 + ((uint8_t)bodyFlags >> 2);
        }

        IL2CPP_ASSERT(smallFatFlags == (uint8_t)CorILMethodFormat::Fat);
        const byte* headerStart = (const byte*)GetAlignBorder<4>(bodyStart);
        const CorILMethodFatHeader* methodHeader = (const CorILMethodFatHeader*)headerStart;
        IL2CPP_ASSERT(methodHeader->size == 3);

        const byte* end = headerStart + methodHeader->size * 4 + methodHeader->codeSize;
        if (methodHeader->flags & (uint8_t)CorILMethodFormat::MoreSects)
        {
            const byte* nextSection = (const byte*)GetAlignBorder<4>(end);
            while (true)
            {
                byte kind = *nextSection;
                if (kind & (byte)CorILSecion::FatFormat)
                {
                    const CorILEHSectionHeaderFat* ehSec = (const CorILEHSectionHeaderFat*)nextSection;
                    uint32_t dataSize = (uint32_t)ehSec->dataSize0 | ((uint32_t)ehSec->dataSize1 << 8) | ((uint32_t)ehSec->dataSize2 << 16);
                    nextSection += dataSize;
                }
                else
                {
                    const CorILEHSectionHeaderSmall* ehSec = (const CorILEHSectionHeaderSmall*)nextSection;
                    nextSection += ehSec->dataSize;
                }
                end = nextSection;
                if (!(kind & (byte)CorILSecion::MoreSects))
                {
                    break;
                }
            }
        }
        return (uint32_t)(end - bodyStart);
    }

    LoadImageErrorCode RawImageBase::RecordMethodBodyRanges()
    {
        const Table& methods = GetTable(TableType::METHOD);
        for (uint32_t rowIndex = 1; rowIndex <= methods.rowNum; rowIndex++)
        {
            TbMethod method = ReadMethod(rowIndex);
            if (method.rva == 0)
            {
                continue;
            }
            uint32_t methodImageOffset = 0;
            if (!TranslateRVAToImageOffset(method.rva, methodImageOffset))
            {
                return LoadImageErrorCode::BAD_IMAGE;
            }
            const byte* bodyStart = _imageData + methodImageOffset;
            uint32_t size = ComputeMethodBodyRangeSize(bodyStart);
            RecordPreservedRange(methodImageOffset, size, "MethodBody");
        }
        return LoadImageErrorCode::OK;
    }
```

- [ ] **Step 6: 实现 FieldRVA 保留**

在 `RawImageBase.cpp` 加入：

```cpp
    LoadImageErrorCode RawImageBase::RecordFieldRvaRanges()
    {
        const Table& fieldRvaTable = GetTable(TableType::FIELDRVA);
        if (fieldRvaTable.rowNum == 0)
        {
            return LoadImageErrorCode::OK;
        }

        std::vector<uint32_t> offsets;
        offsets.reserve(fieldRvaTable.rowNum);
        for (uint32_t rowIndex = 1; rowIndex <= fieldRvaTable.rowNum; rowIndex++)
        {
            TbFieldRVA fieldRva = ReadFieldRVA(rowIndex);
            uint32_t imageOffset = 0;
            if (!TranslateRVAToImageOffset(fieldRva.rva, imageOffset))
            {
                return LoadImageErrorCode::BAD_IMAGE;
            }
            offsets.push_back(imageOffset);
        }
        std::sort(offsets.begin(), offsets.end());
        offsets.erase(std::unique(offsets.begin(), offsets.end()), offsets.end());

        for (size_t index = 0; index < offsets.size(); index++)
        {
            uint32_t begin = offsets[index];
            uint32_t sectionEnd = GetSectionRawEndByImageOffset(begin);
            uint32_t next = index + 1 < offsets.size() ? offsets[index + 1] : sectionEnd;
            uint32_t end = std::min(next, sectionEnd);
            if (end > begin)
            {
                RecordPreservedRange(begin, end - begin, "FieldRVA");
            }
        }
        return LoadImageErrorCode::OK;
    }
```

- [ ] **Step 7: 实现 compact buffer 构建与重定向**

在 `RawImageBase.cpp` 加入：

```cpp
    void RawImageBase::RedirectCompactPointers()
    {
        const byte* oldTablesData = _streamTables.data;
        uint32_t tableSourceOffsets[TABLE_NUM] = {};
        for (int i = 0; i < TABLE_NUM; i++)
        {
            if (_tables[i].data)
            {
                tableSourceOffsets[i] = _streamTablesSourceOffset + (uint32_t)(_tables[i].data - oldTablesData);
            }
        }

        _streamStringHeap.data = GetImageDataBySourceOffset(_streamStringHeapSourceOffset);
        _streamUS.data = GetImageDataBySourceOffset(_streamUSSourceOffset);
        _streamBlobHeap.data = GetImageDataBySourceOffset(_streamBlobHeapSourceOffset);
        _streamGuidHeap.data = GetImageDataBySourceOffset(_streamGuidHeapSourceOffset);
        _streamTables.data = GetImageDataBySourceOffset(_streamTablesSourceOffset);

        for (int i = 0; i < TABLE_NUM; i++)
        {
            if (_tables[i].data)
            {
                _tables[i].data = GetImageDataBySourceOffset(tableSourceOffsets[i]);
            }
        }
    }

    LoadImageErrorCode RawImageBase::BuildCompactImageData()
    {
        std::sort(_preservedRanges.begin(), _preservedRanges.end(), [](const RawImagePreservedRange& a, const RawImagePreservedRange& b) {
            return a.sourceOffset < b.sourceOffset;
        });

        std::vector<RawImagePreservedRange> merged;
        for (const RawImagePreservedRange& range : _preservedRanges)
        {
            if (merged.empty())
            {
                merged.push_back(range);
                continue;
            }
            RawImagePreservedRange& last = merged.back();
            uint32_t lastEnd = last.sourceOffset + last.size;
            if (range.sourceOffset <= lastEnd)
            {
                uint32_t rangeEnd = range.sourceOffset + range.size;
                last.size = std::max(lastEnd, rangeEnd) - last.sourceOffset;
            }
            else
            {
                merged.push_back(range);
            }
        }
        _preservedRanges.swap(merged);

        uint32_t compactLength = 0;
        for (RawImagePreservedRange& range : _preservedRanges)
        {
            range.compactOffset = compactLength;
            compactLength += range.size;
        }
        if (compactLength == 0 || compactLength >= _imageLength)
        {
            _compactImageLength = _imageLength;
            _isCompacted = false;
            return LoadImageErrorCode::OK;
        }

        const byte* originalImageData = _imageData;
        byte* compactImageData = (byte*)HYBRIDCLR_MALLOC(compactLength);
        for (const RawImagePreservedRange& range : _preservedRanges)
        {
            std::memcpy(compactImageData + range.compactOffset, originalImageData + range.sourceOffset, range.size);
        }

        _imageData = compactImageData;
        _compactImageLength = compactLength;
        _ptrRawDataEnd = _imageData + compactLength;
        _isCompacted = true;
        RedirectCompactPointers();

        HYBRIDCLR_FREE((void*)originalImageData);
        return LoadImageErrorCode::OK;
    }
```

- [ ] **Step 8: 替换 CompactRawImageData no-op**

替换为：

```cpp
    LoadImageErrorCode RawImageBase::CompactRawImageData()
    {
        if (_isCompacted)
        {
            return LoadImageErrorCode::OK;
        }

        _preservedRanges.clear();
        RecordCliStreamRange(_streamStringHeap, _streamStringHeapSourceOffset, "#Strings");
        RecordCliStreamRange(_streamUS, _streamUSSourceOffset, "#US");
        RecordCliStreamRange(_streamBlobHeap, _streamBlobHeapSourceOffset, "#Blob");
        RecordCliStreamRange(_streamGuidHeap, _streamGuidHeapSourceOffset, "#GUID");
        RecordCliStreamRange(_streamTables, _streamTablesSourceOffset, "#~");

        LoadImageErrorCode err = RecordMethodBodyRanges();
        if (err != LoadImageErrorCode::OK)
        {
            return err;
        }
        err = RecordFieldRvaRanges();
        if (err != LoadImageErrorCode::OK)
        {
            return err;
        }
        return BuildCompactImageData();
    }
```

- [ ] **Step 9: 编译 FS Android**

运行:

```powershell
cd D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler
$env:JAVA_HOME='D:\Program Files\Unity 2022.3.62f3\Editor\Data\PlaybackEngines\AndroidPlayer\OpenJDK'
$env:Path="$env:JAVA_HOME\bin;$env:Path"
& 'D:\bin\gradle-7.5.1\bin\gradle.bat' :launcher:assembleDebug --no-daemon --console=plain
```

预期: PASS。

---

### Task 5: 同步到 FSPatcher

**Files:**
- 修改: `D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\libil2cpp\hybridclr\metadata\RawImageBase.h`
- 修改: `D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\libil2cpp\hybridclr\metadata\RawImageBase.cpp`
- 修改: `D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\libil2cpp\hybridclr\metadata\Assembly.cpp`
- 修改 matching files under FS client FSPatcher package.

- [ ] **Step 1: 同步 AppIl2cpp 到 ECMA335 FSPatcher**

运行:

```powershell
$src='D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP\libil2cpp\hybridclr\metadata'
$dst='D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\libil2cpp\hybridclr\metadata'
Copy-Item -LiteralPath "$src\RawImageBase.h" -Destination "$dst\RawImageBase.h" -Force
Copy-Item -LiteralPath "$src\RawImageBase.cpp" -Destination "$dst\RawImageBase.cpp" -Force
Copy-Item -LiteralPath "$src\Assembly.cpp" -Destination "$dst\Assembly.cpp" -Force
```

预期: 三个文件时间戳更新。

- [ ] **Step 2: 同步 AppIl2cpp 到 FS 客户端 FSPatcher**

运行:

```powershell
$src='D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\unityLibrary\src\main\Il2CppOutputProject\IL2CPP\libil2cpp\hybridclr\metadata'
$dst='D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\libil2cpp\hybridclr\metadata'
Copy-Item -LiteralPath "$src\RawImageBase.h" -Destination "$dst\RawImageBase.h" -Force
Copy-Item -LiteralPath "$src\RawImageBase.cpp" -Destination "$dst\RawImageBase.cpp" -Force
Copy-Item -LiteralPath "$src\Assembly.cpp" -Destination "$dst\Assembly.cpp" -Force
```

预期: 三个文件时间戳更新。

- [ ] **Step 3: 提交 ECMA335 FSPatcher**

运行:

```powershell
git -C D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher status --short
git -C D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher add libil2cpp/hybridclr/metadata/RawImageBase.h libil2cpp/hybridclr/metadata/RawImageBase.cpp libil2cpp/hybridclr/metadata/Assembly.cpp
git -C D:\dr2\Misc\Ecma335UnitTest\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher commit -m "优化 RawImage 原始字节常驻内存"
```

预期: 生成 FSPatcher 提交。若该仓库有用户既有改动，只提交上述三个文件。

---

### Task 6: ECMA335 真机验证

**Files:**
- 使用: `D:\dr2\Misc\Ecma335UnitTest\AGENTS.md`
- 使用: `D:\dr2\Misc\Ecma335UnitTest\Docs\SuperpowerEcma335BclTestPlan.md`

- [ ] **Step 1: 构建 ECMA335 工程**

运行:

```powershell
cd D:\dr2\Misc\Ecma335UnitTest
.\00build.bat
```

预期: PASS。

- [ ] **Step 2: 安装到真机**

运行:

```powershell
cd D:\dr2\Misc\Ecma335UnitTest\out\android_proj
$env:ANDROID_SERIAL='1C111FDF600AW5'
$env:JAVA_HOME='D:\Program Files\Unity 2022.3.62f3\Editor\Data\PlaybackEngines\AndroidPlayer\OpenJDK'
$env:Path="$env:JAVA_HOME\bin;$env:Path"
& 'D:\bin\gradle-7.5.1\bin\gradle.bat' :launcher:installDebug --no-daemon --console=plain
```

预期: `BUILD SUCCESSFUL`。

- [ ] **Step 3: 启动并抓取验收日志**

运行:

```powershell
$env:ANDROID_SERIAL='1C111FDF600AW5'
adb shell pm clear com.wann.HybridCLRDemo
adb logcat -c
adb shell am start -n com.wann.HybridCLRDemo/com.unity3d.player.UnityPlayerActivity
Start-Sleep -Seconds 15
adb logcat -v time -d 2>&1 | Select-String -Pattern "StreamingAssets|path: .*Hotfix.dll.bytes|已从 StreamingAssets 加载热更程序集|已加载 ilasm ECMA-335 测试程序集|\[(ECMA335|BCL|REGRESSION)\].*(开始执行|总计|验收失败|失败:)"
```

预期:

```text
path: .../Hotfix.dll.bytes, exist: False
已从 StreamingAssets 加载热更程序集: Hotfix.dll
已加载 ilasm ECMA-335 测试程序集: Ecma335IlCases...
[ECMA335][AOT] 总计 94，失败 0
[BCL][AOT] 总计 28，失败 0
[ECMA335][HybridCLR] 总计 94，失败 0
[BCL][HybridCLR] 总计 28，失败 0
```

---

### Task 7: FS 真机构建与安装

**Files:**
- 使用: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler`

- [ ] **Step 1: 构建 FS APK**

运行:

```powershell
cd D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler
$env:JAVA_HOME='D:\Program Files\Unity 2022.3.62f3\Editor\Data\PlaybackEngines\AndroidPlayer\OpenJDK'
$env:Path="$env:JAVA_HOME\bin;$env:Path"
& 'D:\bin\gradle-7.5.1\bin\gradle.bat' :launcher:assembleDebug --no-daemon --console=plain
```

预期: `BUILD SUCCESSFUL`，APK 位于 `launcher\build\outputs\apk\debug\launcher-debug.apk`。

- [ ] **Step 2: 安装到 Pixel 6**

运行:

```powershell
$env:ANDROID_SERIAL='1C111FDF600AW5'
adb install -r D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\BuildCache\DR2NativeProfiler\launcher\build\outputs\apk\debug\launcher-debug.apk
```

预期: `Success`。

- [ ] **Step 3: 冒烟验证登录完成**

运行:

```powershell
$env:ANDROID_SERIAL='1C111FDF600AW5'
adb shell am force-stop com.fs.t.prf
adb logcat -c
adb shell am start -n com.fs.t.prf/com.dhplugin.unity.MainActivity
Start-Sleep -Seconds 120
adb logcat -v time -d | Select-String -Pattern "登录场景完成|FATAL EXCEPTION|CRASH|Abort|SIGSEGV"
```

预期: 输出 `登录场景完成`，没有 `FATAL EXCEPTION`、`CRASH`、`Abort`、`SIGSEGV`。

---

### Task 8: FS 登录场景 native heap 验收采集

**Files:**
- 使用: `D:\dr2\Misc\perfetto\00wann\run_heap_profile.sh`
- 生成: `D:\dr2\Misc\perfetto\00wann\PerfData\mem\<timestamp>`

- [ ] **Step 1: 登录完成触发采集**

运行:

```powershell
$env:ANDROID_SERIAL='1C111FDF600AW5'
cd D:\dr2\Misc\perfetto\00wann
bash run_heap_profile.sh
```

预期:

```text
Profiling active
heapprofd 已就绪，启动目标应用: com.fs.t.prf/com.dhplugin.unity.MainActivity
LOGIN_SCENE_DONE|pattern=登录场景完成|stable_seconds=30
HEAP_MEMINFO_VALIDATION=PASS
```

- [ ] **Step 2: 分析分类口径**

运行:

```powershell
cd D:\dr2\Misc\perfetto\00wann
bash run_heap_alloc_stacks_by_symbol_latest.sh
```

预期:

```text
生成 heap_analyze\summary.xlsx
生成 heap_analyze\pprof_categories
```

- [ ] **Step 3: 验收数字**

从最新 `heap_analyze\summary.xlsx` 或同目录分类输出读取：

```text
il2cpp_meta_mib = <数值>
hybridclr_mib = <数值>
combined_mib = il2cpp_meta_mib + hybridclr_mib
```

预期:

```text
combined_mib <= 134.85
CopyBytes 对应 Hotfix/AOT 原始字节常驻显著下降
heap_meminfo_validation.txt 包含 HEAP_MEMINFO_VALIDATION=PASS
logcat.txt 包含 登录场景完成
```

若 `combined_mib > 134.85`，不得声明最终目标完成，进入 Task 10。

---

### Task 9: 更新 FSPatcher 文档

**Files:**
- 修改: `D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj\Packages\com.code-philosophy.hybridclr\Data~\FSPatcher\docs\hybridclr_raw_image_resident_memory.md`

- [ ] **Step 1: 更新实现说明**

追加以下章节：

```markdown
## RawImage compact 实现

实现位置：
- `libil2cpp/hybridclr/metadata/RawImageBase.h`
- `libil2cpp/hybridclr/metadata/RawImageBase.cpp`
- `libil2cpp/hybridclr/metadata/Assembly.cpp`

实现逻辑：
1. `Assembly::Create` 与 `Assembly::LoadMetadataForAOTAssembly` 在 runtime metadata 初始化完成后调用 `RawImageBase::CompactRawImageData`。
2. compact 保留 `#Strings`、`#US`、`#Blob`、`#GUID`、`#~`、MethodBody 和 FieldRVA 数据。
3. compact 后原始 image offset 仍作为逻辑 offset；`GetDataPtrByImageOffset` 和默认值读取通过 range 映射返回 compact buffer 内的地址。
4. compact 完成后释放完整 PE 原始拷贝，降低 Hotfix/AOT 原始字节常驻。

验证要求：
- ECMA335 AOT 与 HybridCLR 均通过。
- FS 真机 `1C111FDF600AW5` 进入登录场景并输出 `登录场景完成`。
- native heap 采集 `heap_meminfo_validation` 通过。
- `il2cpp_meta + hybridclr` 合计不高于 134.85 MiB 时，才视为 50% 验收通过。
```

- [ ] **Step 2: 确认没有改 FS Wiki**

运行:

```powershell
git -C D:\dr2\Misc\perfetto status --short
```

预期: 没有 FS Wiki 路径。

---

### Task 10: 结果判定与第二阶段入口

**Files:**
- 使用: `D:\dr2\Misc\perfetto\00wann\PerfData\mem\<timestamp>\heap_analyze`
- 创建 if needed: `D:\dr2\Misc\perfetto\00wann\docs\superpowers\specs\<date>-hybridclr-runtime-metadata-phase2.md`

- [ ] **Step 1: 如果 RawImage compact 未达到 50%，定位剩余最大项**

运行:

```powershell
cd D:\dr2\Misc\perfetto\00wann
bash run_heap_alloc_stacks_by_symbol_latest.sh
```

预期: 在 `summary.xlsx` 中列出剩余 `il2cpp_meta` 与 `hybridclr` 最大栈。

- [ ] **Step 2: 写第二阶段证据**

如果 `combined_mib > 134.85`，新建第二阶段 spec，必须记录：

```text
优化后采集目录
登录完成日志时间
heap_meminfo_validation 结果
il2cpp_meta MiB
hybridclr MiB
combined MiB
距离 134.85 MiB 目标线差值
剩余 Top 10 栈
下一阶段候选：InterpreterImage runtime metadata、AOT homologous map、il2cpp_meta ClassInit、泛型缓存
```

- [ ] **Step 3: 停止最终完成声明**

若未达到 134.85 MiB，最终回复只能说明第一阶段 RawImage compact 已完成与剩余差距，不能说 50% 目标已完成。

---

## 完成标准

必须同时满足：

```text
ECMA335 AOT 总计 94，失败 0
ECMA335 HybridCLR 总计 94，失败 0
BCL AOT 总计 28，失败 0
BCL HybridCLR 总计 28，失败 0
FS 真机输出 登录场景完成
heap_meminfo_validation 为 PASS
CopyBytes 原始 DLL/AOT 常驻显著下降
il2cpp_meta + hybridclr <= 134.85 MiB
FSPatcher 同步完成并提交
FSPatcher docs 已更新
FS Wiki 未更新
```

## 自审结果

- 覆盖用户要求：常驻内存目标、真实证据、真机登录场景、ECMA335、FSPatcher 同步、文档落点均有任务。
- 类型一致性：计划内 native API 统一使用 `CompactRawImageData`、`RawImagePreservedRange`、`GetImageDataBySourceOffset`。
- 风险边界：RawImage compact 第一阶段不触碰 `il2cpp_meta/ClassInit` 与泛型缓存；若最终数字未达 50%，Task 10 强制进入第二阶段证据归因。


