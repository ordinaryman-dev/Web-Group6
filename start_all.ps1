# 启动顺序：先启动数据库，再启动网页；整个流程复用同目录的两个脚本。
# 一键启动：MySQL + Web
# 用法：powershell -ExecutionPolicy Bypass -File .\start_all.ps1
# 停止：Ctrl+C 停 Web，再运行 .\stop_all.ps1 停 MySQL

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

& (Join-Path $root "start_mysql.ps1")
& (Join-Path $root "run_dev.ps1")
