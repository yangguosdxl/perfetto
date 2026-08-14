#!/usr/bin/env python3
"""Native heap profile 采集入口。

该脚本保留 run_heap_profile.sh 的命令行参数，但用 Python 管理子进程和
SIGINT，确保人工 Ctrl+C 后仍等待 heap_profile.py 完成 trace 拉取和本地处理。
"""

from __future__ import annotations

import datetime as _datetime
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO

from device_test_framework.actions import (
    ProfileActionContext,
    load_action_module,
    resolve_action_module_path,
    run_profile_action_module,
)

APP = os.environ.get("MMAP_PHYS_APP")
LAUNCH_ACTIVITY = f"{APP}/com.dhplugin.unity.MainActivity"
RPC_LOCAL_PORT = int(os.environ.get("HEAP_PROFILE_RPC_LOCAL_PORT", "12346"))
TRACE_BUFFER_KIB = 63488
DEFAULT_FS_SYMBOLS_DIR = Path(
    r"D:\dr2\Trunk_LocalBuild\ClientPublish\DreamRivakes2_U3DProj"
    r"\BuildCache\Published\Android\DreamRivakes2.apk"
    r"\unityLibrary\symbols\arm64-v8a")
LEGACY_SYMBOLS_RELATIVE_DIR = Path("workspace/allsymbols/arm64-v8a")
APP_START_TIMEOUT_SECONDS = int(
    os.environ.get("HEAP_PROFILE_APP_START_TIMEOUT_S", "60"))
MALLOC_LIVE_SQL = ("select coalesce(sum(size), 0) as malloc_live_bytes "
                   "from heap_profile_allocation;")
HEALTH_SQL = (
    "select coalesce(sum(value), 0) as health_sum from stats where name in "
    "('traced_buf_bytes_overwritten','traced_buf_chunks_overwritten',"
    "'traced_buf_chunks_discarded','traced_buf_trace_writer_packet_loss',"
    "'traced_buf_patches_failed','traced_buf_abi_violations',"
    "'heapprofd_buffer_overran','heapprofd_client_error',"
    "'heapprofd_missing_packet','heapprofd_non_finalized_profile',"
    "'heapprofd_rejected_concurrent');")

WINDOWS_SIGBREAK_BRIDGE_CODE = r"""
import runpy
import signal
import sys


def _forward_sigbreak_to_sigint(signum, frame):
  handler = signal.getsignal(signal.SIGINT)
  if callable(handler):
    handler(signal.SIGINT, frame)
  elif handler == signal.SIG_DFL:
    raise KeyboardInterrupt


if hasattr(signal, "SIGBREAK"):
  signal.signal(signal.SIGBREAK, _forward_sigbreak_to_sigint)

script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
"""


class TeeLogger:

  def __init__(self, log_path: Path) -> None:
    self.log_path = log_path
    self._lines: list[str] = []
    self._lock = threading.Lock()

  def pump(self, proc: subprocess.Popen[str]) -> None:
    with self.log_path.open("w", encoding="utf-8", errors="replace") as log:
      assert proc.stdout is not None
      for line in proc.stdout:
        with self._lock:
          self._lines.append(line)
        sys.stdout.write(line)
        sys.stdout.flush()
        log.write(line)
        log.flush()

  def contains(self, pattern: str) -> bool:
    with self._lock:
      return any(pattern in line for line in self._lines)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
  return subprocess.run(cmd, check=False, **kwargs)


def build_heap_profile_command(inner_python: str, heap_profile_script: Path,
                               args: list[str]) -> list[str]:
  """Windows 用桥接进程把 Ctrl-Break 转成 Perfetto 期待的 SIGINT。"""
  if os.name == "nt":
    return [
        inner_python,
        "-c",
        WINDOWS_SIGBREAK_BRIDGE_CODE,
        str(heap_profile_script),
        *args,
    ]
  return [inner_python, str(heap_profile_script), *args]


def profiler_creation_flags() -> int:
  if os.name == "nt":
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
  return 0


def request_profiler_shutdown(profiler_proc: subprocess.Popen[str]) -> None:
  """请求 heap_profile.py 走 Perfetto 自身的 trace 收尾路径。"""
  if profiler_proc.poll() is not None:
    return
  try:
    if os.name == "nt":
      profiler_proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
      profiler_proc.send_signal(signal.SIGINT)
  except (ValueError, OSError) as exc:
    print(
        "HEAP_PROFILE_WARN|reason=shutdown_signal_failed|"
        f"pid={profiler_proc.pid}|error={exc}"
    )
    profiler_proc.terminate()


def wait_for_profiler_exit(
    profiler_proc: subprocess.Popen[str],
    timeout_s: int,
    shutdown_marker_seen: bool,
) -> tuple[int, bool]:
  """等待 heap_profile.py 退出；超时后强制终止，避免主脚本无限等待。"""
  if profiler_proc.poll() is not None:
    return profiler_proc.returncode or 0, False

  if not shutdown_marker_seen:
    print(
        "HEAP_PROFILE_FAILED|reason=profiler_shutdown_timeout|"
        f"timeout_s={timeout_s}|pid={profiler_proc.pid}"
    )
    request_profiler_shutdown(profiler_proc)
    return force_stop_profiler(profiler_proc), True

  try:
    return profiler_proc.wait(timeout=timeout_s), False
  except subprocess.TimeoutExpired:
    print(
        "HEAP_PROFILE_FAILED|reason=profiler_shutdown_timeout|"
        f"timeout_s={timeout_s}|pid={profiler_proc.pid}"
    )
    request_profiler_shutdown(profiler_proc)
    return force_stop_profiler(profiler_proc), True


def force_stop_profiler(profiler_proc: subprocess.Popen[str]) -> int:
  if profiler_proc.poll() is None:
    profiler_proc.terminate()
    try:
      return profiler_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
      profiler_proc.kill()
      return profiler_proc.wait(timeout=5)
  return profiler_proc.returncode or 0


def adb_binary() -> str:
  return os.environ.get("ADB_BINARY", "adb")


def cp_binary() -> str:
  return os.environ.get("CP_BINARY", "cp")


def start_logcat_capture(
    out_dir: Path, env: dict[str, str]
) -> tuple[subprocess.Popen[bytes], BinaryIO, BinaryIO]:
  """清空旧 logcat，并把登录阶段日志保存到本次输出目录。"""
  run([adb_binary(), "logcat", "-c"], env=env)
  stdout_file = (out_dir / "logcat.txt").open("wb")
  stderr_file = (out_dir / "logcat.err.txt").open("wb")
  proc = subprocess.Popen(
      [adb_binary(), "logcat", "-v", "time"],
      stdout=stdout_file,
      stderr=stderr_file,
      env=env,
  )
  return proc, stdout_file, stderr_file


def stop_logcat_capture(
    proc: subprocess.Popen[bytes] | None,
    stdout_file: BinaryIO | None,
    stderr_file: BinaryIO | None,
) -> None:
  """结束 adb logcat，避免采集完成后留下后台进程和未刷新的文件句柄。"""
  if proc and proc.poll() is None:
    proc.terminate()
    try:
      proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
      proc.kill()
      proc.wait(timeout=5)
  for file_obj in (stdout_file, stderr_file):
    if file_obj:
      file_obj.close()


def launch_target_app(env: dict[str, str]) -> subprocess.CompletedProcess:
  return run([adb_binary(), "shell", "am", "start", "-n", LAUNCH_ACTIVITY],
             env=env)


def resolve_shell_path(base_dir: Path, value: str) -> Path:
  """把 config.sh 中的 MSYS 路径转换成当前 Python 可访问的宿主机路径。"""
  if os.name == "nt" and value.startswith("/") and not value.startswith("//"):
    cygpath = shutil.which("cygpath")
    if cygpath:
      proc = subprocess.run(
          [cygpath, "-w", value],
          text=True,
          stdout=subprocess.PIPE,
          stderr=subprocess.DEVNULL,
          check=False,
      )
      if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).resolve()

  path = Path(value)
  if not path.is_absolute():
    path = base_dir / path
  return path.resolve()


def _is_executable(path: Path) -> bool:
  if os.name == "nt" and path.suffix.lower() not in (".exe", ".bat", ".cmd",
                                                      ".com"):
    return False
  return path.exists() and os.access(path, os.X_OK)


def resolve_perfetto_tool(perfetto_root: Path, tool: str,
                          env_name: str) -> Path:
  """按宿主机优先级选择 Perfetto 工具，兼容 Linux 和 Windows Git Bash。"""
  override = os.environ.get(env_name)
  if override:
    return Path(override)

  if os.name == "nt":
    candidates_by_tool = {
        "trace_processor_shell": [
            perfetto_root / "out/win_clang/trace_processor_shell.exe",
            perfetto_root / "out/win_clang/trace_processor_shell.cmd",
            perfetto_root / "out/win/trace_processor_shell.exe",
            perfetto_root / "out/win/trace_processor_shell.cmd",
            perfetto_root / "out/linux_clang_release/trace_processor_shell",
            perfetto_root / "out/android_arm64/trace_processor_shell",
        ],
        "traceconv": [
            perfetto_root / "out/win_clang/traceconv.exe",
            perfetto_root / "out/win_clang/traceconv.cmd",
            perfetto_root / "out/win/traceconv.exe",
            perfetto_root / "out/win/traceconv.cmd",
            perfetto_root / "out/android_arm64/msvc/traceconv.exe",
            perfetto_root / "out/linux_clang_release/traceconv",
            perfetto_root / "out/android_arm64/traceconv",
        ],
    }
  else:
    candidates_by_tool = {
        "trace_processor_shell": [
            perfetto_root / "out/linux_clang_release/trace_processor_shell",
            perfetto_root / "out/android_arm64/trace_processor_shell",
            perfetto_root / "out/win_clang/trace_processor_shell.exe",
            perfetto_root / "out/win/trace_processor_shell.exe",
        ],
        "traceconv": [
            perfetto_root / "out/linux_clang_release/traceconv",
            perfetto_root / "out/android_arm64/traceconv",
            perfetto_root / "out/android_arm64/msvc/traceconv.exe",
            perfetto_root / "out/win_clang/traceconv.exe",
            perfetto_root / "out/win/traceconv.exe",
        ],
    }

  for candidate in candidates_by_tool[tool]:
    if _is_executable(candidate):
      return candidate

  from_path = shutil.which(tool) or shutil.which(f"{tool}.exe")
  if from_path:
    return Path(from_path)

  print(f"HEAP_PROFILE_FAILED|reason=perfetto_tool_missing|tool={tool}",
        file=sys.stderr)
  for candidate in candidates_by_tool[tool]:
    print(f"  checked={candidate}", file=sys.stderr)
  raise SystemExit(1)


def load_perfetto_root(script_dir: Path) -> Path:
  cmd = "source config.sh; printf '%s' \"$PerfettoRoot\""
  proc = subprocess.run(
      ["bash", "-lc", cmd],
      cwd=script_dir,
      text=True,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      check=False,
  )
  if proc.returncode != 0 or not proc.stdout:
    print(
        f"HEAP_PROFILE_FAILED|reason=load_config_failed|stderr={proc.stderr.strip()}"
    )
    raise SystemExit(1)
  return resolve_shell_path(script_dir, proc.stdout.strip())


def source_fsbootcmd(script_dir: Path, env: dict[str, str]) -> int:
  proc = subprocess.run(
      ["bash", "-lc", "source fsbootcmd_push_to_phone.sh"],
      cwd=script_dir,
      env=env,
      check=False,
  )
  return proc.returncode


def append_symbol_path(paths: list[str], path: Path) -> None:
  value = str(path)
  if value not in paths:
    paths.append(value)


def default_symbol_paths(script_dir: Path) -> list[str]:
  """返回 traceconv 默认符号目录，优先使用当前 FS 打包产物。"""
  paths: list[str] = []
  explicit_fs_symbols = os.environ.get("RUN_HEAP_PROFILE_SYMBOLS_DIR")
  if explicit_fs_symbols:
    append_symbol_path(paths, resolve_shell_path(script_dir, explicit_fs_symbols))
  elif DEFAULT_FS_SYMBOLS_DIR.exists():
    append_symbol_path(paths, DEFAULT_FS_SYMBOLS_DIR)

  # 旧目录里仍可能有 libBattleLogic.so/libprotobuf.so 等补充符号。
  legacy_symbols = script_dir / LEGACY_SYMBOLS_RELATIVE_DIR
  if legacy_symbols.exists():
    append_symbol_path(paths, legacy_symbols.resolve())
  return paths


def build_env(perfetto_root: Path, script_dir: Path | None = None) -> dict[str, str]:
  env = os.environ.copy()
  env["MSYS_NO_PATHCONV"] = "1"
  env["PERFETTO_SYMBOLIZER_MODE"] = "index"
  if "PERFETTO_BINARY_PATH" not in env:
    symbol_script_dir = script_dir if script_dir is not None else Path.cwd()
    symbol_paths = default_symbol_paths(symbol_script_dir)
    if symbol_paths:
      env["PERFETTO_BINARY_PATH"] = os.pathsep.join(symbol_paths)
  clang_bins = (
      [perfetto_root / "buildtools/win/clang/bin"]
      if os.name == "nt" else []) + [
          perfetto_root / "buildtools/linux64/clang/bin",
      ]
  existing_clang_bins = [str(path) for path in clang_bins if path.exists()]
  if existing_clang_bins:
    env["PATH"] = os.pathsep.join(
        [*existing_clang_bins, env.get("PATH", "")])
  extra_path = env.get("RUN_HEAP_PROFILE_EXTRA_PATH")
  if extra_path:
    env["PATH"] = f"{extra_path}{os.pathsep}{env.get('PATH', '')}"
  python_path = str(perfetto_root / "python")
  if env.get("PYTHONPATH"):
    python_path = f"{python_path}{os.pathsep}{env['PYTHONPATH']}"
  env["PYTHONPATH"] = python_path
  env["PYTHONUNBUFFERED"] = "1"
  return env


def query_trace_value(trace_processor: Path, query: str,
                      trace_path: Path) -> str:
  proc = subprocess.run(
      [str(trace_processor), "-Q", query,
       str(trace_path)],
      stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL,
      text=True,
      check=False,
  )
  lines = proc.stdout.splitlines()
  if not lines:
    return ""
  return lines[-1].replace('"', "").replace("\r", "")


def parse_meminfo_native_heap_alloc_bytes(meminfo_path: Path) -> int:
  with meminfo_path.open(encoding="utf-8", errors="replace") as meminfo:
    for line in meminfo:
      parts = line.split()
      if len(parts) >= 9 and parts[0] == "Native" and parts[1] == "Heap":
        return int(parts[8].replace(",", "")) * 1024
  raise ValueError("parse_native_heap_alloc_failed")


def capture_meminfo_snapshot(out_dir: Path, package_name: str) -> bool:
  meminfo_path = out_dir / "dumpsys_meminfo.txt"
  tmp_path = meminfo_path.with_suffix(".txt.tmp")
  print(f"\n抓取采集后 meminfo: adb shell dumpsys meminfo {package_name}")
  with tmp_path.open("wb") as out:
    proc = subprocess.run(
        [adb_binary(), "shell", "dumpsys", "meminfo", package_name],
        stdout=out,
        stderr=subprocess.DEVNULL,
        check=False,
    )
  if proc.returncode == 0:
    if b"No process found for:" in tmp_path.read_bytes():
      tmp_path.unlink(missing_ok=True)
      return False
    tmp_path.replace(meminfo_path)
    return True
  tmp_path.unlink(missing_ok=True)
  return False


def count_heap_dump_files(out_dir: Path) -> int:
  return len(list(out_dir.glob("heap_dump.*.pb"))) + len(
      list(out_dir.glob("heap_dump.*.pb.gz")))


def write_validation(validation_path: Path,
                     lines: list[str],
                     append: bool = False) -> None:
  mode = "a" if append else "w"
  with validation_path.open(mode, encoding="utf-8") as out:
    for line in lines:
      print(line)
      out.write(line + "\n")


def append_run_summary(out_dir: Path, line: str) -> None:
  with (out_dir / "run_summary.txt").open("a", encoding="utf-8") as summary:
    summary.write(line + "\n")


def print_profile_config(
    perfetto_root: Path,
    traceconv: Path,
    trace_processor: Path,
    env: dict[str, str],
    interval_bytes: str,
    shmem_size_bytes: str,
    action_module_path: Path,
) -> tuple[str, str]:
  """在真机操作前输出本轮关键配置。"""
  serial = env.get("ANDROID_SERIAL", "")
  config_line = (
      "HEAP_PROFILE_CONFIG|"
      f"app={APP}|activity={LAUNCH_ACTIVITY}|serial={serial}|"
      f"interval_bytes={interval_bytes}|shmem_size_bytes={shmem_size_bytes}|"
      f"trace_buffer_kib={TRACE_BUFFER_KIB}|"
      f"rpc_local_port={RPC_LOCAL_PORT}|action_script={action_module_path}"
  )
  tool_line = (
      "HEAP_PROFILE_TOOLS|"
      f"perfetto_root={perfetto_root}|traceconv={traceconv}|"
      f"trace_processor={trace_processor}|"
      f"symbol_paths={env.get('PERFETTO_BINARY_PATH', '')}"
  )
  print(config_line)
  print(tool_line)
  return config_line, tool_line


def write_profile_config_snapshot(out_dir: Path,
                                  config_lines: tuple[str, str]) -> None:
  """heapprofd 启动后再保存配置，避免提前占用 Perfetto 输出目录。"""
  config_line, tool_line = config_lines
  (out_dir / "heap_profile_config.txt").write_text(
      config_line + "\n" + tool_line + "\n", encoding="utf-8")
  append_run_summary(out_dir, config_line)
  append_run_summary(out_dir, tool_line)


def validate_heap_profile_against_meminfo(
    out_dir: Path,
    package_name: str,
    trace_processor: Path,
) -> int:
  meminfo_path = out_dir / "dumpsys_meminfo.txt"
  validation_path = out_dir / "heap_meminfo_validation.txt"
  trace_path = out_dir / "symbolized-trace"
  if not trace_path.exists():
    trace_path = out_dir / "raw-trace"

  if not meminfo_path.exists() and not capture_meminfo_snapshot(
      out_dir, package_name):
    write_validation(
        validation_path,
        [
            f"HEAP_MEMINFO_VALIDATION=FAIL|reason=dumpsys_meminfo_failed|path={meminfo_path}"
        ],
    )
    return 1

  if not trace_path.exists():
    write_validation(
        validation_path,
        [
            f"HEAP_MEMINFO_VALIDATION=FAIL|reason=trace_missing|checked={trace_path}"
        ],
    )
    return 1

  try:
    meminfo_alloc_bytes = parse_meminfo_native_heap_alloc_bytes(meminfo_path)
  except ValueError:
    write_validation(
        validation_path,
        [
            f"HEAP_MEMINFO_VALIDATION=FAIL|reason=parse_native_heap_alloc_failed|meminfo={meminfo_path}"
        ],
    )
    return 1

  malloc_live_bytes_text = query_trace_value(trace_processor, MALLOC_LIVE_SQL,
                                             trace_path)
  if not re.fullmatch(r"-?\d+", malloc_live_bytes_text or ""):
    write_validation(
        validation_path,
        [
            f"HEAP_MEMINFO_VALIDATION=FAIL|reason=query_malloc_live_failed|trace={trace_path}|value={malloc_live_bytes_text}"
        ],
    )
    return 1
  malloc_live_bytes = int(malloc_live_bytes_text)

  health_sum = query_trace_value(trace_processor, HEALTH_SQL, trace_path)
  if not re.fullmatch(r"-?\d+", health_sum or ""):
    health_sum = "unknown"

  diff_bytes = abs(malloc_live_bytes - meminfo_alloc_bytes)
  allowed_diff_bytes = int(
      os.environ.get("HEAP_PROFILE_MEMINFO_ALLOWED_DIFF_BYTES", "67108864"))
  heap_dump_count = count_heap_dump_files(out_dir)

  write_validation(
      validation_path,
      [
          f"malloc_live_bytes={malloc_live_bytes}",
          f"meminfo_native_heap_alloc_bytes={meminfo_alloc_bytes}",
          f"diff_bytes={diff_bytes}",
          f"allowed_diff_bytes={allowed_diff_bytes}",
          f"health_sum={health_sum}",
          f"heap_dump_count={heap_dump_count}",
          f"trace_path={trace_path}",
          f"meminfo_path={meminfo_path}",
          f"live_sql={MALLOC_LIVE_SQL}",
      ],
  )

  if diff_bytes <= allowed_diff_bytes:
    write_validation(
        validation_path,
        [
            "HEAP_MEMINFO_VALIDATION=PASS|"
            f"malloc_live_bytes={malloc_live_bytes}|"
            f"meminfo_native_heap_alloc_bytes={meminfo_alloc_bytes}|"
            f"diff_bytes={diff_bytes}|allowed_diff_bytes={allowed_diff_bytes}"
        ],
        append=True,
    )
    return 0

  write_validation(
      validation_path,
      [
          "HEAP_MEMINFO_VALIDATION=FAIL|"
          "reason=malloc_live_not_comparable_to_meminfo_alloc|"
          f"malloc_live_bytes={malloc_live_bytes}|"
          f"meminfo_native_heap_alloc_bytes={meminfo_alloc_bytes}|"
          f"diff_bytes={diff_bytes}|allowed_diff_bytes={allowed_diff_bytes}|"
          f"health_sum={health_sum}|heap_dump_count={heap_dump_count}"
      ],
      append=True,
  )
  return 1


def wait_for_log_pattern(
    logger: TeeLogger,
    proc: subprocess.Popen[str],
    pattern: str,
    timeout_s: int,
) -> bool:
  deadline = time.time() + timeout_s
  while time.time() < deadline:
    if logger.contains(pattern):
      return True
    if proc.poll() is not None:
      return False
    time.sleep(0.5)
  return False


def target_process_is_alive(package_name: str, expected_pid: str,
                            env: dict[str, str]) -> bool:
  proc = run(
      [adb_binary(), "shell", "pidof", package_name],
      env=env,
      stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL,
      text=True,
  )
  return proc.returncode == 0 and expected_pid in proc.stdout.split()


def wait_for_pid(package_name: str, shutdown_requested: callable,
                 timeout_s: int) -> str:
  pid = ""
  deadline = time.time() + timeout_s
  while not pid and not shutdown_requested() and time.time() < deadline:
    proc = subprocess.run(
        [adb_binary(), "shell", "pidof", package_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    pid = proc.stdout.replace("\r", "").strip()
    if pid:
      break
    print(f"\r等待应用启动: {package_name} ...", end="", flush=True)
    time.sleep(0.2)
  if not pid and not shutdown_requested():
    print(
        "\nHEAP_PROFILE_FAILED|reason=app_start_timeout|"
        f"package={package_name}|timeout_s={timeout_s}"
    )
  return pid


def parse_args(argv: list[str]) -> tuple[list[str], list[str], list[str]]:
  args = list(argv)
  if args and args[0] == "45000":
    # 兼容历史 AI 验证入口。Native heap 采集不能再按固定时长收尾，
    # 真实结束条件由配置的测试脚本协程决定。
    args = args[1:]
  interval_bytes = args[0] if len(args) >= 1 else "1024"
  shmem_size = args[1] if len(args) >= 2 else "8388608"

  duration_args: list[str] = []
  interval_args = ["-i", interval_bytes] if interval_bytes else []
  shmem_args = ["--shmem-size", shmem_size] if shmem_size else []
  return duration_args, interval_args, shmem_args


def main(argv: list[str]) -> int:
  script_dir = Path(__file__).resolve().parent
  os.chdir(script_dir)
  if not APP:
    print(
        "HEAP_PROFILE_FAILED|reason=app_config_missing|"
        f"config={script_dir / 'config.sh'}|name=MMAP_PHYS_APP"
    )
    return 1
  perfetto_root = load_perfetto_root(script_dir)
  env = build_env(perfetto_root, script_dir=script_dir)
  os.environ.update(env)

  traceconv = resolve_perfetto_tool(perfetto_root, "traceconv", "TRACECONV")
  trace_processor = resolve_perfetto_tool(perfetto_root, "trace_processor_shell",
                                          "TRACE_PROCESSOR")
  duration_args, interval_args, shmem_args = parse_args(argv)
  interval_bytes = interval_args[1] if interval_args else ""
  shmem_size_bytes = shmem_args[1] if shmem_args else ""
  try:
    action_module_path = resolve_action_module_path(
        script_dir, os.environ.get("PERF_PROFILE_ACTION_SCRIPT", ""))
    action_module = load_action_module(action_module_path)
  except (OSError, RuntimeError) as exc:
    print(f"HEAP_PROFILE_FAILED|reason=action_script_invalid|error={exc}")
    return 1

  configured_output = os.environ.get("DEVICE_TEST_OUTPUT_DIR", "").strip()
  out_dir = (
      Path(configured_output).resolve() if configured_output else
      script_dir / "PerfData/mem" / _datetime.datetime.now().strftime(
          "%Y-%m-%d_%H-%M-%S"))
  config_lines = print_profile_config(
      perfetto_root,
      traceconv,
      trace_processor,
      env,
      interval_bytes,
      shmem_size_bytes,
      action_module_path,
  )
  out_dir.mkdir(parents=True, exist_ok=True)

  if source_fsbootcmd(script_dir, env) != 0:
    return 1

  run([adb_binary(), "push", "debugconfig.txt", f"/sdcard/Android/data/{APP}/files"],
      env=env)

  shutdown_requested = False
  profiler_proc: subprocess.Popen[str] | None = None
  logcat_proc: subprocess.Popen[bytes] | None = None
  logcat_stdout: BinaryIO | None = None
  logcat_stderr: BinaryIO | None = None
  app_start_failed = False
  app_start_timeout_failed = False
  profiler_shutdown_timeout_failed = False
  action_failed = False
  action_running = False

  def request_shutdown(signum, _frame) -> None:
    nonlocal shutdown_requested
    shutdown_requested = True
    if profiler_proc and profiler_proc.poll() is None and not action_running:
      print("\n收到中断信号，已请求 heap_profile.py 停止采集并等待 trace 收尾...")
      request_profiler_shutdown(profiler_proc)

  signal.signal(signal.SIGINT, request_shutdown)
  signal.signal(signal.SIGTERM, request_shutdown)

  print(f"\n重启目标应用以覆盖启动后的 native malloc 分配: {APP}")
  run([adb_binary(), "shell", "am", "force-stop", APP], env=env)
  time.sleep(1)

  prebuilt_traceconv = Path.home() / ".local/share/perfetto/prebuilts/traceconv"
  prebuilt_traceconv.parent.mkdir(parents=True, exist_ok=True)
  run([cp_binary(), str(traceconv), str(prebuilt_traceconv)], env=env)

  profile_log = Path(tempfile.gettempdir()
                    ) / f"run_heap_profile_{int(time.time())}_{os.getpid()}.log"
  profile_log_dest = out_dir / "heap_profile.log"
  logger = TeeLogger(profile_log)
  inner_python_override = os.environ.get("RUN_HEAP_PROFILE_INNER_PYTHON")
  inner_python = (
      str(resolve_shell_path(script_dir, inner_python_override))
      if inner_python_override else sys.executable)
  heap_profile_args = [
      "-n",
      APP,
      "-o",
      str(out_dir),
      "--no-running",
      "--traceconv-binary",
      str(traceconv),
      "--trace-processor-binary",
      str(trace_processor),
      *duration_args,
      *interval_args,
      *shmem_args,
  ]
  cmd = build_heap_profile_command(
      inner_python, perfetto_root / "python/tools/heap_profile.py",
      heap_profile_args)
  profiler_proc = subprocess.Popen(
      cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
      env=env,
      bufsize=1,
      creationflags=profiler_creation_flags(),
  )
  pump_thread = threading.Thread(
      target=logger.pump, args=(profiler_proc,), daemon=True)
  pump_thread.start()

  active_timeout_s = int(os.environ.get("HEAP_PROFILE_ACTIVE_TIMEOUT_S", "60"))
  if not wait_for_log_pattern(logger, profiler_proc, "Profiling active",
                              active_timeout_s):
    run([cp_binary(), str(profile_log), str(profile_log_dest)], env=env)
    print(
        f"HEAP_PROFILE_FAILED|reason=profiling_active_timeout|out_dir={out_dir}|log={profile_log_dest}"
    )
    if profiler_proc.poll() is None:
      request_profiler_shutdown(profiler_proc)
      force_stop_profiler(profiler_proc)
    pump_thread.join(timeout=5)
    return 1
  write_profile_config_snapshot(out_dir, config_lines)

  try:
    if not shutdown_requested:
      logcat_proc, logcat_stdout, logcat_stderr = start_logcat_capture(
          out_dir, env)
      print(f"\nheapprofd 已就绪，启动目标 Activity: {LAUNCH_ACTIVITY}")
      launch_proc = launch_target_app(env)
      if launch_proc.returncode != 0:
        app_start_failed = True
        print(
            "HEAP_PROFILE_FAILED|reason=app_start_failed|"
            f"rc={launch_proc.returncode}|activity={LAUNCH_ACTIVITY}"
        )
        request_profiler_shutdown(profiler_proc)
        pid = ""
      else:
        pid = wait_for_pid(APP, lambda: shutdown_requested,
                           APP_START_TIMEOUT_SECONDS)
        if not pid and not shutdown_requested:
          app_start_timeout_failed = True
          request_profiler_shutdown(profiler_proc)
      if pid:
        print(f"\r应用已启动 {APP} pid={pid}")
        context = ProfileActionContext(
            app=APP,
            pid=int(pid),
            output_dir=out_dir.resolve(),
            logcat_path=(out_dir / "logcat.txt").resolve(),
            adb=adb_binary(),
            rpc_local_port=RPC_LOCAL_PORT,
            android_serial=env.get("ANDROID_SERIAL", ""),
            rpc_timeout_seconds=float(
                os.environ.get("HEAP_PROFILE_RPC_TIMEOUT_S", "30")),
            summary_path=(out_dir / "run_summary.txt").resolve(),
        )
        action_running = True
        try:
          action_result = run_profile_action_module(
              action_module,
              context,
              lambda: shutdown_requested,
              lambda: target_process_is_alive(APP, pid, env),
          )
        finally:
          action_running = False
        action_failed = not action_result.success
        request_profiler_shutdown(profiler_proc)
  finally:
    stop_logcat_capture(logcat_proc, logcat_stdout, logcat_stderr)

  meminfo_captured = False
  shutdown_timeout_s = int(
      os.environ.get("HEAP_PROFILE_SHUTDOWN_SIGNAL_TIMEOUT_S", "600"))
  shutdown_marker_seen = wait_for_log_pattern(
      logger, profiler_proc, "Waiting for profiler shutdown", shutdown_timeout_s)
  if shutdown_marker_seen:
    meminfo_captured = capture_meminfo_snapshot(out_dir, APP)

  heap_profile_rc, profiler_shutdown_timeout_failed = wait_for_profiler_exit(
      profiler_proc, shutdown_timeout_s, shutdown_marker_seen)
  pump_thread.join(timeout=5)
  run([cp_binary(), str(profile_log), str(profile_log_dest)], env=env)

  if profiler_shutdown_timeout_failed:
    return 1
  if app_start_failed or app_start_timeout_failed:
    return 1

  if not meminfo_captured and ((out_dir / "raw-trace").exists() or
                               (out_dir / "symbolized-trace").exists()):
    capture_meminfo_snapshot(out_dir, APP)

  validation_rc = validate_heap_profile_against_meminfo(out_dir, APP,
                                                        trace_processor)
  if heap_profile_rc != 0:
    print(f"HEAP_PROFILE_FAILED|rc={heap_profile_rc}|out_dir={out_dir}")
    return heap_profile_rc
  if action_failed:
    print(f"HEAP_PROFILE_FAILED|reason=profile_action_failed|out_dir={out_dir}")
    return 1
  return validation_rc


if __name__ == "__main__":
  try:
    raise SystemExit(main(sys.argv[1:]))
  except BrokenPipeError:
    raise SystemExit(1)
