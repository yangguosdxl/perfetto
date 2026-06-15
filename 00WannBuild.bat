@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

rem 双端构建入口：默认依次编译 Windows x64 和 Android arm64。
rem 可用参数：all、win、android、clean、help。
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "GN=%ROOT%\third_party\gn\gn.exe"
set "NINJA=%ROOT%\third_party\ninja\ninja.exe"
set "PYTHON=python"
set "WIN_OUT=out\win_clang"
set "ANDROID_OUT=out\android_arm64"

rem Android NDK 路径可通过环境变量覆盖：
rem   set ANDROID_NDK_ROOT=D:\Android\android-ndk-r28
if not defined ANDROID_NDK_ROOT set "ANDROID_NDK_ROOT=D:\Android\android-ndk-r28"
if not defined ANDROID_API_LEVEL set "ANDROID_API_LEVEL=21"
if not defined BUILD_JOBS set "BUILD_JOBS=%NUMBER_OF_PROCESSORS%"
if not defined BUILD_JOBS set "BUILD_JOBS=8"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=all"

if /i "%ACTION%"=="help" goto :Help
if /i "%ACTION%"=="-h" goto :Help
if /i "%ACTION%"=="--help" goto :Help

call :CheckCommonTools || exit /b !ERRORLEVEL!

if /i "%ACTION%"=="clean" (
  call :Clean
  exit /b !ERRORLEVEL!
)

call :SetupMsvcEnv || exit /b !ERRORLEVEL!

if /i "%ACTION%"=="win" (
  call :BuildWin
  exit /b !ERRORLEVEL!
)

if /i "%ACTION%"=="android" (
  call :BuildAndroid
  exit /b !ERRORLEVEL!
)

if /i "%ACTION%"=="all" (
  call :BuildWin || exit /b !ERRORLEVEL!
  call :BuildAndroid || exit /b !ERRORLEVEL!
  echo.
  echo [完成] Windows 与 Android 双端编译均已完成。
  exit /b 0
)

echo [错误] 未知参数：%ACTION%
echo.
goto :Help

:Help
echo 用法：
echo   00WannBuild.bat              编译 Windows + Android
echo   00WannBuild.bat all          编译 Windows + Android
echo   00WannBuild.bat win          只编译 Windows x64
echo   00WannBuild.bat android      只编译 Android arm64
echo   00WannBuild.bat clean        清理两个输出目录
echo.
echo 可覆盖的环境变量：
echo   ANDROID_NDK_ROOT             当前默认：%ANDROID_NDK_ROOT%
echo   ANDROID_API_LEVEL            当前默认：%ANDROID_API_LEVEL%
echo   BUILD_JOBS                   当前默认：%BUILD_JOBS%
exit /b 0

:CheckCommonTools
echo [检查] 基础构建工具
if not exist "%GN%" (
  echo [错误] 找不到 GN：%GN%
  exit /b 1
)
if not exist "%NINJA%" (
  echo [错误] 找不到 Ninja：%NINJA%
  exit /b 1
)
"%PYTHON%" --version >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 Python，请确认 python 在 PATH 中。
  exit /b 1
)
echo [检查] GN、Ninja、Python 可用。
exit /b 0

:SetupMsvcEnv
echo [检查] MSVC 与 Windows SDK
set "WINSDK_BASE="
set "WINSDK_VER="
set "MSVC_BASE="
set "_MSVC_INFO=%TEMP%\perfetto_msvc_paths_%RANDOM%.txt"

"%PYTHON%" "%ROOT%\gn\standalone\toolchain\win_find_msvc.py" > "%_MSVC_INFO%"
if errorlevel 1 (
  echo [错误] MSVC 探测脚本执行失败。
  if exist "%_MSVC_INFO%" del /f /q "%_MSVC_INFO%" >nul 2>nul
  exit /b 1
)

set /p WINSDK_BASE=<"%_MSVC_INFO%"
for /f "usebackq skip=1 delims=" %%L in ("%_MSVC_INFO%") do (
  if not defined WINSDK_VER (
    set "WINSDK_VER=%%L"
  ) else if not defined MSVC_BASE (
    set "MSVC_BASE=%%L"
  )
)
if exist "%_MSVC_INFO%" del /f /q "%_MSVC_INFO%" >nul 2>nul

if not defined WINSDK_BASE (
  echo [错误] 未找到 Windows SDK。
  exit /b 1
)
if not defined WINSDK_VER (
  echo [错误] 未找到 Windows SDK 版本。
  exit /b 1
)
if not defined MSVC_BASE (
  echo [错误] 未找到 MSVC 工具链。
  exit /b 1
)

rem clang-cl 需要 INCLUDE/LIB 才能找到 MSVC/WinSDK 头文件和库。
set "INCLUDE=%MSVC_BASE%\include;%WINSDK_BASE%\Include\%WINSDK_VER%\ucrt;%WINSDK_BASE%\Include\%WINSDK_VER%\um;%WINSDK_BASE%\Include\%WINSDK_VER%\shared;%WINSDK_BASE%\Include\%WINSDK_VER%\winrt;%WINSDK_BASE%\Include\%WINSDK_VER%\cppwinrt;%INCLUDE%"
set "LIB=%MSVC_BASE%\lib\x64;%WINSDK_BASE%\Lib\%WINSDK_VER%\ucrt\x64;%WINSDK_BASE%\Lib\%WINSDK_VER%\um\x64;%LIB%"

echo [检查] MSVC：%MSVC_BASE%
echo [检查] WinSDK：%WINSDK_BASE%\%WINSDK_VER%
exit /b 0

:BuildWin
echo.
echo [Windows] 写入 GN 参数：%WIN_OUT%\args.gn
if not exist "%ROOT%\%WIN_OUT%" mkdir "%ROOT%\%WIN_OUT%"
> "%ROOT%\%WIN_OUT%\args.gn" (
  echo # Windows x64 Release 构建配置
  echo is_debug = false
  echo is_clang = true
  echo skip_buildtools_check = true
)

pushd "%ROOT%" >nul
echo [Windows] 生成 Ninja 文件
"%GN%" gen "%WIN_OUT%"
if errorlevel 1 (
  popd >nul
  echo [Windows] GN gen 失败。
  exit /b 1
)

echo [Windows] 开始编译，线程数：%BUILD_JOBS%
"%NINJA%" -C "%WIN_OUT%" -j %BUILD_JOBS%
if errorlevel 1 (
  popd >nul
  echo [Windows] Ninja 编译失败。
  exit /b 1
)
popd >nul

echo [Windows] 编译完成：%ROOT%\%WIN_OUT%
exit /b 0

:BuildAndroid
echo.
echo [Android] 检查 NDK：%ANDROID_NDK_ROOT%
if not exist "%ANDROID_NDK_ROOT%\toolchains\llvm\prebuilt\windows-x86_64\bin\clang.exe" (
  echo [错误] 找不到 Android NDK clang。
  echo [错误] 当前检查路径：%ANDROID_NDK_ROOT%\toolchains\llvm\prebuilt\windows-x86_64\bin\clang.exe
  exit /b 1
)

set "ANDROID_NDK_ROOT_GN=%ANDROID_NDK_ROOT:\=/%"

echo [Android] 写入 GN 参数：%ANDROID_OUT%\args.gn
if not exist "%ROOT%\%ANDROID_OUT%" mkdir "%ROOT%\%ANDROID_OUT%"
> "%ROOT%\%ANDROID_OUT%\args.gn" (
  echo # Android arm64 Release 构建配置（Windows 宿主）
  echo target_os = "android"
  echo target_cpu = "arm64"
  echo android_ndk_root = "%ANDROID_NDK_ROOT_GN%"
  echo android_host = "windows-x86_64"
  echo skip_buildtools_check = true
  echo is_debug = false
  echo android_api_level = %ANDROID_API_LEVEL%
)

pushd "%ROOT%" >nul
echo [Android] 生成 Ninja 文件
"%GN%" gen "%ANDROID_OUT%"
if errorlevel 1 (
  popd >nul
  echo [Android] GN gen 失败。
  exit /b 1
)

echo [Android] 开始编译，线程数：%BUILD_JOBS%
"%NINJA%" -C "%ANDROID_OUT%" -j %BUILD_JOBS%
if errorlevel 1 (
  popd >nul
  echo [Android] Ninja 编译失败。
  exit /b 1
)
popd >nul

echo [Android] 编译完成：%ROOT%\%ANDROID_OUT%
echo [Android] 关键产物：
echo   %ROOT%\%ANDROID_OUT%\perfetto
echo   %ROOT%\%ANDROID_OUT%\traced
echo   %ROOT%\%ANDROID_OUT%\trace_processor_shell
echo   %ROOT%\%ANDROID_OUT%\libperfetto.so
exit /b 0

:Clean
echo [清理] 删除输出目录
if exist "%ROOT%\%WIN_OUT%" rmdir /s /q "%ROOT%\%WIN_OUT%"
if exist "%ROOT%\%ANDROID_OUT%" rmdir /s /q "%ROOT%\%ANDROID_OUT%"
echo [清理] 完成。
exit /b 0
