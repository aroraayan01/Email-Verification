@echo off
cd /d "%~dp0"
echo Starting Email Verifier...
echo.
echo   Open this in your browser:  http://127.0.0.1:8000
echo   Press Ctrl+C to stop.
echo.
start "" http://127.0.0.1:8000
python -m uvicorn webapp.main:app --port 8000
pause
