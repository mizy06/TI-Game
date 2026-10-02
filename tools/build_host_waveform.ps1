$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$out = Join-Path $PSScriptRoot 'waveform_analyzer_host.dll'
$cc = Get-Command clang,gcc,cc -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $cc) { throw 'Install LLVM/Clang or MinGW GCC, then rerun this script.' }
& $cc.Source -std=c99 -O2 -shared -fPIC -I $root `
    (Join-Path $root 'waveform_analyzer.c') `
    (Join-Path $root 'waveform_cascade.c') `
    (Join-Path $PSScriptRoot 'host_api.c') -o $out
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Built $out"
