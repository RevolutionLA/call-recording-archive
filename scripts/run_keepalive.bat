@echo off
rem Stage-A transcribe keepalive: parallel workers, auto-restart on crash, exits when queue empty.
cd /d E:\AI\2Voice\FunASR
:loop
"C:\ProgramData\anaconda3\envs\sensevoice\python.exe" scripts\check_pending.py || goto done
"C:\ProgramData\anaconda3\envs\sensevoice\python.exe" pipeline.py run --workers 1 >> logs\run_all.log 2>&1
echo [%date% %time%] run exited (rc=%errorlevel%), re-check queue in 5s >> logs\run_keepalive.log
timeout /t 5 /nobreak >nul
goto loop
:done
echo [%date% %time%] queue empty, all done >> logs\run_keepalive.log
