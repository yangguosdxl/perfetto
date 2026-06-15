# meminfo Android demo 真机验证说明

本文独立说明 `00wann/meminfo_android_demo/` 的实现细节、验证流程和真机验证结论。它用于验证 `adb shell dumpsys meminfo <package>` 中 Native Heap、mmap、SQLite、Objects、Graphics 等指标的实际表现。

## 目标

验证以下口径是否能在真实 Android App 和真机 `dumpsys meminfo` 中被观察到：

```text
Java heap       -> Dalvik Heap / App Summary Java Heap
native malloc   -> Native Heap / App Summary Native Heap
匿名 mmap       -> Unknown
文件 mmap       -> Other mmap
SQLite          -> SQL / DATABASES
View 对象       -> Objects Views
Graphics        -> EGL mtrack / GL mtrack / App Summary Graphics
```

## 文件结构

```text
00wann/meminfo_android_demo/
  AndroidManifest.xml
  res/values/strings.xml
  res/values/styles.xml
  src/com/example/meminfodemo/MainActivity.java
  jni/Android.mk
  jni/Application.mk
  jni/meminfo_demo_jni.cpp
  build_demo_apk.sh
  verify_meminfo_demo.py

00wann/run_meminfo_android_demo.sh
00wann/test_meminfo_android_demo.py
```

## 构建方式

没有新建 Gradle 工程，原因是当前环境可用的是 Android SDK `android-23`、build-tools `26.0.1` 和 NDK，直接用命令行工具构建更稳定、依赖更少。

构建脚本：

```bash
00wann/meminfo_android_demo/build_demo_apk.sh
```

关键步骤：

```text
1. ndk-build 编译 jni/meminfo_demo_jni.cpp
2. aapt2 compile/link 编译资源并生成 R.java
3. javac 使用 android-23/android.jar 编译 Java
4. dx 生成 classes.dex
5. zip 写入 classes.dex 和 lib/arm64-v8a/libmeminfodemo.so
6. zipalign 对齐 APK
7. apksigner 使用 debug.keystore 签名
```

由于 build-tools 26.0.1 的 `apksigner` 在 Java 17 下访问 JDK 内部包会被模块系统拦截，脚本显式设置：

```bash
JAVA_TOOL_OPTIONS="--add-opens=java.base/java.io=ALL-UNNAMED \
--add-exports=java.base/sun.security.x509=ALL-UNNAMED \
--add-exports=java.base/sun.security.pkcs=ALL-UNNAMED"
```

签名用的 `debug.keystore` 固定放在 demo 目录，避免每次重建 keystore 导致真机安装时报：

```text
INSTALL_FAILED_UPDATE_INCOMPATIBLE
```

运行脚本仍保留兜底逻辑：如果签名不兼容，会先 `adb uninstall com.example.meminfodemo` 再安装。

## App 实现细节

包名：

```text
com.example.meminfodemo
```

入口：

```text
com.example.meminfodemo/.MainActivity
```

脚本启动自动分配时传入：

```bash
adb shell am start -n com.example.meminfodemo/.MainActivity --ez auto_allocate true
```

`MainActivity.onCreate()` 检查 `auto_allocate`，延迟 800 ms 后调用 `allocateAll()`。

### Java heap

实现位置：

```text
MainActivity.allocateJavaHeap()
```

分配量：

```text
JAVA_HEAP_MB = 48
```

实现方式：

```java
byte[] block = new byte[1024 * 1024];
for (int j = 0; j < block.length; j += 4096) {
  block[j] = (byte) i;
}
javaBlocks.add(block);
```

关键点：

- 每个 1 MB byte array 都逐 4 KB 写入一次。
- 这样避免只保留虚拟地址空间，确保物理页实际建立。
- `javaBlocks` 持有引用，避免 GC 回收。

预期影响：

```text
Dalvik Heap PSS / Private Dirty 增加
App Summary Java Heap 增加
```

### native malloc

实现位置：

```text
MainActivity.nativeAllocateMalloc()
jni/meminfo_demo_jni.cpp
```

分配量：

```text
NATIVE_MALLOC_MB = 64
```

实现方式：

```cpp
void* ptr = malloc(size);
touch_pages(ptr, size, 0x5a);
g_malloc_blocks.push_back(ptr);
```

关键点：

- JNI 中直接调用 `malloc()`。
- `touch_pages()` 逐 4 KB 写入，强制建立物理页。
- `g_malloc_blocks` 持有指针，避免释放。

预期影响：

```text
Native Heap PSS 增加
Native Heap Private Dirty 增加
App Summary Native Heap 增加
Heap Alloc 增加
```

### 匿名 mmap

实现位置：

```text
MainActivity.nativeAllocateAnonMmap()
jni/meminfo_demo_jni.cpp
```

分配量：

```text
ANON_MMAP_MB = 64
```

实现方式：

```cpp
void* ptr = mmap(nullptr, size, PROT_READ | PROT_WRITE,
                 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, ptr, size, "demo_anon_mmap");
touch_pages(ptr, size, 0x33);
g_mmap_blocks.push_back({ptr, size, -1});
```

关键点：

- 匿名 mmap 不走 malloc。
- `PR_SET_VMA_ANON_NAME` 给 VMA 命名为 `demo_anon_mmap`。
- libmeminfo 对 `[anon:...]` 中非 dalvik、非 malloc/scudo 的区域会归入 `HEAP_UNKNOWN`，最终表格显示到 `Unknown`。

预期影响：

```text
Unknown PSS 增加
Unknown Private Dirty 增加
```

### 文件 mmap

实现位置：

```text
MainActivity.nativeAllocateFileMmap()
jni/meminfo_demo_jni.cpp
```

分配量：

```text
FILE_MMAP_MB = 32
```

实现方式：

```cpp
int fd = open(file_path.c_str(), O_CREAT | O_RDWR, 0600);
ftruncate(fd, size);
void* ptr = mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
touch_pages(ptr, size, 0x7f);
g_mmap_blocks.push_back({ptr, size, fd});
```

文件路径：

```text
/data/user/0/com.example.meminfodemo/files/demo_other_mmap.bin
```

关键点：

- VMA 名称是普通文件路径，不以 `.so/.apk/.dex/.oat/.art/.ttf` 结尾。
- libmeminfo 对这类有名字但不属于已知后缀的 VMA 归入 `Other mmap`。

预期影响：

```text
Other mmap PSS 增加
Other mmap Private Dirty / Private Clean 变化
```

### SQLite

实现位置：

```text
MainActivity.allocateSqlite()
```

参数：

```text
SQLITE_BLOB_ROWS = 2048
SQLITE_STATEMENTS = 256
payload = 4096 bytes
```

实现方式：

```java
database = SQLiteDatabase.openOrCreateDatabase(
    new File(getFilesDir(), "demo.db").getAbsolutePath(), null);
database.execSQL("CREATE TABLE IF NOT EXISTS blobs (id INTEGER PRIMARY KEY, payload BLOB)");
database.insert("blobs", null, values);
sqliteStatements.add(database.compileStatement(
    "SELECT COUNT(*) FROM blobs WHERE id >= " + i));
```

关键点：

- 写入大量 4 KB blob，让数据库文件变大。
- 保持 `SQLiteDatabase` 和 `SQLiteStatement`，让 `SQL` 与 `DATABASES` 统计稳定存在。

预期影响：

```text
SQL MEMORY_USED 增加
PAGECACHE_OVERFLOW 增加
DATABASES 中 dbsz 增加
Lookaside(b) 出现非零 slot 数
```

### Objects / Views

实现位置：

```text
MainActivity.allocateViews()
```

实现方式：

```java
for (int i = 0; i < 80; i++) {
  TextView view = new TextView(MainActivity.this);
  root.addView(view, 2);
}
```

预期影响：

```text
Objects Views 增加
```

### Graphics

实现位置：

```text
MainActivity.allocateGraphicBuffers()
MainActivity.DemoRenderer.allocateTextures()
```

实现方式：

```java
ImageReader.newInstance(2048, 2048, PixelFormat.RGBA_8888, 3);
surface.lockCanvas(null);
surface.unlockCanvasAndPost(canvas);
```

以及：

```java
GLES20.glGenTextures(TEXTURE_COUNT, textures, 0);
ByteBuffer pixels = ByteBuffer.allocateDirect(TEXTURE_SIZE * TEXTURE_SIZE * 4);
GLES20.glTexImage2D(..., pixels);
```

关键点：

- demo 确实创建了 `ImageReader/Surface` 图形缓冲。
- demo 也创建了 GL texture，并上传真实像素数据，避免空 texture 被驱动懒分配。
- 但 `dumpsys meminfo` 的 `EGL mtrack/GL mtrack` 来自设备 memtrack HAL，是否把这些资源归到 App pid 取决于 GPU 驱动和 HAL 实现。

预期影响：

```text
理想情况下 Graphics / GL mtrack / EGL mtrack 增加
实际设备上可能无增长或反向变化，因此验证脚本会把 Graphics 结果显式输出；最新真机验证中该项已 PASS。
```

## 真机验证脚本

入口：

```bash
00wann/run_meminfo_android_demo.sh
```

Windows Git Bash 下构建脚本会优先探测 Unity 2022.3.62 自带的
`AndroidPlayer/SDK`、`AndroidPlayer/NDK` 和 `AndroidPlayer/OpenJDK`。
如需覆盖，可设置 `ANDROID_SDK_ROOT`、`ANDROID_NDK_ROOT`、`ANDROID_BUILD_TOOLS`
或 `ANDROID_JAR`。

为兼容当前 Android 设备的安装限制，demo 的 `targetSdkVersion` 为 24。
Windows Git Bash 构建时不调用 `ndk-build.cmd`、`dx.bat` 或 `apksigner.bat`；
脚本直接使用 NDK `clang++` 编译 JNI，并通过 `dx.jar`、`apksigner.jar`
完成 dex 和签名，避免 `.cmd/.bat` 路径空格和参数转换问题。

流程：

```text
1. 构建 APK。
2. adb install -r 安装；签名不兼容时卸载旧包再装。
3. force-stop demo。
4. 启动普通 Activity，等待 pid，抓 baseline dumpsys meminfo。
5. force-stop demo。
6. 清 logcat。
7. 带 auto_allocate=true 启动 Activity。
8. 等待 logcat 出现 MEMINFO_DEMO_READY。
9. 再等待 5 秒，让 GL/SQLite/页面状态稳定。
10. 抓 after dumpsys meminfo。
11. 调用 verify_meminfo_demo.py 对比 baseline/after。
12. 输出 verify.txt，并保存两份 meminfo 原文。
```

输出目录格式：

```text
00wann/PerfData/meminfo_demo_YYYYMMDD_HHMMSS/
  baseline_meminfo.txt
  after_meminfo.txt
  verify.txt
```

最近一次真机验证目录：

```text
00wann/PerfData/meminfo_demo_20260604_165648/
```

## 校验脚本实现

实现位置：

```text
00wann/meminfo_android_demo/verify_meminfo_demo.py
```

核心能力：

```text
parse_meminfo(text)
  -> pid / process_name
  -> 主表 table[label].pss/private_dirty/private_clean/rss/heap_alloc...
  -> App Summary PSS/RSS
  -> SQL MEMORY_USED/PAGECACHE_OVERFLOW/MALLOC_SIZE
  -> DATABASES pgsz/dbsz/lookaside/cache hits/cache misses/cache size

build_growth_checks(before, after)
  -> Native Heap Private Dirty
  -> Other mmap PSS
  -> Unknown PSS
  -> Graphics Summary PSS
  -> SQLite MEMORY_USED
  -> SQLite database size
```

阻断项：

```text
Native Heap Private Dirty >= 32 MB
Other mmap PSS            >= 8 MB
Unknown PSS               >= 16 MB
SQLite MEMORY_USED        >= 64 KB
SQLite database size      >= 1 MB
```

设备依赖项：

```text
Graphics Summary PSS      >= 16 MB 时 PASS；若设备 memtrack 没有归因则 WARN
```

Graphics 保留设备依赖说明：AOSP 源码说明 graphics 统计可能被驱动误报；实际 memtrack HAL 只上报设备愿意归因给该 pid 的 smaps-unaccounted 图形内存。本次最新真机验证中该项 PASS。

## 真机验证结果

执行命令：

```bash
00wann/run_meminfo_android_demo.sh
```

结果目录：

```text
00wann/PerfData/meminfo_demo_20260604_165648/
```

校验结果：

```text
PASS: Native Heap Private Dirty: before=17680KB after=86340KB delta=68660KB min=32768KB
PASS: Other mmap PSS: before=973KB after=34342KB delta=33369KB min=8192KB
PASS: Unknown PSS: before=595KB after=66135KB delta=65540KB min=16384KB
PASS: Graphics Summary PSS: before=45340KB after=113208KB delta=67868KB min=16384KB
PASS: SQLite MEMORY_USED: before=0KB after=2133KB delta=2133KB min=64KB
PASS: SQLite database size: before=0KB after=18468KB delta=18468KB min=1024KB
```

after 快照关键行：

```text
  Native Heap    86405    86340        0        0    90036    97188    91350     1569
  Dalvik Heap    49972    49800        0        0    57844   150086    51782    98304
   Other mmap    34342    32776      924        0    36964
   EGL mtrack    39816    39816        0        0    39816
    GL mtrack    73392    73392        0        0    73392
      Unknown    66135    66116        8        0    67000
        TOTAL   364626   355960     1244        0   499676   247274   143132    99873
```

after Summary：

```text
           Java Heap:    55516                          84912
         Native Heap:    86340                          90036
                Code:      544                         104232
               Stack:      968                            976
            Graphics:   113208                         113208
       Private Other:   100628
              System:     7422
             Unknown:                                  106312

           TOTAL PSS:   364626            TOTAL RSS:   499676      TOTAL SWAP (KB):        0
```

after SQL/DATABASES：

```text
SQL
         MEMORY_USED:     2133
  PAGECACHE_OVERFLOW:     2060          MALLOC_SIZE:       46

DATABASES
      pgsz     dbsz   Lookaside(b) cache hits cache misses cache size  Dbname
PER CONNECTION STATS
         4    18468            110  4095   274    25  /data/user/0/com.example.meminfodemo/files/demo.db
POOL STATS
     cache hits  cache misses    cache size  Dbname
           4095           275          4370  /data/user/0/com.example.meminfodemo/files/demo.db
```

## 结论

```text
Native Heap:
  通过 JNI malloc + 逐页写入，Native Heap Private Dirty 增加 68660 KB。
  证明 malloc 物理驻留页会进入 Native Heap，并反映到 App Summary Native Heap。

Other mmap:
  通过普通文件 mmap + 逐页写入，Other mmap PSS 增加 33369 KB。
  证明有文件名但不匹配 .so/.apk/.dex/.oat/.art/.ttf 的 VMA 会进入 Other mmap。

Unknown:
  通过命名匿名 mmap + 逐页写入，Unknown PSS 增加 65540 KB。
  证明非 malloc/scudo、非 dalvik、非 stack 的匿名 VMA 会进入 Unknown。

SQLite:
  MEMORY_USED 增加 2133 KB，dbsz 增加 18468 KB，Lookaside(b)=110。
  证明 SQL 和 DATABASES 指标来自进程内 SQLite 统计，并能被 demo 稳定观测。

Objects:
  after 中 Views=91，baseline 中 Views 约 11。
  证明 View 对象计数能反映 App 内存状态变化，但它是对象计数，不是页内存。

Graphics:
  demo 创建了 ImageReader/Surface 图形缓冲和 GL texture，本次真机 EGL mtrack/GL mtrack 合计增长 67868 KB。
  结论是：Graphics 可以通过真实图形资源分配验证，但仍必须结合设备 memtrack HAL、GPU 驱动和 SurfaceFlinger/dmabuf 进一步核对。
```

## 已知限制

- 当前 demo 使用本机现有 Android SDK `android-23` 和 build-tools `26.0.1`，因此 Java 代码保持 Java 7 兼容写法，不使用 AndroidX/Gradle。
- 当前 APK 只构建 `arm64-v8a`，适用于当前连接真机；如需兼容 32 位设备，需要修改 `jni/Application.mk` 的 `APP_ABI`。
- Graphics 验证依赖设备 memtrack HAL；当前最新真机结果可以归因并 PASS，但其它设备仍可能输出 WARN。
- `Other mmap` 和 `Unknown` 的具体归类依赖 AOSP `libmeminfo` 对 VMA 名称的规则；不同 Android 版本若规则变化，输出可能不同。

## 复验命令

```bash
python 00wann/test_meminfo_android_demo.py
bash -n 00wann/meminfo_android_demo/build_demo_apk.sh 00wann/run_meminfo_android_demo.sh
00wann/run_meminfo_android_demo.sh
```
