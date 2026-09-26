@echo off
rem Auto-ingest keepalive: watch the recordings folder and run the full chain
rem scan -> transcribe -> align -> summarize -> graph on a loop, forever.
rem Each stage is incremental + stage-locked, so idle cycles are cheap and a
rem manual `pipeline.py` run never collides with this watcher.
cd /d "%~dp0.."
set "PY=C:\ProgramData\anaconda3\envs\sensevoice\python.exe"
if not exist logs mkdir logs
:loop
"%PY%" pipeline.py scan >> logs\auto.log 2>&1
"%PY%" pipeline.py run --workers 1 >> logs\auto.log 2>&1
rem 精修：Qwen3-ASR 重听已切分段（内存不足时自动跳过本轮，不阻塞后面阶段）
"%PY%" pipeline.py refine >> logs\auto.log 2>&1
"%PY%" pipeline.py align   >> logs\auto.log 2>&1
"%PY%" pipeline.py summarize >> logs\auto.log 2>&1
"%PY%" pipeline.py graph   >> logs\auto.log 2>&1
echo [%date% %time%] cycle done, next sweep in 300s >> logs\auto_keepalive.log
timeout /t 300 /nobreak >nul
goto loop
