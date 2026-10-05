$ErrorActionPreference = 'Stop'

# SysConfig launcher for the CCS makefile.
#
# History / why this is not a plain `sysconfig_cli.bat` call:
#   CCS runs gmake without a console, and node/cmd both refuse to run in that
#   context.  Two earlier implementations failed here:
#     1. `& $env:ComSpec /d /c call <bat>` -> PowerShell 5.1 parses the bare
#        `/d` token as a division operator and throws BadExpression at parse
#        time.  Even with the syntax fixed it silently no-ops, because
#        $env:ComSpec is unset when CCS spawns the build.
#     2. `& cmd.exe /d /c 'call <bat> ...'` -> parses and exits 0, but the
#        batch file never actually executes: cmd.exe and .bat are console
#        applications and cannot attach to the console-less child gmake
#        creates, so the build "succeeds" while producing no files at all.
#
# sysconfig_cli.bat is only a shim that locates node.exe and runs dist/cli.js.
# We skip it entirely and launch node.exe (a headless, non-console process)
# directly against cli.js, which works from any context.

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

# Prefer the sysconfig release bundled with CCS; fall back to any sibling.
$sysConfigDir = Get-ChildItem -Path (Join-Path $ccsRoot 'utils') -Directory -Filter 'sysconfig_*' |
    Where-Object { Test-Path (Join-Path $_.FullName 'dist\cli.js') } |
    Sort-Object Name -Descending |
    Select-Object -First 1

if (-not $sysConfigDir) {
    throw "Unable to locate a sysconfig dist\cli.js below $ccsRoot."
}

$cliJs = Join-Path $sysConfigDir.FullName 'dist\cli.js'

# Resolve node.exe the same way sysconfig_cli.bat does, but without the shim:
#   1. <sysconfig>\nodejs\node.exe   (standalone bundle)
#   2. <ccsRoot>\tools\node\node.exe (CCS-bundled node)
#   3. whatever `node` resolves to on PATH
$standaloneNode = Join-Path $sysConfigDir.FullName 'nodejs\node.exe'
$ccsNode = Join-Path $ccsRoot 'tools\node\node.exe'
if (Test-Path $standaloneNode) {
    $nodeExe = $standaloneNode
} elseif (Test-Path $ccsNode) {
    $nodeExe = $ccsNode
} else {
    $nodeCmd = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCmd) {
        throw "Unable to locate node.exe; sysconfig_cli.bat needs it too."
    }
    $nodeExe = $nodeCmd.Source
}

# Pass arguments through as a real array (- not a rebuilt command string) so
# PowerShell handles quoting of the --script / -o values correctly.
& $nodeExe $cliJs @args
exit $LASTEXITCODE
