@echo off
rem Starts BaiDesk automatically when this PC turns on (before anyone signs in). RUN AS ADMINISTRATOR.
schtasks /create /tn "BaiDesk" /tr "cmd /c \"cd /d %~dp0 && start_baidesk.bat\"" /sc onstart /ru SYSTEM /rl HIGHEST /f
if errorlevel 1 (echo Failed - did you run this as administrator? & pause & exit /b 1)
echo Done. BaiDesk will start by itself whenever the PC starts.
echo To remove:  schtasks /delete /tn "BaiDesk" /f
pause
