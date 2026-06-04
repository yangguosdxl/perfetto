LOCAL_PATH := $(call my-dir)

include $(CLEAR_VARS)
LOCAL_MODULE := meminfodemo
LOCAL_SRC_FILES := meminfo_demo_jni.cpp
LOCAL_LDLIBS := -llog
include $(BUILD_SHARED_LIBRARY)
