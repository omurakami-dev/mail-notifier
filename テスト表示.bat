@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py "%~dp0mail_notifier.py" --test) else (python "%~dp0mail_notifier.py" --test)
