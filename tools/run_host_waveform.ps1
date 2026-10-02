param(
    [string]$Port = "COM10"
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
python tools\host_waveform.py --port $Port --loop
