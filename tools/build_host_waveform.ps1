$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$out = Join-Path $PSScriptRoot 'waveform_analyzer_host.dll'
$vsdev = Get-ChildItem `
    'C:\Program Files\Microsoft Visual Studio\2022', `
    'C:\Program Files (x86)\Microsoft Visual Studio\2022' `
    -Recurse -Filter VsDevCmd.bat -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($vsdev) {
    # Import MSVC headers, libraries, and Windows SDK variables into this
    # PowerShell process before invoking standalone LLVM Clang.
    $dump = cmd.exe /d /s /c "call `"$($vsdev.FullName)`" -arch=x64 >nul && set"
    foreach ($line in $dump) {
        if ($line -match '^([^=]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        }
    }
}
$compiler = @(
    (Join-Path $env:ProgramFiles 'LLVM\bin\clang.exe'),
    (Get-Command clang,gcc,cc -ErrorAction SilentlyContinue | Select-Object -First 1).Source
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $compiler) { throw 'Install LLVM/Clang or MinGW GCC, then rerun this script.' }
$pic = @()
if ($env:OS -ne 'Windows_NT') { $pic = @('-fPIC') }
& $compiler -std=c99 -O2 -shared @pic -I $root `
    (Join-Path $root 'waveform_analyzer.c') `
    (Join-Path $root 'waveform_cascade.c') `
    (Join-Path $PSScriptRoot 'host_api.c') -o $out
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Built $out"
