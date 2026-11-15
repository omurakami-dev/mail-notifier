@echo off
chcp 65001 >nul
powershell -NoProfile -Command "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Startup')+'\MailNotifier.lnk')"
echo 自動起動の登録を解除しました。
pause
