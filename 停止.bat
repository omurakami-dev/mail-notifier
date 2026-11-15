@echo off
chcp 65001 >nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*mail_notifier.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo メール見張り番を停止しました。
timeout /t 3 >nul
