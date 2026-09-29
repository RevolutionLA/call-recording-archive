@echo off
rem CallRec - stop the local background service (asks for confirmation first).
rem Running stages are interrupted but per-call progress is saved; the next
rem start + "工作台" click resumes where it stopped.
cd /d "%~dp0"
set "PYW=C:\ProgramData\anaconda3\envs\sensevoice\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" scripts\launcher.py --stop
exit /b 0
