#include <android/log.h>
#include <errno.h>
#include <jni.h>
#include <malloc.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define LOG_TAG "HeapDemo"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

struct DemoArgs {
  char result_path[512];
  long total_bytes;
  int start_delay_seconds;
  int alloc_seconds;
  int hold_seconds;
};

static int64_t now_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static void sleep_until_ns(int64_t target_ns) {
  for (;;) {
    int64_t remain = target_ns - now_ns();
    if (remain <= 0) {
      return;
    }
    struct timespec ts = {
        .tv_sec = remain / 1000000000LL,
        .tv_nsec = remain % 1000000000LL,
    };
    nanosleep(&ts, NULL);
  }
}

static void write_result(const char* path,
                         const char* state,
                         size_t allocation_count,
                         size_t expected_live_bytes,
                         size_t mallinfo_uordblks) {
  FILE* fp = fopen(path, "w");
  if (!fp) {
    LOGE("打开结果文件失败: %s errno=%d", path, errno);
    return;
  }
  fprintf(fp,
          "state=%s\n"
          "pid=%d\n"
          "allocation_count=%zu\n"
          "expected_live_bytes=%zu\n"
          "mallinfo_uordblks=%zu\n",
          state, getpid(), allocation_count, expected_live_bytes,
          mallinfo_uordblks);
  fclose(fp);
}

static void* run_demo(void* opaque) {
  struct DemoArgs* args = (struct DemoArgs*)opaque;
  const size_t min_size = 1;
  const size_t max_size = 1024 * 1024;
  const size_t step = 4093;
  size_t next_size = min_size;
  size_t expected_live_bytes = 0;
  size_t capacity = 1024;
  size_t count = 0;
  void** blocks = calloc(capacity, sizeof(void*));
  if (!blocks) {
    LOGE("calloc blocks failed");
    free(args);
    return NULL;
  }

  sleep((unsigned int)args->start_delay_seconds);
  write_result(args->result_path, "allocating", 0, 0, 0);
  volatile uint64_t checksum = 0;
  int64_t start_ns = now_ns();
  int64_t alloc_duration_ns = (int64_t)args->alloc_seconds * 1000000000LL;
  while (expected_live_bytes < (size_t)args->total_bytes) {
    size_t size = next_size;
    if (size > (size_t)args->total_bytes - expected_live_bytes) {
      size = (size_t)args->total_bytes - expected_live_bytes;
    }
    if (count == capacity) {
      capacity *= 2;
      void** new_blocks = realloc(blocks, capacity * sizeof(void*));
      if (!new_blocks) {
        LOGE("realloc blocks failed");
        break;
      }
      blocks = new_blocks;
    }
    void* ptr = malloc(size);
    if (!ptr) {
      LOGE("malloc failed count=%zu size=%zu", count, size);
      break;
    }
    /* 每次分配都写入，确保 Native Heap PSS 能反映真实驻留。 */
    memset(ptr, (int)(count & 0xff), size);
    checksum += ((unsigned char*)ptr)[0];
    blocks[count++] = ptr;
    expected_live_bytes += size;
    next_size += step;
    if (next_size > max_size) {
      next_size = min_size + (next_size % max_size);
    }

    int64_t target_ns = start_ns + (int64_t)((long double)expected_live_bytes /
                                             (long double)args->total_bytes *
                                             (long double)alloc_duration_ns);
    sleep_until_ns(target_ns);
  }

  struct mallinfo info = mallinfo();
  LOGI(
      "ALLOCATED pid=%d allocations=%zu expected=%zu mallinfo=%zu "
      "checksum=%llu",
      getpid(), count, expected_live_bytes, (size_t)info.uordblks,
      (unsigned long long)checksum);
  write_result(args->result_path, "allocated", count, expected_live_bytes,
               (size_t)info.uordblks);

  sleep((unsigned int)args->hold_seconds);

  for (size_t i = 0; i < count; ++i) {
    free(blocks[i]);
  }
  free(blocks);
  write_result(args->result_path, "done", count, expected_live_bytes, 0);
  free(args);
  return NULL;
}

JNIEXPORT void JNICALL Java_com_example_heapprofddemo_MainActivity_nativeStart(
    JNIEnv* env,
    jclass clazz,
    jstring files_dir,
    jlong total_bytes,
    jint start_delay_seconds,
    jint alloc_seconds,
    jint hold_seconds) {
  (void)clazz;
  const char* dir = (*env)->GetStringUTFChars(env, files_dir, NULL);
  if (!dir) {
    return;
  }
  struct DemoArgs* args = calloc(1, sizeof(struct DemoArgs));
  if (!args) {
    (*env)->ReleaseStringUTFChars(env, files_dir, dir);
    return;
  }
  snprintf(args->result_path, sizeof(args->result_path),
           "%s/malloc_demo_result.txt", dir);
  args->total_bytes = (long)total_bytes;
  args->start_delay_seconds = (int)start_delay_seconds;
  args->alloc_seconds = (int)alloc_seconds;
  args->hold_seconds = (int)hold_seconds;
  (*env)->ReleaseStringUTFChars(env, files_dir, dir);

  pthread_t thread;
  if (pthread_create(&thread, NULL, run_demo, args) != 0) {
    LOGE("pthread_create failed");
    free(args);
    return;
  }
  pthread_detach(thread);
}
