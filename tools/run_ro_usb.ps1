param(
    [string]$Port = 'COM15',
    [string]$Resource = 'USB0::0xF4EC::0x1101::SDG6XFCQ900843::INSTR',
    [int]$MaxCases = 0,
    [ValidateRange(1, 8)][int]$Workers = 3
)
$ErrorActionPreference = 'Stop'
$roProjectRoot = Split-Path $PSScriptRoot -Parent
$roUsbArguments = @((Join-Path $roProjectRoot 'server.py'), 'RO', '--port', $Port,
    '--ro-transport', 'visa', '--ro-resource', $Resource, '--ro-timeout', '14',
    '--r-frames', '3', '--r-workers', "$Workers", '--ro-vpp-calibrate', '--ro-oversample', '32', '--ro-stop-on-failure')
if ($MaxCases -gt 0) {
    $roUsbArguments += @('--max-cases', $MaxCases)
}
python @roUsbArguments
exit $LASTEXITCODE
