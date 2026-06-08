# Ophelia Project — Windows / PowerShell install
# Usage: cd e:\Projects\Ophelia ; .\scripts\install.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host ""
Write-Host "=== Ophelia Project — Windows install ===" -ForegroundColor Cyan
Write-Host ""

Set-Location $Root

Write-Host "[1/3] Installing Python package..." -ForegroundColor Yellow
pip install -e .
pip install "uvicorn[standard]>=0.32"

Write-Host "[2/3] Auto-setup (~/.ophelia, .env)..." -ForegroundColor Yellow
ophelia setup --do

Write-Host "[3/3] Full setup guide..." -ForegroundColor Yellow
ophelia setup

Write-Host ""
Write-Host "Done. Next: ophelia check --chat-only" -ForegroundColor Green
