@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" -c "import yt_dlp, imageio_ffmpeg, requests, googleapiclient, faster_whisper" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" -m clipforge
if errorlevel 1 goto :failed
exit /b 0
:failed
echo ClipForge could not start. See the error above.
pause
exit /b 1
