# 启动开发服务器（MySQL 版）
# 用法：先运行 .\start_mysql.ps1，再运行 .\run_dev.ps1
# 打开 http://127.0.0.1:5000

$env:SECRET_KEY  = "dev-secret-key-change-me"
$env:DATABASE_URL = "mysql+pymysql://hero_app:HeroApp2026@127.0.0.1:3306/hero?charset=utf8mb4"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "找不到虚拟环境：$python，请先运行 python -m venv .venv 并安装依赖。"
    exit 1
}

Write-Host "Hotel Hero -> http://127.0.0.1:5000"
Set-Location $PSScriptRoot
& $python -m flask --app app run --host 127.0.0.1 --port 5000
