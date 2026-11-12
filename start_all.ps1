# 一键启动：MySQL + Web
# 用法：powershell -ExecutionPolicy Bypass -File .\start_all.ps1
# 停止：Ctrl+C 停 Web，再运行 .\stop_all.ps1 停 MySQL

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

& (Join-Path $root "start_mysql.ps1")
& (Join-Path $root "run_dev.ps1")
