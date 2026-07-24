# run_heap_profile Device Parameter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--device <serial>` to `run_heap_profile.py` and its shell wrapper so every adb process in one Native heap profile run targets the same device without breaking the existing sampling arguments.

**Architecture:** Keep `run_heap_profile.sh` as a transparent argument-forwarding wrapper. Parse the named option in Python, then set `ANDROID_SERIAL` in the shared child-process environment before starting local adb commands, the fsboot shell helper, or Perfetto's `heap_profile.py`; Perfetto uses bare `adb` commands and therefore inherits the same selection.

**Tech Stack:** Python 3 standard library (`argparse`, `subprocess`), Bash, adb environment contract, existing fake-process integration test.

---

## File Structure

- Modify `test_run_heap_profile.sh`: extend the fake adb/fsboot/profiler fixtures and assert argument parsing plus end-to-end environment propagation.
- Modify `run_heap_profile.py`: parse `--device`, validate positional arguments, and apply the selected serial to `ANDROID_SERIAL`.
- Keep `run_heap_profile.sh` unchanged: it already forwards `"$@"` exactly.
- Modify `AGENTS.md`: make the required real-device invocation explicit.
- Modify `README.md`: document the public wrapper and Python controller interface.
- Modify `docs/heap_profile.md`: update focused Native heap collection instructions and validation examples.

### Task 1: Add Failing Device-Parameter Tests

**Files:**
- Modify: `test_run_heap_profile.sh`
- Test: `test_run_heap_profile.sh`

- [ ] **Step 1: Make each fake boundary record the inherited device**

Change the fake adb body to retain its existing command log and add a separate environment record:

```bash
printf 'adb %s\n' "$*" >>"$log_file"
printf 'ADB_ANDROID_SERIAL=%s\n' "${ANDROID_SERIAL:-}" >>"$log_file"
```

Replace the no-op fake `fsbootcmd_push_to_phone.sh` with:

```bash
cat >"$tmpdir/fsbootcmd_push_to_phone.sh" <<'EOF'
printf 'FSBOOT_ANDROID_SERIAL=%s\n' "${ANDROID_SERIAL:-}" >>"${TEST_LOG:?}"
EOF
```

Add this line to the fake `heap_profile.py` log block:

```python
log.write("HEAP_PROFILE_ANDROID_SERIAL=" +
          os.environ.get("ANDROID_SERIAL", "") + "\n")
```

- [ ] **Step 2: Add direct parser assertions**

After the existing `build_env` unit checks, import the copied controller and assert the wished-for API:

```python
device, duration, interval, shmem = module.parse_args(
    ["--device", "test-device", "128", "268435456"])
if device != "test-device":
  raise SystemExit(f"设备参数解析错误: {device!r}")
if duration or interval != ["-i", "128"] or shmem != ["--shmem-size", "268435456"]:
  raise SystemExit("设备参数不应改变现有采样参数")

device, duration, interval, shmem = module.parse_args([])
if device is not None:
  raise SystemExit("未传 --device 时不应强制选择设备")

for invalid in (["--device"], ["1024", "8388608", "extra"]):
  try:
    module.parse_args(invalid)
  except SystemExit:
    pass
  else:
    raise SystemExit(f"非法参数应失败: {invalid!r}")
```

- [ ] **Step 3: Add end-to-end propagation assertions**

Run the existing fake collection with a named device and assert all three boundaries receive it:

```bash
run_script "$TEST_LOG" --device test-device 128 268435456
for marker in \
  "ADB_ANDROID_SERIAL=test-device" \
  "FSBOOT_ANDROID_SERIAL=test-device" \
  "HEAP_PROFILE_ANDROID_SERIAL=test-device"; do
  if ! grep -Fq "$marker" "$TEST_LOG"; then
    echo "设备序列号未传到完整采集链: $marker"
    cat "$TEST_LOG"
    exit 1
  fi
done
```

- [ ] **Step 4: Run the test and verify RED**

Run:

```bash
bash test_run_heap_profile.sh
```

Expected: FAIL in the parser assertion because the current `parse_args()` returns three values and treats `--device` as the sampling interval. This proves the new test exercises missing behavior.

- [ ] **Step 5: Commit the failing test**

```bash
git add test_run_heap_profile.sh
git commit -m "测试：覆盖 heap profile 设备参数"
```

### Task 2: Parse and Propagate the Device Serial

**Files:**
- Modify: `run_heap_profile.py:12`
- Modify: `run_heap_profile.py:626`
- Modify: `run_heap_profile.py:643`
- Test: `test_run_heap_profile.sh`

- [ ] **Step 1: Import the standard parser**

Add the import with the other standard-library imports:

```python
import argparse
```

- [ ] **Step 2: Replace the permissive positional parser**

Replace `parse_args()` with:

```python
def parse_args(
    argv: list[str],
) -> tuple[str | None, list[str], list[str], list[str]]:
  parser = argparse.ArgumentParser(
      description="Collect a Perfetto Native heap profile for the FS app.")
  parser.add_argument(
      "--device",
      metavar="SERIAL",
      help="adb device serial; omitted to use adb's default device selection")
  parser.add_argument("profile_args", nargs="*", metavar="PROFILE_ARG")
  parsed = parser.parse_args(argv)

  args = list(parsed.profile_args)
  if args and args[0] == "45000":
    # Keep the historical AI validation entry point compatible. Collection
    # ends after the login marker and stable period, never at a fixed duration.
    args = args[1:]
  if len(args) > 2:
    parser.error("expected at most interval_bytes and shmem_size")

  interval_bytes = args[0] if len(args) >= 1 else "1024"
  shmem_size = args[1] if len(args) >= 2 else "8388608"
  duration_args: list[str] = []
  interval_args = ["-i", interval_bytes] if interval_bytes else []
  shmem_args = ["--shmem-size", shmem_size] if shmem_size else []
  return parsed.device, duration_args, interval_args, shmem_args
```

- [ ] **Step 3: Apply the serial before starting any child process**

In `main()`, parse before loading or launching tools and override the copied environment only when the option is present:

```python
def main(argv: list[str]) -> int:
  device_serial, duration_args, interval_args, shmem_args = parse_args(argv)
  script_dir = Path(__file__).resolve().parent
  os.chdir(script_dir)
  perfetto_root = load_perfetto_root(script_dir)
  env = build_env(perfetto_root, script_dir=script_dir)
  if device_serial:
    env["ANDROID_SERIAL"] = device_serial
  os.environ.update(env)

  traceconv = resolve_perfetto_tool(perfetto_root, "traceconv", "TRACECONV")
  trace_processor = resolve_perfetto_tool(perfetto_root, "trace_processor_shell",
                                          "TRACE_PROCESSOR")
```

Remove the old three-value `parse_args(argv)` call below tool resolution.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
bash test_run_heap_profile.sh
```

Expected: exit 0, ending with the existing success message, with the new parser and three environment markers accepted.

- [ ] **Step 5: Run static checks**

Run:

```bash
bash -n run_heap_profile.sh test_run_heap_profile.sh
python -m py_compile run_heap_profile.py
git diff --check
```

Expected: all commands exit 0 with no syntax, compile, or whitespace errors.

- [ ] **Step 6: Commit implementation**

```bash
git add run_heap_profile.py test_run_heap_profile.sh
git commit -m "功能：支持指定 heap profile 设备"
```

### Task 3: Update Usage Documentation

**Files:**
- Modify: `AGENTS.md:15`
- Modify: `README.md:403`
- Modify: `README.md:473`
- Modify: `docs/heap_profile.md:1`

- [ ] **Step 1: Update project validation instructions**

Change the Native heap instruction in `AGENTS.md` so its adjustable-sampling command is:

```text
00wann/run_heap_profile.sh --device 1C111FDF600AW5 <interval_bytes> <shmem_size>
```

State that AI real-device validation must pass `--device 1C111FDF600AW5` and must still omit duration.

- [ ] **Step 2: Update README command and parameter tables**

Add the device-specific invocation to both wrapper and Python controller sections:

```bash
./run_heap_profile.sh --device 1C111FDF600AW5
./run_heap_profile.sh --device 1C111FDF600AW5 1024 67108864
```

Add a named-parameter row:

```markdown
| `--device SERIAL` | adb 默认选机 | 指定 adb 设备序列号；本次采集的所有 adb 子进程使用同一设备。 |
```

Document that an existing external `ANDROID_SERIAL` remains effective when `--device` is omitted and that `--device` takes precedence when supplied.

- [ ] **Step 3: Update focused collection documentation**

In `docs/heap_profile.md`, use `--device 1C111FDF600AW5` in real-device and AI validation examples. Explain that the controller exports the selected value through `ANDROID_SERIAL` to its own adb commands, `fsbootcmd_push_to_phone.sh`, and Perfetto `heap_profile.py`.

- [ ] **Step 4: Check documentation and regression tests**

Run:

```bash
rg -n -- "--device|ANDROID_SERIAL|run_heap_profile.sh" AGENTS.md README.md docs/heap_profile.md
bash test_run_heap_profile.sh
git diff --check
```

Expected: all three documents describe the same interface; test and diff checks exit 0.

- [ ] **Step 5: Commit documentation**

```bash
git add AGENTS.md README.md docs/heap_profile.md
git commit -m "文档：说明 heap profile 设备参数"
```

### Task 4: Verify the Completed Change

**Files:**
- Verify: `run_heap_profile.py`
- Verify: `run_heap_profile.sh`
- Verify: `test_run_heap_profile.sh`
- Verify: `PerfData/mmap_phys/<timestamp>/memory_validation.json`

- [ ] **Step 1: Run the full local verification set**

```bash
bash test_run_heap_profile.sh
bash -n run_heap_profile.sh test_run_heap_profile.sh
python -m py_compile run_heap_profile.py
git diff --check
```

Expected: all commands exit 0; the shell test prints its success summary.

- [ ] **Step 2: Confirm the only connected test device is allowed**

```bash
adb devices
```

Expected: `1C111FDF600AW5` is present with state `device`. Do not run real-device validation against any other serial.

- [ ] **Step 3: Run the required 45-second no-stack mmap validation**

From Git Bash, run:

```bash
MMAP_PHYS_APP=com.fs.t.prf \
MMAP_PHYS_ACTIVITY=com.fs.t.prf/com.dhplugin.unity.MainActivity \
ANDROID_SERIAL=1C111FDF600AW5 \
./run_mmap_phys_profile.sh --no-mmap-callstacks -d 45000
```

Expected behavior: the script force-stops FS first, starts Perfetto before launching FS, collects for 45 seconds, does not enable heapprofd malloc, and writes a new `PerfData/mmap_phys/<timestamp>/memory_validation.json`. Do not use `adb monkey`; if the target scenario needs interaction, ask the user to operate it manually.

- [ ] **Step 4: Inspect no-stack health results**

Read the newest `memory_validation.json` and confirm:

```text
validation.status = pass
mmap.syscall_events > 0
mmap.smaps_snapshots > 0
trace_health loss/error counters = 0
```

If any warning or failure appears, inspect the trace, collector output, and Perfetto-source-backed event path; fix the cause and rerun rather than recording a failed result.

- [ ] **Step 5: Review final repository state**

```bash
git status --short
git log -4 --oneline
```

Expected: only generated validation data may remain untracked or ignored; the design, tests, implementation, and documentation commits are visible.
