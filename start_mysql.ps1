# 启动本地 MySQL 8（免安装版，端口 3306）
# 用法：右键“使用 PowerShell 运行”，或在终端执行 .\start_mysql.ps1

$mysqld  = "C:\Users\a7268\mysql\mysql-8.0.29-winx64\bin\mysqld.exe"
$basedir = "C:/Users/a7268/mysql/mysql-8.0.29-winx64"
$datadir = "C:/Users/a7268/mysql/data"

$listening = Get-NetTCPConnection -LocalPort 3306 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "MySQL 已在 3306 监听，无需重复启动。"
    exit 0
}

if (-not (Test-Path $mysqld)) {
    Write-Error "找不到 mysqld：$mysqld"
    exit 1
}

Write-Host "正在启动 MySQL..."
Start-Process -FilePath $mysqld `
    -ArgumentList "--basedir=$basedir", "--datadir=$datadir", "--port=3306" `
    -WindowStyle Hidden

Start-Sleep -Seconds 3
if (Get-NetTCPConnection -LocalPort 3306 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "MySQL 已启动：127.0.0.1:3306"
} else {
    Write-Warning "3 秒内未监听 3306，请检查 $datadir 下的 .err 日志。"
}
