$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path $PSScriptRoot -Parent
$makefile = @(
    (Join-Path (Get-Location) 'makefile'),
    (Join-Path $projectRoot 'Debug\makefile')
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $makefile) {
    throw 'Unable to locate a generated makefile.'
}

$rootMatch = Select-String -Path $makefile -Pattern '^CG_TOOL_ROOT\s*:=\s*(.+)$' | Select-Object -First 1
if (-not $rootMatch) {
    throw "Unable to locate CG_TOOL_ROOT in $makefile."
}

$compilerRoot = $rootMatch.Matches[0].Groups[1].Value.Trim()
$compilerPath = Join-Path $compilerRoot 'bin\tiarmclang.exe'
$compilerLib = [IO.Path]::GetFullPath((Join-Path $compilerRoot 'lib'))
$fixedArgs = [Collections.Generic.List[string]]::new()

# CCS 7.1 emits the SysConfig linker script twice for TI Clang projects:
# once as the positional file after --rom_model and once as a generated -l
# input.  The linker treats that second occurrence as another MEMORY block.
$hasRomModelScript = $false
for ($index = 0; $index -lt $args.Count - 1; $index++) {
    if ($args[$index] -eq '-Wl,--rom_model' -and
        $args[$index + 1].EndsWith('.cmd', [StringComparison]::OrdinalIgnoreCase)) {
        $hasRomModelScript = $true
        break
    }
}

for ($index = 0; $index -lt $args.Count; $index++) {
    $argument = $args[$index]

    if ($hasRomModelScript -and $argument.StartsWith('-Wl,-l') -and
        $argument.IndexOf('device_linker.cmd', [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        continue
    }

    if ($index + 1 -lt $args.Count -and $args[$index + 1].StartsWith('/')) {
        if ($argument -match '^(-I|-[^-]*,-i)[A-Za-z]$') {
            $fixedArgs.Add($argument + ':' + $args[++$index])
            continue
        }
    }

    if ($argument -match '^-Wl,-i.*[\\/]tools[\\/]compiler[\\/].*[\\/]lib"?$') {
        $fixedArgs.Add('-Wl,-i"' + $compilerLib + '"')
    } else {
        $fixedArgs.Add($argument)
    }
}

& $compilerPath @fixedArgs
exit $LASTEXITCODE
