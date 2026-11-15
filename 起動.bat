@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw "%~dp0mail_notifier.py"
) else (
  start "" pythonw "%~dp0mail_notifier.py"
)
echo メール見張り番を起動しました。この窓は閉じてかまいません。
timeout /t 3 >nul
