@echo off
rem Launches the usage widget. A console window flashes briefly then closes.
rem Prefer run-widget.vbs for a completely silent start.
cd /d "%~dp0"
set "PYW=%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
start "" "%PYW%" "usage_widget.pyw"
