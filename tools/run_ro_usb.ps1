param(
    [string]$Port = 'COM15',
    [string]$Resource = 'USB0::0xF4EC::0x1101::SDG6XFCQ900843::INSTR',
    [int]$MaxCases = 0
)
$ErrorActionPreference = 'Stop'
$roProjectRoot = Split-Path $PSScriptRoot -Parent
$roUsbArguments = @((Join-Path $roProjectRoot 'server.py'), '--port', $Port,
    '--ro-transport', 'visa', '--ro-resource', $Resource)
if ($MaxCases -gt 0) {
    $roUsbArguments += @('--max-cases', $MaxCases)
}
python @roUsbArguments
exit $LASTEXITCODE
