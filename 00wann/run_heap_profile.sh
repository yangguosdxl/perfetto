# 在BAT里运行时，会出现符号无法正确解析的问题
source config.sh

export MSYS_NO_PATHCONV=1
export PERFETTO_SYMBOLIZER_MODE=index 
export PERFETTO_BINARY_PATH='./workspace/allsymbols/arm64-v8a'
export PATH="$PerfettoRoot/buildtools/linux64/clang/bin:$PATH"
dir="PerfData/mem/`date +%F_%k-%M-%S`"
if [ ! -d "$dir" ]
then
    mkdir -p "$dir"
fi

source fsbootcmd_push_to_phone.sh

app=com.tencent.dhwdxkty.trunk.profiler
adb push debugconfig.txt /sdcard/Android/data/$app/files

while [ "$pid" == "" ]
do
    pid=$(adb shell pidof $app)
    printf "\rwait %s start ..." "$app"
done
printf "\r%s started (pid: %s)\n" "$app" "$pid"

cp $PerfettoRoot/out/linux_clang_release/traceconv ~/.local/share/perfetto/prebuilts/traceconv
python3 $PerfettoRoot/python/tools/heap_profile.py -n $app -o "$dir"
