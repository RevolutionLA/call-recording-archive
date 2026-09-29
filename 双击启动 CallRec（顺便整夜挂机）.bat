@echo off
rem CallRec - start the cockpit AND switch on the overnight ingest loop
rem (same as opening the page and pressing "整夜挂机").
rem It keeps running the whole chain every 5 minutes until you press stop in
rem the page, or run 停止 CallRec.bat. Keep the machine awake (no sleep).
cd /d "%~dp0"
set "PYW=C:\ProgramData\anaconda3\envs\sensevoice\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" scripts\launcher.py auto
exit /b 0
