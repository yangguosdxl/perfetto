#include <jni.h>

#include <android/log.h>
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <unistd.h>

#include <string>
#include <vector>

#ifndef PR_SET_VMA
#define PR_SET_VMA 0x53564d41
#endif
#ifndef PR_SET_VMA_ANON_NAME
#define PR_SET_VMA_ANON_NAME 0
#endif

#define LOG_TAG "MeminfoDemoJni"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

namespace {

struct MmapBlock {
  void* ptr;
  size_t size;
  int fd;
};

std::vector<void*> g_malloc_blocks;
std::vector<MmapBlock> g_mmap_blocks;

size_t mb_to_bytes(jint mb) {
  return static_cast<size_t>(mb) * 1024u * 1024u;
}

void touch_pages(void* ptr, size_t size, unsigned char value) {
  // 逐页写入强制建立物理页，确保 PSS/Private Dirty 能被 dumpsys 观察到。
  volatile unsigned char* bytes = static_cast<volatile unsigned char*>(ptr);
  for (size_t offset = 0; offset < size; offset += 4096) {
    bytes[offset] = value;
  }
  if (size > 0) {
    bytes[size - 1] = value;
  }
}

std::string to_string(JNIEnv* env, jstring value) {
  const char* chars = env->GetStringUTFChars(value, nullptr);
  std::string result(chars == nullptr ? "" : chars);
  if (chars != nullptr) {
    env->ReleaseStringUTFChars(value, chars);
  }
  return result;
}

}  // namespace

extern "C" JNIEXPORT jlong JNICALL
Java_com_example_meminfodemo_MainActivity_nativeAllocateMalloc(
    JNIEnv*, jclass, jint mb) {
  const size_t size = mb_to_bytes(mb);
  void* ptr = malloc(size);
  if (ptr == nullptr) {
    LOGE("malloc 失败: mb=%d errno=%d", mb, errno);
    return 0;
  }
  touch_pages(ptr, size, 0x5a);
  g_malloc_blocks.push_back(ptr);
  LOGI("malloc 已分配并触页: %zu bytes", size);
  return static_cast<jlong>(size);
}

extern "C" JNIEXPORT jlong JNICALL
Java_com_example_meminfodemo_MainActivity_nativeAllocateAnonMmap(
    JNIEnv*, jclass, jint mb) {
  const size_t size = mb_to_bytes(mb);
  void* ptr = mmap(nullptr, size, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (ptr == MAP_FAILED) {
    LOGE("匿名 mmap 失败: mb=%d errno=%d", mb, errno);
    return 0;
  }
  prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, ptr, size, "demo_anon_mmap");
  // 命名后的匿名 mmap 会进入 libmeminfo 的 Unknown 路径，便于验证 Unknown 行。
  touch_pages(ptr, size, 0x33);
  g_mmap_blocks.push_back({ptr, size, -1});
  LOGI("匿名 mmap 已分配并触页: %zu bytes", size);
  return static_cast<jlong>(size);
}

extern "C" JNIEXPORT jlong JNICALL
Java_com_example_meminfodemo_MainActivity_nativeAllocateFileMmap(
    JNIEnv* env, jclass, jstring path, jint mb) {
  const size_t size = mb_to_bytes(mb);
  const std::string file_path = to_string(env, path);
  int fd = open(file_path.c_str(), O_CREAT | O_RDWR, 0600);
  if (fd < 0) {
    LOGE("打开 mmap 文件失败: %s errno=%d", file_path.c_str(), errno);
    return 0;
  }
  if (ftruncate(fd, static_cast<off_t>(size)) != 0) {
    LOGE("设置 mmap 文件大小失败: %s errno=%d", file_path.c_str(), errno);
    close(fd);
    return 0;
  }
  void* ptr = mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  if (ptr == MAP_FAILED) {
    LOGE("文件 mmap 失败: %s errno=%d", file_path.c_str(), errno);
    close(fd);
    return 0;
  }
  touch_pages(ptr, size, 0x7f);
  // 文件路径出现在 smaps VMA 名称中，libmeminfo 会把它归入 Other mmap。
  g_mmap_blocks.push_back({ptr, size, fd});
  LOGI("文件 mmap 已分配并触页: %s %zu bytes", file_path.c_str(), size);
  return static_cast<jlong>(size);
}

extern "C" JNIEXPORT void JNICALL
Java_com_example_meminfodemo_MainActivity_nativeReleaseAll(JNIEnv*, jclass) {
  for (void* ptr : g_malloc_blocks) {
    free(ptr);
  }
  g_malloc_blocks.clear();
  for (const MmapBlock& block : g_mmap_blocks) {
    munmap(block.ptr, block.size);
    if (block.fd >= 0) {
      close(block.fd);
    }
  }
  g_mmap_blocks.clear();
  LOGI("native demo 内存已释放");
}
