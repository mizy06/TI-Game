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

for ($index = 0; $index -lt $args.Count; $index++) {
    $argument = $args[$index]
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
