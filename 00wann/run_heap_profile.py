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

APP = "com.fs.t.prf"
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


def adb_binary() -> str:
  return os.environ.get("ADB_BINARY", "adb")


def cp_binary() -> str:
  return os.environ.get("CP_BINARY", "cp")


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


def build_env(perfetto_root: Path) -> dict[str, str]:
  env = os.environ.copy()
  env["MSYS_NO_PATHCONV"] = "1"
  env["PERFETTO_SYMBOLIZER_MODE"] = "index"
  env["PERFETTO_BINARY_PATH"] = "./workspace/allsymbols/arm64-v8a"
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


def wait_for_pid(package_name: str, shutdown_requested: callable) -> str:
  pid = ""
  while not pid and not shutdown_requested():
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
  return pid


def parse_args(argv: list[str]) -> tuple[list[str], list[str], list[str]]:
  duration_ms = argv[0] if len(argv) >= 1 else ""
  interval_bytes = argv[1] if len(argv) >= 2 else "1024"
  shmem_size = argv[2] if len(argv) >= 3 else "8388608"

  duration_args = ["-d", duration_ms] if duration_ms else []
  interval_args = ["-i", interval_bytes] if interval_bytes else []
  shmem_args = ["--shmem-size", shmem_size] if shmem_size else []
  return duration_args, interval_args, shmem_args


def main(argv: list[str]) -> int:
  script_dir = Path(__file__).resolve().parent
  os.chdir(script_dir)
  perfetto_root = load_perfetto_root(script_dir)
  env = build_env(perfetto_root)
  os.environ.update(env)

  traceconv = resolve_perfetto_tool(perfetto_root, "traceconv", "TRACECONV")
  trace_processor = resolve_perfetto_tool(perfetto_root, "trace_processor_shell",
                                          "TRACE_PROCESSOR")
  duration_args, interval_args, shmem_args = parse_args(argv)

  out_dir = script_dir / "PerfData/mem" / _datetime.datetime.now().strftime(
      "%Y-%m-%d_%H-%M-%S")
  out_dir.mkdir(parents=True, exist_ok=True)

  if source_fsbootcmd(script_dir, env) != 0:
    return 1

  run([adb_binary(), "push", "debugconfig.txt", f"/sdcard/Android/data/{APP}/files"],
      env=env)

  shutdown_requested = False
  profiler_proc: subprocess.Popen[str] | None = None

  def request_shutdown(signum, _frame) -> None:
    nonlocal shutdown_requested
    shutdown_requested = True
    if profiler_proc and profiler_proc.poll() is None:
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
      profiler_proc.wait()
    pump_thread.join(timeout=5)
    return 1

  if not shutdown_requested:
    print(f"\nheapprofd 已就绪，启动目标应用: {APP}")
    run([adb_binary(), "shell", "monkey", "-p", APP, "1"], env=env)
    pid = wait_for_pid(APP, lambda: shutdown_requested)
    if pid:
      print(f"\r应用已启动: {APP} pid={pid}")

  meminfo_captured = False
  shutdown_timeout_s = int(
      os.environ.get("HEAP_PROFILE_SHUTDOWN_SIGNAL_TIMEOUT_S", "600"))
  if wait_for_log_pattern(logger, profiler_proc,
                          "Waiting for profiler shutdown", shutdown_timeout_s):
    meminfo_captured = capture_meminfo_snapshot(out_dir, APP)

  heap_profile_rc = profiler_proc.wait()
  pump_thread.join(timeout=5)
  run([cp_binary(), str(profile_log), str(profile_log_dest)], env=env)

  if not meminfo_captured and ((out_dir / "raw-trace").exists() or
                               (out_dir / "symbolized-trace").exists()):
    capture_meminfo_snapshot(out_dir, APP)

  validation_rc = validate_heap_profile_against_meminfo(out_dir, APP,
                                                        trace_processor)
  if heap_profile_rc != 0:
    print(f"HEAP_PROFILE_FAILED|rc={heap_profile_rc}|out_dir={out_dir}")
    return heap_profile_rc
  return validation_rc


if __name__ == "__main__":
  try:
    raise SystemExit(main(sys.argv[1:]))
  except BrokenPipeError:
    raise SystemExit(1)
