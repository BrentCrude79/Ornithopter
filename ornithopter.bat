@echo off
REM ornithopter launcher for Windows. Usage:
REM   ornithopter.bat --key sk-zen-... [--override claude-opus-4-5] [--port 8646]
python "%~dp0ornithopter.py" %*
