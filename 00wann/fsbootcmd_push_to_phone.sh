source config.sh

export MSYS_NO_PATHCONV=1
phonePath=/data/local/tmp
cfgName=FSBootCmdLine.cfg
adb shell rm -f $phonePath/$cfgName
adb push $cfgName $phonePath
adb shell touch $phonePath/$cfgName
adb shell ls -l $phonePath/$cfgName
