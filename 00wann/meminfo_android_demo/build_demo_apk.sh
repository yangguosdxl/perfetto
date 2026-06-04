#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
sdk_root=${ANDROID_SDK_ROOT:-${ANDROID_HOME:-/home/dianhun/disk2/Android/tools}}
ndk_root=${ANDROID_NDK_ROOT:-/home/dianhun/disk2/Android/android-ndk-r27d}
build_tools=${ANDROID_BUILD_TOOLS:-$sdk_root/build-tools/26.0.1}
android_jar=${ANDROID_JAR:-$sdk_root/platforms/android-23/android.jar}

app_name=meminfo-demo
package_name=com.example.meminfodemo
build_dir="$script_dir/build"
gen_dir="$build_dir/gen"
classes_dir="$build_dir/classes"
dex_dir="$build_dir/dex"
apkroot_dir="$build_dir/apkroot"
linked_apk="$build_dir/${app_name}-linked.apk"
unsigned_apk="$build_dir/${app_name}-unsigned.apk"
aligned_apk="$build_dir/${app_name}-aligned.apk"
signed_apk="$build_dir/${app_name}.apk"
keystore="$script_dir/debug.keystore"

log() {
  printf '[build-demo-apk] %s\n' "$*"
}

run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

for path in "$build_tools/aapt2" "$build_tools/dx" "$build_tools/zipalign" \
    "$build_tools/apksigner" "$android_jar" "$ndk_root/ndk-build"; do
  if [[ ! -e "$path" ]]; then
    echo "缺少构建工具: $path" >&2
    exit 1
  fi
done

rm -rf "$build_dir" "$script_dir/libs" "$script_dir/obj"
mkdir -p "$gen_dir" "$classes_dir" "$dex_dir" "$apkroot_dir/lib/arm64-v8a"

log "编译 native JNI"
run "$ndk_root/ndk-build" \
  "NDK_PROJECT_PATH=$script_dir" \
  "APP_BUILD_SCRIPT=$script_dir/jni/Android.mk" \
  "NDK_APPLICATION_MK=$script_dir/jni/Application.mk" \
  -j"$(nproc)"

log "编译 Android resources"
run "$build_tools/aapt2" compile --dir "$script_dir/res" -o "$build_dir/res.zip"
run "$build_tools/aapt2" link \
  -I "$android_jar" \
  --manifest "$script_dir/AndroidManifest.xml" \
  --java "$gen_dir" \
  --auto-add-overlay \
  -o "$linked_apk" \
  "$build_dir/res.zip"

log "编译 Java"
mapfile -t java_sources < <(find "$script_dir/src" "$gen_dir" -name '*.java' | sort)
run javac -source 1.7 -target 1.7 \
  -bootclasspath "$android_jar" \
  -d "$classes_dir" \
  "${java_sources[@]}"

log "生成 classes.dex"
run "$build_tools/dx" --dex --output="$dex_dir/classes.dex" "$classes_dir"

log "组装 APK"
cp "$linked_apk" "$unsigned_apk"
cp "$dex_dir/classes.dex" "$apkroot_dir/classes.dex"
cp "$script_dir/libs/arm64-v8a/libmeminfodemo.so" "$apkroot_dir/lib/arm64-v8a/"
(
  cd "$apkroot_dir"
  run zip -qr "$unsigned_apk" classes.dex lib
)

log "zipalign 和签名"
run "$build_tools/zipalign" -f 4 "$unsigned_apk" "$aligned_apk"
if [[ ! -f "$keystore" ]]; then
  run keytool -genkeypair -keystore "$keystore" -storepass android -keypass android \
    -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Perfetto Meminfo Demo,C=CN"
fi
run env "JAVA_TOOL_OPTIONS=--add-opens=java.base/java.io=ALL-UNNAMED --add-exports=java.base/sun.security.x509=ALL-UNNAMED --add-exports=java.base/sun.security.pkcs=ALL-UNNAMED" \
  "$build_tools/apksigner" sign \
  --ks "$keystore" \
  --ks-pass pass:android \
  --key-pass pass:android \
  --out "$signed_apk" \
  "$aligned_apk"

log "APK 已生成: $signed_apk"
log "包名: $package_name"
