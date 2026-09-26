@echo off
rem Web dashboard keepalive: restart uvicorn whenever it exits.
cd /d E:\AI\2Voice\FunASR
:loop
"C:\ProgramData\anaconda3\envs\sensevoice\python.exe" pipeline.py web >> logs\web.log 2>&1
echo [%date% %time%] web exited, restart in 3s >> logs\web_keepalive.log
timeout /t 3 /nobreak >nul
goto loop
