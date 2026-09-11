@echo off
REM zen-claude-proxy launcher for Windows. Usage:
REM   zen-claude-proxy.bat --key sk-zen-... [--override claude-opus-4-5] [--port 8646]
python "%~dp0zen-claude-proxy.py" %*
