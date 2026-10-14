@echo off
cd /d "%~dp0"
set "PY=C:\Users\Admin\AppData\Local\Programs\Python\Python312\pythonw.exe"
if not exist "%PY%" set "PY=pythonw.exe"
start "" "%PY%" "phone_mirror.py"
