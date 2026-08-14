#!/usr/bin/env bash

# 这些函数只做宿主机工具探测，不执行采集逻辑；各入口脚本按需 source。
source config.sh

# 采集时只隐藏 ANR 等系统错误对话框，不影响系统记录 ANR。
ANDROID_ERROR_DIALOGS_ORIGINAL_VALUE=
ANDROID_ERROR_DIALOGS_SUPPRESSED=0

suppress_android_error_dialogs() {
  local adb_bin=${ADB_BINARY:-adb}
  local original_value
  if ! original_value=$("$adb_bin" shell settings get global hide_error_dialogs 2>/dev/null); then
    echo "ANDROID_ERROR_DIALOGS_WARN|action=read|setting=hide_error_dialogs" >&2
    return 0
  fi
  original_value=${original_value//$'\r'/}
  if ! "$adb_bin" shell settings put global hide_error_dialogs 1 >/dev/null 2>&1; then
    echo "ANDROID_ERROR_DIALOGS_WARN|action=suppress|setting=hide_error_dialogs|original=${original_value:-empty}" >&2
    return 0
  fi
  ANDROID_ERROR_DIALOGS_ORIGINAL_VALUE=$original_value
  ANDROID_ERROR_DIALOGS_SUPPRESSED=1
  echo "ANDROID_ERROR_DIALOGS|state=suppressed|original=${original_value:-empty}"
}

restore_android_error_dialogs() {
  if [[ "$ANDROID_ERROR_DIALOGS_SUPPRESSED" != "1" ]]; then
    return 0
  fi

  local adb_bin=${ADB_BINARY:-adb}
  local rc=0
  if [[ "$ANDROID_ERROR_DIALOGS_ORIGINAL_VALUE" == "null" ]]; then
    "$adb_bin" shell settings delete global hide_error_dialogs >/dev/null 2>&1 || rc=$?
  else
    "$adb_bin" shell settings put global hide_error_dialogs \
      "$ANDROID_ERROR_DIALOGS_ORIGINAL_VALUE" >/dev/null 2>&1 || rc=$?
  fi
  if [[ "$rc" -eq 0 ]]; then
    echo "ANDROID_ERROR_DIALOGS|state=restored|value=${ANDROID_ERROR_DIALOGS_ORIGINAL_VALUE:-empty}"
  else
    echo "ANDROID_ERROR_DIALOGS_WARN|action=restore|setting=hide_error_dialogs|original=${ANDROID_ERROR_DIALOGS_ORIGINAL_VALUE:-empty}|rc=$rc" >&2
  fi
  ANDROID_ERROR_DIALOGS_SUPPRESSED=0
  return 0
}

restore_android_error_dialogs_on_exit() {
  local rc=$?
  trap - EXIT
  restore_android_error_dialogs
  exit "$rc"
}

is_windows_git_bash() {
  [[ "${OSTYPE:-}" == msys* || "${MSYSTEM:-}" != "" ]]
}

select_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3 python py; do
    if [[ -n "$candidate" ]] && command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

select_perfetto_tool() {
  local tool=$1
  local perfetto_root=${2:-}
  local override=${3:-}
  local candidate
  local -a rels=()

  if [[ -n "$override" ]]; then
    printf '%s\n' "$override"
    return 0
  fi

  if [[ -n "$perfetto_root" ]]; then
    if is_windows_git_bash; then
      case "$tool" in
        trace_processor_shell)
          rels=(
            out/win_clang/trace_processor_shell.exe
            out/win/trace_processor_shell.exe
            out/linux_clang_release/trace_processor_shell
            out/android_arm64/trace_processor_shell
          )
          ;;
        traceconv)
          rels=(
            out/win_clang/traceconv.exe
            out/win/traceconv.exe
            out/android_arm64/msvc/traceconv.exe
            out/linux_clang_release/traceconv
            out/android_arm64/traceconv
          )
          ;;
      esac
    else
      case "$tool" in
        trace_processor_shell)
          rels=(
            out/linux_clang_release/trace_processor_shell
            out/android_arm64/trace_processor_shell
            out/win_clang/trace_processor_shell.exe
            out/win/trace_processor_shell.exe
          )
          ;;
        traceconv)
          rels=(
            out/linux_clang_release/traceconv
            out/android_arm64/traceconv
            out/android_arm64/msvc/traceconv.exe
            out/win_clang/traceconv.exe
            out/win/traceconv.exe
          )
          ;;
      esac
    fi

    local fallback=
    for candidate in "${rels[@]}"; do
      if [[ -z "$fallback" ]]; then
        fallback="$perfetto_root/$candidate"
      fi
      candidate="$perfetto_root/$candidate"
      if [[ -x "$candidate" ]]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    done
    if [[ -n "$fallback" ]]; then
      printf '%s\n' "$fallback"
      return 0
    fi
  fi

  for candidate in "$tool" "$tool.exe"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

unity_android_root() {
  local candidate
  for candidate in \
    "${UNITY_ANDROID_ROOT:-}" \
    "/d/Program Files/Unity 2022.3.62f3/Editor/Data/PlaybackEngines/AndroidPlayer"; do
    if [[ -n "$candidate" && -d "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

select_android_sdk_root() {
  local root
  for root in "${ANDROID_SDK_ROOT:-}" "${ANDROID_HOME:-}"; do
    if [[ -n "$root" && -d "$root" ]]; then
      printf '%s\n' "$root"
      return 0
    fi
  done
  root=$(unity_android_root || true)
  if [[ -n "$root" && -d "$root/SDK" ]]; then
    printf '%s\n' "$root/SDK"
    return 0
  fi
  return 1
}

select_android_ndk_root() {
  local root
  for root in "${ANDROID_NDK_ROOT:-}" "${ANDROID_NDK:-}"; do
    if [[ -n "$root" && -d "$root" ]]; then
      printf '%s\n' "$root"
      return 0
    fi
  done
  root=$(unity_android_root || true)
  if [[ -n "$root" && -d "$root/NDK" ]]; then
    printf '%s\n' "$root/NDK"
    return 0
  fi
  return 1
}

select_unity_jdk_bin() {
  local root
  root=$(unity_android_root || true)
  if [[ -n "$root" && -d "$root/OpenJDK/bin" ]]; then
    printf '%s\n' "$root/OpenJDK/bin"
    return 0
  fi
  return 1
}

select_build_tools_dir() {
  local sdk_root=$1
  local candidate
  if [[ -n "${ANDROID_BUILD_TOOLS:-}" && -d "${ANDROID_BUILD_TOOLS:-}" ]]; then
    printf '%s\n' "$ANDROID_BUILD_TOOLS"
    return 0
  fi
  for candidate in "$sdk_root/build-tools/26.0.1" "$sdk_root/build-tools/30.0.3"; do
    if [[ -d "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  find "$sdk_root/build-tools" -mindepth 1 -maxdepth 1 -type d 2>/dev/null |
    sort -V |
    tail -1
}

select_android_jar() {
  local sdk_root=$1
  local candidate
  if [[ -n "${ANDROID_JAR:-}" && -f "${ANDROID_JAR:-}" ]]; then
    printf '%s\n' "$ANDROID_JAR"
    return 0
  fi
  for candidate in "$sdk_root/platforms/android-23/android.jar" "$sdk_root/platforms/android-30/android.jar"; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  find "$sdk_root/platforms" -mindepth 2 -maxdepth 2 -name android.jar 2>/dev/null |
    sort -V |
    tail -1
}

find_build_tool() {
  local dir=$1
  local name=$2
  local candidate
  for candidate in "$dir/$name" "$dir/$name.exe" "$dir/$name.bat" "$dir/$name.cmd"; do
    if [[ -e "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

run_host_tool() {
  local tool=$1
  shift
  local win_tool
  if is_windows_git_bash; then
    win_tool=$(cygpath -w "$tool")
  else
    win_tool="$tool"
  fi
  case "${tool,,}" in
    *.cmd|*.bat)
      local cmdline="call \"$win_tool\""
      local arg converted prefix value
      for arg in "$@"; do
        converted=$arg
        if is_windows_git_bash; then
          case "$arg" in
            *=/*)
              prefix=${arg%%=*}
              value=${arg#*=}
              converted="$prefix=$(cygpath -w "$value")"
              ;;
            /*)
              converted=$(cygpath -w "$arg")
              ;;
          esac
        fi
        converted=${converted//\"/\\\"}
        cmdline="$cmdline \"$converted\""
      done
      cmd.exe /S /C "$cmdline"
      ;;
    *)
      "$win_tool" "$@"
      ;;
  esac
}

select_ndk_build() {
  local ndk_root=$1
  find_build_tool "$ndk_root" ndk-build
}

select_ndk_prebuilt_tag() {
  local ndk_root=$1
  local candidate
  for candidate in windows-x86_64 linux-x86_64 darwin-x86_64; do
    if [[ -d "$ndk_root/toolchains/llvm/prebuilt/$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

cpu_count() {
  if command -v nproc >/dev/null 2>&1; then
    nproc
  elif [[ -n "${NUMBER_OF_PROCESSORS:-}" ]]; then
    printf '%s\n' "$NUMBER_OF_PROCESSORS"
  else
    printf '4\n'
  fi
}
