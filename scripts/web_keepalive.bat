@echo off
rem Web dashboard keepalive: restart uvicorn whenever it exits.
cd /d "%~dp0.."
set "PY=C:\ProgramData\anaconda3\envs\sensevoice\python.exe"
:loop
"%PY%" pipeline.py web >> logs\web.log 2>&1
echo [%date% %time%] web exited, restart in 3s >> logs\web_keepalive.log
timeout /t 3 /nobreak >nul
goto loop
