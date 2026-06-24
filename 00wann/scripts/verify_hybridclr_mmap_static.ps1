param(
    [string]$FsClientRoot = "D:/dr2/Trunk_LocalBuild/ClientPublish/DreamRivakes2_U3DProj",
    [string]$EcmaRoot = "D:/dr2/Misc/Ecma335UnitTest"
)

$ErrorActionPreference = "Stop"

$failures = New-Object System.Collections.Generic.List[string]

function Read-Text([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        $failures.Add("missing file: $Path")
        return ""
    }
    return Get-Content -Raw -Encoding UTF8 -LiteralPath $Path
}

function Assert-Contains([string]$Name, [string]$Text, [string]$Pattern) {
    if ($Text -notmatch $Pattern) {
        $failures.Add("missing implementation: $Name -> $Pattern")
    }
}

function Assert-NotContains([string]$Name, [string]$Text, [string]$Pattern) {
    if ($Text -match $Pattern) {
        $failures.Add("unexpected implementation: $Name -> $Pattern")
    }
}

function Assert-FilePairContains([string]$RelativePath, [string[]]$Patterns) {
    $fsPath = Join-Path $FsClientRoot $RelativePath
    $ecmaPath = Join-Path $EcmaRoot ("Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/" + $RelativePath.Substring("Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/".Length))
    $fsText = Read-Text $fsPath
    $ecmaText = Read-Text $ecmaPath

    foreach ($pattern in $Patterns) {
        Assert-Contains "FS FSPatcher sync: $RelativePath" $fsText $pattern
        Assert-Contains "ECMA335 FSPatcher sync: $RelativePath" $ecmaText $pattern
    }
}

$runtimeApiCs = Read-Text (Join-Path $FsClientRoot "Packages/com.code-philosophy.hybridclr/Runtime/RuntimeApi.cs")
$hybridMgr = Read-Text (Join-Path $FsClientRoot "Assets/Scripts/Main/GameAppHybridCLRManager.cs")
$buildHybrid = Read-Text (Join-Path $FsClientRoot "Assets/Editor/BuildPackage.HybridCLR.cs")
$buildPackage = Read-Text (Join-Path $FsClientRoot "Assets/Editor/BuildPackage.cs")

Assert-Contains "RuntimeApi.cs AOT file api" $runtimeApiCs 'LoadMetadataForAOTAssemblyFromFile'
Assert-Contains "RuntimeApi.cs Hotfix file api" $runtimeApiCs 'LoadHotfixAssemblyFromFile'
Assert-Contains "RuntimeApi.cs returns Assembly" $runtimeApiCs 'System\.Reflection\.Assembly|Assembly\s+LoadHotfixAssemblyFromFile'

Assert-Contains "GameAppHybridCLRManager low memory switch" $hybridMgr 'DeviceQualityUtil\.IsLowMemory\(\)'
Assert-Contains "GameAppHybridCLRManager uses MapFileLoader" $hybridMgr 'MapFileLoader'
Assert-Contains "GameAppHybridCLRManager AOT catalogue check" $hybridMgr 'CanUseMemoryMappedFile'
Assert-Contains "GameAppHybridCLRManager AOT file api call" $hybridMgr 'LoadMetadataForAOTAssemblyFromFile'
Assert-Contains "GameAppHybridCLRManager Hotfix file api call" $hybridMgr 'LoadHotfixAssemblyFromFile'

Assert-Contains "BuildPackage.HybridCLR AOT catalogue" $buildHybrid 'GenerateAOTMetadataCatalogue'
Assert-Contains "BuildPackage.HybridCLR AOTAssemblies catalogue" $buildHybrid 'AOTAssemblies'
Assert-Contains "BuildPackage menu" $buildPackage 'GenerateAOTMetadataCatalogue'

Assert-FilePairContains "Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/libil2cpp/hybridclr/metadata/RawImageBase.h" @(
    "RawImageDataOwner",
    "FSNativeMmap"
)
Assert-FilePairContains "Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/libil2cpp/hybridclr/metadata/RawImageBase.cpp" @(
    "FSNative::SysCallWrapper::MunmapFile",
    "RawImageDataOwner::FSNativeMmap"
)
Assert-FilePairContains "Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/libil2cpp/fs-native/SysCallWrapper.h" @(
    "MmapFileFromPath"
)
Assert-FilePairContains "Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/libil2cpp/fs-native/SysCallWrapper.cpp" @(
    "MmapFileFromPath",
    "g_mappedFiles"
)
Assert-FilePairContains "Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/libil2cpp/hybridclr/RuntimeApi.cpp" @(
    "LoadMetadataForAOTAssemblyFromFile",
    "LoadHotfixAssemblyFromFile",
    "StringUtils::Utf16ToUtf8"
)
Assert-FilePairContains "Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/libil2cpp/hybridclr/metadata/Assembly.cpp" @(
    "LoadMetadataForAOTAssemblyFromFile",
    "LoadFromFile",
    "RawImageDataOwner::FSNativeMmap"
)

$assemblyCpp = Read-Text (Join-Path $FsClientRoot "Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/libil2cpp/hybridclr/metadata/Assembly.cpp")
$aotFileMethod = [regex]::Match($assemblyCpp, 'LoadMetadataForAOTAssemblyFromFile[\s\S]*?\n\s*\}\s*\n', [System.Text.RegularExpressions.RegexOptions]::Multiline)
if ($aotFileMethod.Success) {
    Assert-NotContains "AOT mmap file api forbids CopyBytes" $aotFileMethod.Value 'CopyBytes'
} else {
    $failures.Add("cannot locate AOT mmap file api body: Assembly.cpp")
}

$runtimeApiCpp = Read-Text (Join-Path $FsClientRoot "Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/libil2cpp/hybridclr/RuntimeApi.cpp")
$runtimeApiCppEcma = Read-Text (Join-Path $EcmaRoot "Packages/com.code-philosophy.hybridclr/Data~/FSPatcher/libil2cpp/hybridclr/RuntimeApi.cpp")
Assert-NotContains "FS RuntimeApi.cpp forbids String_t-only marshal helper" $runtimeApiCpp 'il2cpp_codegen_marshal_string\((dllPath|assemblyPath)\)'
Assert-NotContains "ECMA335 RuntimeApi.cpp forbids String_t-only marshal helper" $runtimeApiCppEcma 'il2cpp_codegen_marshal_string\((dllPath|assemblyPath)\)'

if ($failures.Count -gt 0) {
    Write-Host "HybridCLR mmap static verification failed, count=$($failures.Count):"
    foreach ($failure in $failures) {
        Write-Host " - $failure"
    }
    exit 1
}

Write-Host "HybridCLR mmap static verification passed."
