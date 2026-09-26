@echo off
rem Stage-A transcribe keepalive: parallel workers, auto-restart on crash, exits when queue empty.
cd /d "%~dp0.."
set "PY=C:\ProgramData\anaconda3\envs\sensevoice\python.exe"
:loop
"%PY%" scripts\check_pending.py || goto done
"%PY%" pipeline.py run --workers 1 >> logs\run_all.log 2>&1
echo [%date% %time%] run exited (rc=%errorlevel%), re-check queue >> logs\run_keepalive.log
rem rc>=75 means memory exhausted: wait longer for RAM to free up
if errorlevel 75 (timeout /t 60 /nobreak >nul) else (timeout /t 5 /nobreak >nul)
goto loop
:done
echo [%date% %time%] queue empty, all done >> logs\run_keepalive.log
