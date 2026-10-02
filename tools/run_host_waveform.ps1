param(
    [Parameter(Mandatory=$true)]
    [string]$Port
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
python (Join-Path $PSScriptRoot 'host_waveform.py') --port $Port --loop
