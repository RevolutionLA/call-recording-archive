@echo off
rem =====================================================================
rem  CallRec - start the local cockpit (double click me)
rem  No console window is left behind: the real work happens in
rem  scripts\launcher.py, which launches the web service hidden and then
rem  opens http://127.0.0.1:8760 in your browser.
rem  Everything the pipeline used to need a command line for now lives on
rem  the "工作台" tab of that page.
rem
rem  Pass "auto" as an argument (or use the .bat next to it) to also switch
rem  on the overnight ingest loop right after the page comes up.
rem =====================================================================
cd /d "%~dp0"
set "PYW=C:\ProgramData\anaconda3\envs\sensevoice\pythonw.exe"
if exist "%PYW%" goto run
set "PYW=pythonw"
where pythonw >nul 2>nul && goto run
echo Python not found. Edit this file and point PYW at your pythonw.exe
echo (for example C:\ProgramData\anaconda3\envs\sensevoice\pythonw.exe).
pause
exit /b 1
:run
start "" "%PYW%" scripts\launcher.py %*
exit /b 0
