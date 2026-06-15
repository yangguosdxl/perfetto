#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
source "$script_dir/../common_tools.sh"
unset MSYS_NO_PATHCONV
sdk_root=$(select_android_sdk_root)
ndk_root=$(select_android_ndk_root)
build_tools=$(select_build_tools_dir "$sdk_root")
android_jar=$(select_android_jar "$sdk_root")
ndk_prebuilt=$(select_ndk_prebuilt_tag "$ndk_root")
clangxx="$ndk_root/toolchains/llvm/prebuilt/$ndk_prebuilt/bin/clang++"
if [[ -e "${clangxx}.exe" ]]; then
  clangxx="${clangxx}.exe"
fi
jdk_bin=$(select_unity_jdk_bin || true)
if [[ -n "$jdk_bin" ]]; then
  export PATH="$jdk_bin:$PATH"
fi
aapt2=$(find_build_tool "$build_tools" aapt2)
zipalign=$(find_build_tool "$build_tools" zipalign)
dx_jar="$build_tools/lib/dx.jar"
apksigner_jar="$build_tools/lib/apksigner.jar"
java_tool=$(command -v java)
keytool_tool=$(command -v keytool)
jar_tool=$(command -v jar)

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
  run_host_tool "$@"
}

for path in "$aapt2" "$zipalign" "$android_jar" "$clangxx" "$dx_jar" \
    "$apksigner_jar" "$java_tool" "$keytool_tool" "$jar_tool"; do
  if [[ ! -e "$path" ]]; then
    echo "缺少构建工具: $path" >&2
    exit 1
  fi
done

rm -rf "$build_dir" "$script_dir/libs" "$script_dir/obj"
mkdir -p "$gen_dir" "$classes_dir" "$dex_dir" "$apkroot_dir/lib/arm64-v8a"

log "编译 native JNI"
mkdir -p "$script_dir/libs/arm64-v8a"
run "$clangxx" --target=aarch64-linux-android23 -O2 -Wall -Wextra -shared -fPIC \
  -o "$script_dir/libs/arm64-v8a/libmeminfodemo.so" \
  "$script_dir/jni/meminfo_demo_jni.cpp" \
  -static-libstdc++ \
  -llog

log "编译 Android resources"
run "$aapt2" compile --dir "$script_dir/res" -o "$build_dir/res.zip"
run "$aapt2" link \
  -I "$android_jar" \
  --manifest "$script_dir/AndroidManifest.xml" \
  --java "$gen_dir" \
  --auto-add-overlay \
  -o "$linked_apk" \
  "$build_dir/res.zip"

log "编译 Java"
mapfile -t java_sources < <(find "$script_dir/src" "$gen_dir" -name '*.java' | sort)
run javac -encoding UTF-8 -source 1.7 -target 1.7 \
  -bootclasspath "$android_jar" \
  -d "$classes_dir" \
  "${java_sources[@]}"

log "生成 classes.dex"
run "$java_tool" -cp "$dx_jar" com.android.dx.command.Main \
  --dex --output="$dex_dir/classes.dex" "$classes_dir"

log "组装 APK"
cp "$linked_apk" "$unsigned_apk"
cp "$dex_dir/classes.dex" "$apkroot_dir/classes.dex"
cp "$script_dir/libs/arm64-v8a/libmeminfodemo.so" "$apkroot_dir/lib/arm64-v8a/"
(
  cd "$apkroot_dir"
  run "$jar_tool" uf "$unsigned_apk" classes.dex lib
)

log "zipalign 和签名"
run "$zipalign" -f 4 "$unsigned_apk" "$aligned_apk"
if [[ ! -f "$keystore" ]]; then
  run "$keytool_tool" -genkeypair -keystore "$keystore" -storepass android -keypass android \
    -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Perfetto Meminfo Demo,C=CN"
fi
run env "JAVA_TOOL_OPTIONS=--add-opens=java.base/java.io=ALL-UNNAMED --add-exports=java.base/sun.security.x509=ALL-UNNAMED --add-exports=java.base/sun.security.pkcs=ALL-UNNAMED" \
  "$java_tool" -jar "$apksigner_jar" sign \
  --ks "$keystore" \
  --ks-pass pass:android \
  --key-pass pass:android \
  --out "$signed_apk" \
  "$aligned_apk"

log "APK 已生成: $signed_apk"
log "包名: $package_name"
