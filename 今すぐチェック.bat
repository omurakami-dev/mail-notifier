@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Gmailを今すぐチェックします(時間帯・曜日の設定に関係なく1回だけ)...
where py >nul 2>nul
if %errorlevel%==0 (py "%~dp0mail_notifier.py" --once) else (python "%~dp0mail_notifier.py" --once)
echo 終わりました。結果は mail_notifier.log にも書かれています。
pause
