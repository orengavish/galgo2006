@echo off
REM Galgo June — scheduled daily tick fetcher
REM Run by Windows Task Scheduler at 17:30 CT (23:30 UTC / 00:30 UTC+1)
REM This is the ONLY scheduled fetcher — runs on this computer only.

cd /d C:\Projects\galgo2026\june
python trader\fetch_scheduler.py >> logs\fetch_scheduler.log 2>&1
