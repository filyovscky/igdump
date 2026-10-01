@echo off
setlocal

set "SCRIPT_DIR=%~dp0"

if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    "%SCRIPT_DIR%.venv\Scripts\python.exe" "%SCRIPT_DIR%insta_html_export.py" %*
) else if exist "%SCRIPT_DIR%.venv\bin\python" (
    "%SCRIPT_DIR%.venv\bin\python" "%SCRIPT_DIR%insta_html_export.py" %*
) else (
    echo Python 3 not found in .venv. Create venv first:
    echo   python -m venv .venv
    echo   pip install -r requirements.txt
    exit /b 1
)
