@echo off
rem Starts the VilloMillo dashboard on this PC and opens it in your browser.
rem Close this window (or press Ctrl+C) to stop the app.
setlocal
cd /d "%~dp0"

rem serve = dashboard only. run = dashboard plus the background worker that does research cycles.
set MODE=serve
set URL=http://127.0.0.1:8780

set PY=python
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo Python was not found. Install Python 3.11 or newer from python.org and tick "Add to PATH".
        pause
        exit /b 1
    )
    set PY=py -3
)

rem The dashboard needs these packages; install whatever is missing.
%PY% -c "import fastapi, uvicorn, jinja2, yaml, yt_dlp, cv2, numpy, PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing the packages the app needs. This only happens once...
    %PY% -m pip install -r requirements-villomillo.txt yt-dlp opencv-python-headless numpy pillow
    if errorlevel 1 (
        echo.
        echo The install failed. Copy the text above and send it to Claude.
        pause
        exit /b 1
    )
)

echo.
echo Starting VilloMillo (%MODE%). The browser opens in a few seconds.
start "" cmd /c "timeout /t 4 /nobreak >nul & start %URL%"

%PY% -m villomillo %MODE%

echo.
echo The app stopped. If that was not on purpose, copy the text above and send it to Claude.
pause
