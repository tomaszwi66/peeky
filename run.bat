@echo off
REM Launch Peeky in the background ^(no console window^).
cd /d "%~dp0"
start "" pythonw peeky.py
