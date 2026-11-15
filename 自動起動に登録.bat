@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\MailNotifier.lnk'); $s.TargetPath='%~dp0起動.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
echo パソコンの起動時に、メール見張り番が自動で立ち上がるように登録しました。
echo やめたいときは「自動起動を解除.bat」を実行してください。
pause
