param(
    [Parameter(Mandatory=$true)]
    [string]$Port,
    [string]$DataDir = (Join-Path (Split-Path $PSScriptRoot -Parent) 'data')
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
python (Join-Path $root 'server.py') --port $Port --data-dir $DataDir
exit $LASTEXITCODE
