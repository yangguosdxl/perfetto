# `dumpsys meminfo` 指标说明

本文解释命令：

```bash
adb shell dumpsys meminfo com.tencent.dhwdxkty.trunk.profiler
```

所有数值默认单位都是 KB。`dumpsys meminfo` 是一次瞬时快照，主表的物理内存口径主要来自目标进程的 `/proc/<pid>/smaps`，显存中 smaps 无法覆盖的部分来自 Android memtrack HAL，`Heap Size/Alloc/Free` 来自进程内 native/Java 运行时接口。

## 本次输出快照

```text
Applications Memory Usage (in Kilobytes):
Uptime: 58245254 Realtime: 58245254

** MEMINFO in pid 20012 [com.tencent.dhwdxkty.trunk.profiler] **
                   Pss  Private  Private     Swap      Rss     Heap     Heap     Heap
                 Total    Dirty    Clean    Dirty    Total     Size    Alloc     Free
                ------   ------   ------   ------   ------   ------   ------   ------
  Native Heap   901372   901300        0        0   904892  1014288   904628   100148
  Dalvik Heap     2063     1876        0        0     9732    28131     3555    24576
 Dalvik Other     1982     1776        0        0     2728
        Stack     4136     4136        0        0     4144
       Ashmem      170      152        0        0      732
    Other dev       28        0       24        0      396
     .so mmap   149237    14784   121184        0   219264
    .jar mmap     3114        0      292        0    41228
    .apk mmap    36778      368    22928        0    56044
    .dex mmap     6589       20     3960        0    10024
    .oat mmap       94        0        0        0     1952
    .art mmap     6635     6088       44        0    27140
   Other mmap    31672       12    31296        0    33544
   EGL mtrack    67792    67792        0        0    67792
    GL mtrack   356552   356552        0        0   356552
      Unknown   446787   446664      112        0   447588
        TOTAL  2015001  1801520   179840        0  2183752  1042419   908183   124724
```

本次最关键的读数：

```text
Native malloc/allocator 私有脏页: 901300 KB
Native allocator 账面统计: Heap Size 1014288 KB, Heap Alloc 904628 KB, Heap Free 100148 KB
显存相关 PSS: EGL mtrack 67792 KB + GL mtrack 356552 KB = 424344 KB
TOTAL PSS: 2015001 KB
TOTAL RSS: 2183752 KB
```

## 数据来源总览

```text
adb shell dumpsys meminfo <package>
  -> system_server 的 ActivityManagerService 选中目标进程
  -> system_server 侧 Debug.getMemoryInfo(pid, mi)
  -> libmeminfo 扫描 /proc/<pid>/smaps，并按 VMA 名称分类到 Debug.MemoryInfo
  -> memtrack HAL 补充 smaps 未统计的 EGL/GL/Other 设备内存
  -> 目标 App 进程内 ActivityThread.dumpMemInfo() 补充 Heap/Objects/SQL/DATABASES
  -> ActivityThread.dumpMemInfoTable() 打印主表和 App Summary
```

依据：

- Android 官方 `dumpsys` 文档说明 `meminfo` 用于展示 App RAM 按分配类型拆分，并解释 PSS、Private Dirty、Private Clean 等概念。
- AOSP `ActivityManagerService` 打印 `Applications Memory Usage`、`Uptime`、`Realtime` 并调度目标进程 dump。
- AOSP `ActivityThread.dumpMemInfoTable()` 决定主表和 `App Summary` 的输出结构；`ActivityThread.dumpMemInfo()` 补充 `Objects`、`SQL`、`DATABASES` 等进程内统计。
- AOSP `Debug.MemoryInfo` 定义 `nativePss/nativePrivateDirty/otherStats` 等字段、行标签和 Summary 聚合公式。
- AOSP `android_os_Debug.cpp` 调用 `ExtractAndroidHeapStats()` 解析 smaps，并调用 memtrack 读取 graphics/gl/other。
- Android `libmeminfo/androidprocheaps.cpp` 按 VMA 名称把 smaps 条目分到 Native Heap、Dalvik、Stack、`.so mmap`、`Other mmap` 等行。
- Linux kernel `fs/proc/task_mmu.c` 生成 `/proc/<pid>/smaps` 的 `Rss/Pss/Private_Dirty/Private_Clean/SwapPss` 等字段。

## 主表列含义

| 列 | 含义 | 主要来源 | 解读重点 |
|---|---|---|---|
| `Pss Total` | Proportional Set Size，按共享比例折算后的驻留物理内存。独占页全算，共享页按共享进程数分摊。 | `/proc/<pid>/smaps` 的 `Pss`，显存 mtrack 行来自 memtrack | 最适合估算进程对系统 RAM 的实际压力。 |
| `Private Dirty` | 进程私有且已修改的驻留页。进程退出后通常可直接释放，不能从文件重新读取。 | smaps `Private_Dirty`，mtrack 行由 AOSP 填为对应 PSS | 排查内存增长时最重要，Native Heap 的 Summary 使用这个值。 |
| `Private Clean` | 进程私有但未修改的驻留页，多见于可回收的文件映射页或私有干净映射。 | smaps `Private_Clean` | 退出后释放；压力下也可能被回收或重新读取。 |
| `Swap Dirty` / `SwapPss` | 已换出的脏页。若 kernel 支持 `SwapPss`，Android 会显示按比例分摊后的 swap；否则显示 `Swap`。 | smaps `Swap` / `SwapPss` | 本次为 0，说明目标进程当前没有被统计到的 swap。 |
| `Rss Total` | Resident Set Size，当前实际驻留 RAM 页总量，不按共享比例折算。 | smaps `Rss`，mtrack 行来自 memtrack | 同一共享页会被多个进程重复计算，所以通常大于 PSS。 |
| `Heap Size` | 对 `Native Heap` 是 native allocator 的账面统计；对 `Dalvik Heap` 是 Java Runtime 当前 heap 总量。 | native: `Debug.getNativeHeapSize()`；dalvik: `Runtime.totalMemory()` | 不是物理内存，也不是进程 VSS；native 端不保证与 Alloc/Free 严格相加。 |
| `Heap Alloc` | 对 `Native Heap` 是 allocator 当前已分配字节；对 `Dalvik Heap` 是 `totalMemory - freeMemory`。 | native: `Debug.getNativeHeapAllocatedSize()`；dalvik: Java Runtime | 更接近“分配器账面分配”，不等于 PSS。 |
| `Heap Free` | 对 `Native Heap` 是 allocator 当前空闲字节；对 `Dalvik Heap` 是 Java Runtime freeMemory。 | native: `Debug.getNativeHeapFreeSize()`；dalvik: Java Runtime | `Native Heap` 可出现 Heap Free 很大但 PSS 不降，因为 allocator 未归还页。 |

关系式：

```text
TOTAL PSS = Native Heap PSS + Dalvik Heap PSS + 所有 Other 分类 PSS + Unknown PSS
TOTAL RSS = Native RSS + Dalvik RSS + Other RSS
TOTAL Heap Size/Alloc/Free = Native Heap 对应值 + Dalvik Heap 对应值
```

## 主表行含义

| 行 | 含义 | libmeminfo 分类依据 | 本次读数解读 |
|---|---|---|---|
| `Native Heap` | native allocator 管理的堆内存，包含传统 `[heap]`、bionic malloc/scudo 匿名映射、GWP-ASan 映射。 | VMA 名以 `[heap]`、`[anon:libc_malloc]`、`[anon:scudo:`、`[anon:GWP-ASan` 开头 | PSS 901372 KB，Private Dirty 901300 KB，说明绝大部分 native heap 已真实驻留且为进程私有。 |
| `Dalvik Heap` | ART Java 对象堆。 | `[anon:dalvik-alloc space]`、`[anon:dalvik-main space]`、large object、zygote、non moving 等 | PSS 2063 KB，Java 堆压力很小。 |
| `Dalvik Other` | ART/VM 元数据和 JIT 等非 Java 对象堆。 | `[anon:dalvik-LinearAlloc]`、GC accounting、JIT code cache、CompilerMetadata、IndirectRef 等 | PSS 1982 KB。 |
| `Stack` | Java/native 线程栈。 | `[stack...]`、`[anon:stack_and_tls:]` | PSS 4136 KB。 |
| `Cursor` | CursorWindow ashmem。 | `/dev/ashmem/CursorWindow` | 本次没有非零行。 |
| `Ashmem` | 普通 ashmem 区域。 | `/dev/ashmem`，排除 CursorWindow/JIT zygote cache 等特殊项 | PSS 170 KB。 |
| `Gfx dev` | 已映射到进程地址空间的 GPU 设备内存。 | VMA 名包含 `kgsl-3d0` | 本次没有出现非零行；高通设备上常见。 |
| `Other dev` | 其他 `/dev/*` 映射，无法细分到 Cursor/Ashmem/Gfx dev。 | VMA 名以 `/dev/` 开头但不匹配更具体分类 | PSS 28 KB。 |
| `.so mmap` | native shared library 映射，包括相邻的 `.so` bss 段。 | 文件名以 `.so` 结尾；相邻匿名 bss 可归到 `.so` | PSS 149237 KB，其中 Private Clean 121184 KB，通常是库代码/只读数据/私有干净页。 |
| `.jar mmap` | jar 文件映射。 | 文件名以 `.jar` 结尾 | PSS 3114 KB。 |
| `.apk mmap` | APK 文件映射，含资源和压缩包内映射。 | 文件名以 `.apk` 结尾 | PSS 36778 KB。 |
| `.ttf mmap` | 字体文件映射。 | 文件名以 `.ttf` 结尾 | 本次没有非零行。 |
| `.dex mmap` | dex/vdex/odex 代码映射。 | 文件名包含 `.dex`、`.odex`，或以 `.vdex` 结尾 | PSS 6589 KB。 |
| `.oat mmap` | ART oat 编译产物映射。 | 文件名以 `.oat` 结尾 | PSS 94 KB。 |
| `.art mmap` | ART image 映射，含 boot/app image。 | 文件名以 `.art` 或 `.art]` 结尾 | PSS 6635 KB。 |
| `Other mmap` | 有名字但未匹配到上述类型的 mmap。 | VMA 名非空，且不是已知分类 | PSS 31672 KB，Private Clean 31296 KB，通常需要看 `/proc/<pid>/smaps` 具体文件名。 |
| `EGL mtrack` | memtrack HAL 上报的 graphics 类内存。 | `memtrack_proc_graphics_pss()` | PSS/Private Dirty/RSS 67792 KB。 |
| `GL mtrack` | memtrack HAL 上报的 GL 类内存，常见于纹理、GL 资源、驱动私有归因。 | `memtrack_proc_gl_pss()` | PSS/Private Dirty/RSS 356552 KB，是本次显存大头。 |
| `Other mtrack` | memtrack HAL 上报的其他设备内存。 | `memtrack_proc_other_pss()` | 本次没有非零行。 |
| `Unknown` | 未被明确分类的剩余 smaps 内存。 | AOSP 先把 exclusive other heap 加总到 unknown，再扣掉已打印的 other 分类 | 本次 PSS 446787 KB、Private Dirty 446664 KB，需要重点排查匿名 mmap、驱动或未命名区域。 |
| `TOTAL` | 主表合计。 | `Debug.MemoryInfo.getTotal*()` | 本次 TOTAL PSS 2015001 KB，RSS 2183752 KB。 |

## Native 分配重点：malloc 与 mmap

### 1. `Native Heap` 里的 malloc/scudo 分配

`Native Heap` 的物理口径来自 smaps，不是简单读取 malloc 请求大小。AOSP `libmeminfo` 当前把以下 VMA 归为 `HEAP_NATIVE`：

```text
[heap]
[anon:libc_malloc]
[anon:scudo:...]
[anon:GWP-ASan...]
```

因此，使用 bionic malloc/scudo 分配出来的大块内存，即使底层通过 `mmap()` 获得，只要 VMA 被 allocator 标成上述名字，仍会进入 `Native Heap`，而不是 `.so mmap` 或 `Other mmap`。

本次：

```text
Native Heap PSS          = 901372 KB
Native Heap PrivateDirty = 901300 KB
Native Heap RSS          = 904892 KB
Heap Alloc               = 904628 KB
Heap Free                = 100148 KB
```

解释：

- `PrivateDirty` 接近 `Pss`，说明这些 native heap 页大多是进程独占脏页。
- `Heap Alloc` 接近 `PrivateDirty`，说明 allocator 账面分配和实际驻留脏页基本一致。
- `Heap Size/Alloc/Free` 都是 allocator 账面统计，本次 `Heap Size - Heap Alloc = 109660 KB`，不等于 `Heap Free = 100148 KB`；空闲页是否归还系统取决于 allocator 行为，不能直接当作可用 RAM。

### 2. `Heap Size/Alloc/Free` 与 PSS 的区别

`Heap Size/Alloc/Free` 来自进程内 allocator 统计：

```text
Debug.getNativeHeapSize()          -> mallinfo().usmblks
Debug.getNativeHeapAllocatedSize() -> mallinfo().uordblks
Debug.getNativeHeapFreeSize()      -> mallinfo().fordblks
```

这三个值是 native allocator 账面数据，不是进程虚拟地址空间大小；`Pss/PrivateDirty/Rss` 是 kernel 页表视角的驻留内存。常见差异：

- malloc 已分配但尚未触页：`Heap Alloc` 增长，PSS 不一定同步增长。
- malloc free 后 allocator 缓存页：`Heap Alloc` 下降，PSS/PrivateDirty 不一定立即下降。
- mmap 文件只读代码或资源：进入 `.so mmap/.apk mmap/.dex mmap` 等行，不进入 `Native Heap`。
- 直接匿名 mmap 且没有 allocator VMA 名：可能进入 `Unknown`；有非已知名字的文件/共享映射可能进入 `Other mmap`。

### 3. mmap 分类

`dumpsys meminfo` 的 mmap 行是按 VMA 名称分类，不是按调用 API 分类。也就是说，“业务代码调用了 mmap”并不必然只出现在 `Other mmap`：

```text
文件名 .so  -> .so mmap
文件名 .apk -> .apk mmap
文件名 .dex/.vdex/.odex -> .dex mmap
文件名 .oat -> .oat mmap
文件名 .art -> .art mmap
文件名 .ttf -> .ttf mmap
其他有名字映射 -> Other mmap
空名或匿名且未命中 Native/Dalvik/Stack 等规则 -> Unknown
```

本次 `Other mmap` 的 PSS 为 31672 KB，其中 Private Clean 31296 KB，说明它主要是可回收的私有干净映射；若要定位具体文件，需要读取同一时刻的：

```bash
adb shell cat /proc/20012/smaps
```

并按 VMA 名称、`Pss`、`Private_Dirty`、`Private_Clean` 聚合。

## 显存分配重点

本次 App Summary 的 `Graphics` 为：

```text
Graphics = Gfx dev Private + EGL mtrack Private + GL mtrack Private
         = 0 + 67792 + 356552
         = 424344 KB
```

AOSP `Debug.MemoryInfo.getSummaryGraphics()` 的聚合公式就是：

```text
getOtherPrivate(OTHER_GL_DEV)
+ getOtherPrivate(OTHER_GRAPHICS)
+ getOtherPrivate(OTHER_GL)
```

其中：

- `Gfx dev`：来自 smaps 中已经映射进进程地址空间的 GPU 设备 VMA，例如高通 `kgsl-3d0`。
- `EGL mtrack`：来自 memtrack HAL 的 `MEMTRACK_TYPE_GRAPHICS` 聚合结果，AOSP 通过 `memtrack_proc_graphics_pss()` 读取。
- `GL mtrack`：来自 memtrack HAL 的 `MEMTRACK_TYPE_GL` 聚合结果，AOSP 通过 `memtrack_proc_gl_pss()` 读取。
- `Other mtrack`：来自 memtrack HAL 的 other 类型，主表可能显示为 `Other mtrack`，但不计入 `App Summary` 的 `Graphics`。

memtrack HAL 的设计目的，是统计普通 smaps 无法追踪的设备相关内存，例如纹理内存、未映射进进程地址空间的 GPU buffer、驱动内部归因等。HAL 还要求不同类型之间不应重叠；但 AOSP 源码也提醒 graphics 统计可能被驱动误报，所以显存结论需要结合设备 GPU 驱动、Surface/Texture 生命周期和 dmabuf/ion 信息复核。

排查建议：

```bash
adb shell dumpsys meminfo com.tencent.dhwdxkty.trunk.profiler
adb shell dumpsys gfxinfo com.tencent.dhwdxkty.trunk.profiler
adb shell dumpsys SurfaceFlinger
adb shell ls -l /proc/20012/fd
adb shell cat /proc/20012/smaps
```

如果设备支持 dmabuf sysfs/debugfs，还应按 buffer exporter、inode、fd/map 引用关系核对。`dumpsys meminfo` 的 mtrack 能告诉你“归到这个 pid 的 GPU/graphics 量”，但不能单独还原每个纹理、Surface 或 dmabuf 的 Java/native 创建栈。

## App Summary 指标

本次输出：

```text
Java Heap:       8008 KB PSS,   36872 KB RSS
Native Heap:   901300 KB PSS,  904892 KB RSS
Code:          163540 KB PSS,  328924 KB RSS
Stack:           4136 KB PSS,    4144 KB RSS
Graphics:      424344 KB PSS,  424344 KB RSS
Private Other: 480032 KB PSS
System:         33641 KB PSS
Unknown:                    484576 KB RSS
TOTAL PSS:    2015001 KB
TOTAL RSS:    2183752 KB
TOTAL SWAP:         0 KB
```

| Summary 行 | AOSP 聚合口径 | 本次解读 |
|---|---|---|
| `Java Heap` | `dalvikPrivateDirty + ART image private` | Java 对象堆和应用私有 ART image，8008 KB。 |
| `Native Heap` | `nativePrivateDirty` | native allocator 私有脏页，901300 KB。注意不是 `nativePss`。 |
| `Code` | `.so/.jar/.apk/.ttf/.dex/.oat` 私有页 + JIT code cache 私有页 | 代码和静态资源私有占用，163540 KB。 |
| `Stack` | `Stack` 的 private dirty | 线程栈私有脏页，4136 KB。 |
| `Graphics` | `Gfx dev + EGL mtrack + GL mtrack` 的 private | 显存相关归因，424344 KB。 |
| `Private Other` | `Total Private - Java - Native - Code - Stack - Graphics` | 未纳入上述 Summary 的进程私有内存，480032 KB。 |
| `System` | `Total PSS - Total PrivateClean - Total PrivateDirty` | PSS 中非私有部分，主要是共享库、共享资源按比例分摊，33641 KB。 |
| `Unknown` RSS | `getSummaryUnknownRss()`，RSS 维度的未归类部分 | 484576 KB，需结合主表 `Unknown` 和 smaps 看具体 VMA。 |
| `TOTAL PSS` | `getSummaryTotalPss()` | 本进程总 PSS，2015001 KB。 |
| `TOTAL RSS` | `getTotalRss()` | 本进程总 RSS，2183752 KB。 |
| `TOTAL SWAP` | `getSummaryTotalSwap()` 或 `getSummaryTotalSwapPss()` | 本次 0 KB。 |

## Objects 指标

`Objects` 是目标 App 进程内运行时对象/系统对象计数，不是内存页统计。

| 指标 | 含义 | 来源 |
|---|---|---|
| `Views` | 当前进程存活的 `View` 实例数。 | `VMDebug.countInstancesOfClasses()` |
| `ViewRootImpl` | 窗口根对象数量，通常接近当前窗口数量。 | `VMDebug.countInstancesOfClasses()` |
| `AppContexts` | `ContextImpl` 实例数。 | `VMDebug.countInstancesOfClasses()` |
| `Activities` | `Activity` 实例数。 | `VMDebug.countInstancesOfClasses()` |
| `Assets` | 全局 Asset 数。 | `AssetManager.getGlobalAssetCount()` |
| `AssetManagers` | 全局 AssetManager 数。 | `AssetManager.getGlobalAssetManagerCount()` |
| `Local Binders` | 本进程本地 Binder 对象数量。 | `Debug.getBinderLocalObjectCount()` |
| `Proxy Binders` | 本进程持有的远端 Binder 代理数量。 | `Debug.getBinderProxyObjectCount()` |
| `Parcel memory` | Parcel 全局分配内存，KB。 | `Parcel.getGlobalAllocSize()/1024` |
| `Parcel count` | Parcel 全局分配计数。 | `Parcel.getGlobalAllocCount()` |
| `Death Recipients` | Binder death recipient 数量。 | `Debug.getBinderDeathObjectCount()` |
| `WebViews` | `WebView` 实例数。 | `VMDebug.countInstancesOfClasses()` |

注意：AOSP 在计数前会主动执行一次 GC，因为对象计数需要尽量排除不可达对象。因此 `dumpsys meminfo` 本身可能轻微扰动目标进程。

## SQL 指标

本次：

```text
SQL
         MEMORY_USED:     1131
  PAGECACHE_OVERFLOW:     1034          MALLOC_SIZE:       70
```

| 指标 | 含义 | 来源 |
|---|---|---|
| `MEMORY_USED` | SQLite 通过 `sqlite3_malloc()` 当前使用的内存，KB。 | `sqlite3_status(SQLITE_STATUS_MEMORY_USED)` |
| `PAGECACHE_OVERFLOW` | SQLite page cache 无法由配置的 pagecache buffer 满足、转而走 sqlite malloc 的字节数，KB。 | `sqlite3_status(SQLITE_STATUS_PAGECACHE_OVERFLOW)` |
| `MALLOC_SIZE` | SQLite 观察到的最大单次 malloc 请求，KB。 | `sqlite3_status(SQLITE_STATUS_MALLOC_SIZE)` |

这些值只覆盖 SQLite 自己的统计，不等同于整个 native heap；SQLite malloc 最终仍可能体现在 `Native Heap` 的物理页中。

## DATABASES 指标

本次有一个数据库：

```text
PER CONNECTION STATS
pgsz=4 KB, dbsz=988 KB, Lookaside=123,
cache hits=258, cache misses=42, cache size=25,
Dbname=/data/user/0/com.tencent.dhwdxkty.trunk.profiler/databases/crashSight_db_

POOL STATS
cache hits=258, cache misses=43, cache size=301
```

| 指标 | 含义 |
|---|---|
| `pgsz` | 数据库页大小，KB。AOSP `DbStats` 中保存为 `pageSize / 1024`。 |
| `dbsz` | 数据库文件大小估算，KB，公式是 `pageCount * pageSize / 1024`。 |
| `Lookaside(b)` | SQLite 当前使用的 lookaside slot 数量。AOSP 表头写 `Lookaside(b)`，但字段来源是 `sqlite3_db_status(... SQLITE_DBSTATUS_LOOKASIDE_USED ...)`，不应把样例值直接按总字节解释。 |
| `cache hits` | SQLite page cache 命中次数。 |
| `cache misses` | SQLite page cache 未命中次数。 |
| `cache size` | 当前或累计 page cache size。 |
| `Dbname` | 数据库路径。 |
| `PER CONNECTION STATS` | 单连接维度统计。 |
| `POOL STATS` | 连接池生命周期累计统计。 |

## 为什么主表与 Summary 数值不完全相同

以本次 `Native Heap` 为例：

```text
主表 Native Heap Pss Total = 901372 KB
App Summary Native Heap    = 901300 KB
```

原因是主表展示 `nativePss`，而 `App Summary` 的 `Native Heap` 展示 `nativePrivateDirty`。AOSP 这样设计是为了让 Summary 更接近“应用自己导致、进程退出可释放的私有内存”。同理，`System` 是 PSS 中扣除所有私有 clean/dirty 后剩下的共享/系统分摊部分。

## 与 `/proc/<pid>/smaps` 的关系

Linux kernel 对每个 VMA 输出类似字段：

```text
Size
Rss
Pss
Shared_Clean
Shared_Dirty
Private_Clean
Private_Dirty
Swap
SwapPss
```

`dumpsys meminfo` 不直接展示所有 smaps 字段，而是：

```text
按 VMA 名称分类 -> 聚合每类 Pss/Rss/PrivateDirty/PrivateClean/Swap -> 打印主表
```

kernel 文档说明 `/proc/<pid>/smaps` 是基于 maps 的扩展，会展示每个 mapping 的内存消耗；`smaps_rollup` 是全部 mapping 的累积视图。源码 `fs/proc/task_mmu.c` 中 `__show_smap()` 输出 `Rss/Pss/Private_Dirty/Private_Clean/SwapPss` 等字段，`show_smap()` 逐 VMA 输出，`show_smaps_rollup()` 输出汇总。

## 排查本进程当前内存的建议顺序

```text
1. 先看 App Summary：
   Native Heap 901300 KB、Graphics 424344 KB、Private Other 480032 KB 是主要方向。

2. 看主表行：
   Native Heap 和 Unknown 的 Private Dirty 很高，分别约 901 MB、446 MB。

3. 对 Native Heap：
   用 Perfetto native heap profile 或 malloc 调用栈定位分配点；
   同时对比 Heap Alloc、Native Heap PSS、Private Dirty。

4. 对直接 mmap/Unknown/Other mmap：
   抓同一时刻 /proc/<pid>/smaps，按 VMA 名称和 PSS/PrivateDirty 聚合；
   如果是匿名 mmap，需要结合 mmap 调用栈采集或内核侧 VMA 归因。

5. 对显存：
   结合 dumpsys gfxinfo、SurfaceFlinger、dmabuf/ion、GPU 驱动统计；
   mtrack 只能给进程级归因，不能单独说明是哪一个纹理或 Surface 创建。
```

## 真实 Android demo 验证

仓库内提供了一个真实可安装的 Android demo，用来在真机上验证本文的主要口径：

```bash
00wann/run_meminfo_android_demo.sh
```

demo 源码路径：

```text
00wann/meminfo_android_demo/
  src/com/example/meminfodemo/MainActivity.java
  jni/meminfo_demo_jni.cpp
  build_demo_apk.sh
  verify_meminfo_demo.py
```

demo 会安装包名 `com.example.meminfodemo`，先抓 baseline，再启动自动分配并抓 after。自动分配内容：

```text
Java heap:       Java byte[] 并逐页写入
Native Heap:     JNI malloc + 逐页写入
Unknown:         JNI 匿名 mmap + PR_SET_VMA 命名 + 逐页写入
Other mmap:      JNI 文件 mmap + 逐页写入
SQLite:          写入 demo.db，并保持 SQLiteStatement
Objects:         增加 TextView 数量
Graphics:        创建 ImageReader/Surface 图形缓冲和 GL texture
```

验证脚本会解析两份 `dumpsys meminfo`，并对关键指标做增长检查。一次真机结果示例：

```text
PASS: Native Heap Private Dirty: before=15828KB after=83848KB delta=68020KB
PASS: Other mmap PSS: before=658KB after=33970KB delta=33312KB
PASS: Unknown PSS: before=583KB after=66131KB delta=65548KB
PASS: Graphics Summary PSS: before=45340KB after=113208KB delta=67868KB
PASS: SQLite MEMORY_USED: before=0KB after=2133KB delta=2133KB
PASS: SQLite database size: before=0KB after=18468KB delta=18468KB
```

其中 `Graphics` 仍保留设备依赖说明：demo 会实际创建图形缓冲和 GL texture，但 `dumpsys meminfo` 中的 `EGL mtrack/GL mtrack` 是否增长取决于设备 memtrack HAL 和 GPU 驱动归因。本次最新真机验证中 `EGL mtrack` 与 `GL mtrack` 均有明显归因，因此 `Graphics Summary PSS` 通过；如果其它设备输出 `WARN`，需要结合 SurfaceFlinger、dmabuf/ion 和 GPU 驱动统计继续核对。

## 参考资料

- Android 官方 `dumpsys` 文档：<https://developer.android.com/tools/dumpsys>
- Android 官方内存管理文档：<https://developer.android.com/topic/performance/memory-management>
- AOSP `ActivityManagerService.java`：<https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/services/core/java/com/android/server/am/ActivityManagerService.java>
- AOSP `ActivityThread.java`：<https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/core/java/android/app/ActivityThread.java>
- AOSP `Debug.java`：<https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/core/java/android/os/Debug.java>
- AOSP `android_os_Debug.cpp`：<https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/core/jni/android_os_Debug.cpp>
- AOSP `libmeminfo/androidprocheaps.cpp`：<https://android.googlesource.com/platform/system/memory/libmeminfo/+/refs/heads/main/androidprocheaps.cpp>
- AOSP `androidprocheaps.h`：<https://android.googlesource.com/platform/system/memory/libmeminfo/+/refs/heads/main/include/meminfo/androidprocheaps.h>
- AOSP memtrack HAL 头文件：<https://android.googlesource.com/platform/hardware/libhardware/+/refs/heads/main/include_all/hardware/memtrack.h>
- AOSP `SQLiteDebug.java`：<https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/core/java/android/database/sqlite/SQLiteDebug.java>
- AOSP `android_database_SQLiteDebug.cpp`：<https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/core/jni/android_database_SQLiteDebug.cpp>
- Linux kernel procfs 文档：<https://docs.kernel.org/filesystems/proc.html>
- Android common kernel `fs/proc/task_mmu.c`：<https://android.googlesource.com/kernel/common/+/refs/heads/android-mainline/fs/proc/task_mmu.c>
