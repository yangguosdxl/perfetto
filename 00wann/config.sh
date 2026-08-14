# 00wann 脚本在本目录执行，PerfettoRoot 使用相对路径指向仓库根目录。
export PerfettoRoot='../../perfetto'
export MMAP_PHYS_APP=com.tencent.dhwdxkty.trunk.profiler
export ANDROID_SERIAL=${ANDROID_SERIAL:-1C111FDF600AW5}
# 表加载完成后执行的 Python 测试模块，相对路径以 00wann 为基准。
export PERF_PROFILE_ACTION_SCRIPT=${PERF_PROFILE_ACTION_SCRIPT:-profile_actions/send_battle_record_gm.py}
# 主调用栈采集默认使用 root standalone traced_perf，避免 system service 的 nobody
# 身份无权读取正式 App 的 /proc/<pid>/maps 和 /proc/<pid>/mem。
export MMAP_PHYS_USE_ROOT_TRACED_PERF=${MMAP_PHYS_USE_ROOT_TRACED_PERF:-1}
