param(
    [ValidateRange(1500,1700)][int]$TimerTicks = 1600,
    [string]$Sdk = 'C:/ti/mspm0_sdk_2_11_00_07',
    [string]$Ccs = 'C:/ti/ccs2101/ccs'
)
$ErrorActionPreference = 'Stop'
$dutyRoot = Split-Path $PSScriptRoot -Parent
$dutyBuild = Join-Path $dutyRoot "tmp/firmware-duty-$TimerTicks"
New-Item -ItemType Directory -Force -Path $dutyBuild | Out-Null
$dutyCompiler = Join-Path $Ccs 'tools/compiler/ti-cgt-armllvm_5.1.1.LTS/bin/tiarmclang.exe'
& (Join-Path $Ccs 'tools/node/node.exe') (Join-Path $Ccs 'utils/sysconfig_1.28.1/dist/cli.js') `
    -s "$Sdk/.metadata/product.json" --script "$dutyRoot/empty.syscfg" -o $dutyBuild --compiler ticlang
if ($LASTEXITCODE -ne 0) { throw 'SysConfig failed' }
$dutyFlags = @('-march=thumbv6m','-mcpu=cortex-m0plus','-mfloat-abi=soft',
    '-mlittle-endian','-mthumb','-O2','-g','-Wall','-Werror',
    '-D__MSPM0G3507__','-D__USE_SYSCONFIG__',"-DACQ_TIMER_PERIOD_TICKS=$TimerTicks",
    "-I$dutyRoot","-I$dutyBuild","-I$Sdk/source/third_party/CMSIS/Core/Include","-I$Sdk/source")
$dutySources = @("$dutyRoot/empty.c", "$dutyBuild/ti_msp_dl_config.c",
    "$Sdk/source/ti/devices/msp/m0p/startup_system_files/ticlang/startup_mspm0g350x_ticlang.c")
$dutyObjects = @()
foreach ($dutySource in $dutySources) {
    $dutyObject = Join-Path $dutyBuild (([IO.Path]::GetFileNameWithoutExtension($dutySource)) + '.obj')
    & $dutyCompiler @dutyFlags -c $dutySource -o $dutyObject
    if ($LASTEXITCODE -ne 0) { throw "Compilation failed: $dutySource" }
    $dutyObjects += $dutyObject
}
& $dutyCompiler @dutyFlags @dutyObjects "-Wl,-i$Sdk/source" `
    "-Wl,-i$Ccs/tools/compiler/ti-cgt-armllvm_5.1.1.LTS/lib" `
    '-Wl,--rom_model' "$dutyBuild/device_linker.cmd" `
    '-Wl,-lti/driverlib/lib/ticlang/m0p/mspm0g1x0x_g3x0x/driverlib.a' '-Wl,-llibc.a' `
    "-Wl,-m$dutyBuild/acquisition.map" -o "$dutyBuild/acquisition.out"
if ($LASTEXITCODE -ne 0) { throw 'Link failed' }
Write-Output "Built $dutyBuild/acquisition.out (timer ticks $TimerTicks; flash and recalibration are separate steps)"
