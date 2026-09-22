$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path $PSScriptRoot -Parent
$rulesFiles = @(
    (Join-Path (Get-Location) 'makefile'),
    (Join-Path (Get-Location) 'subdir_rules.mk'),
    (Join-Path $projectRoot 'Debug\makefile'),
    (Join-Path $projectRoot 'Debug\subdir_rules.mk')
)
$compilerMatch = Select-String -Path $rulesFiles -ErrorAction SilentlyContinue -Pattern '"([^"]+[\\/]tools[\\/]compiler[\\/][^" ]+[\\/]bin[\\/]tiarmclang\.exe)"' |
    Select-Object -First 1

if (-not $compilerMatch) {
    $compilerMatch = Select-String -Path $rulesFiles -ErrorAction SilentlyContinue -Pattern '^CG_TOOL_ROOT\s*:=\s*(.+)$' |
        Select-Object -First 1
}

if (-not $compilerMatch) {
    throw "Unable to locate the TI Arm Clang path in the generated makefiles."
}

$compilerPath = $compilerMatch.Matches[0].Groups[1].Value
if ($compilerMatch.Line -match '^CG_TOOL_ROOT\s*:=\s*(.+)$') {
    $compilerPath = Join-Path $Matches[1].Trim() 'bin\tiarmclang.exe'
}
$ccsRoot = [IO.Path]::GetFullPath((Join-Path (Split-Path $compilerPath -Parent) '..\..\..\..'))
$sysConfig = Get-ChildItem -Path (Join-Path $ccsRoot 'utils') -Directory -Filter 'sysconfig_*' |
    Sort-Object Name -Descending |
    ForEach-Object {
        $candidate = Join-Path $_.FullName 'sysconfig_cli.bat'
        if (Test-Path $candidate) { Get-Item $candidate }
    } |
    Select-Object -First 1

if (-not $sysConfig) {
    throw "Unable to locate sysconfig_cli.bat below $ccsRoot."
}

& $env:ComSpec /d /c call $sysConfig.FullName @args
exit $LASTEXITCODE
