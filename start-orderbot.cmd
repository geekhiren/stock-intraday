@echo off
rem Starts the OrderBOT paper dashboard. Used by the weekday scheduled task; safe to double-click.
rem If a dashboard is already running, this one exits because the port is in use.
cd /d "%~dp0"
if not exist var\log mkdir var\log
echo [%date% %time%] starting >> var\log\dashboard-console.log
python run.py --dashboard >> var\log\dashboard-console.log 2>&1
echo [%date% %time%] stopped with code %errorlevel% >> var\log\dashboard-console.log
