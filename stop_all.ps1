# 一键停止：Web + MySQL
# 用法：powershell -ExecutionPolicy Bypass -File .\stop_all.ps1

$mysqladmin = "C:\Users\a7268\mysql\mysql-8.0.29-winx64\bin\mysqladmin.exe"

# 1) 停 Web（监听 5000 的进程）
$web = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
if ($web) {
    $webPid = ($web | Select-Object -First 1).OwningProcess
    Stop-Process -Id $webPid -Force
    Write-Host "已停止 Web 服务器（PID $webPid）。"
} else {
    Write-Host "Web 服务器未在运行。"
}

# 2) 优雅停止 MySQL
$mysql = Get-NetTCPConnection -LocalPort 3306 -State Listen -ErrorAction SilentlyContinue
if ($mysql) {
    if (Test-Path $mysqladmin) {
        & $mysqladmin -u root -p"RootLocal2026" -h 127.0.0.1 shutdown 2>$null
        Write-Host "已停止 MySQL。"
    } else {
        Write-Warning "找不到 mysqladmin，尝试按端口结束进程。"
        $mysqlPid = ($mysql | Select-Object -First 1).OwningProcess
        Stop-Process -Id $mysqlPid -Force
        Write-Host "已强制停止 MySQL（PID $mysqlPid）。"
    }
} else {
    Write-Host "MySQL 未在运行。"
}
