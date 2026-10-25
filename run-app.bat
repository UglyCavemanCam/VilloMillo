@echo off
rem Starts everything VilloMillo needs and opens the dashboard in your browser:
rem   - the background worker, which looks at links, makes clips and posts them;
rem   - the dashboard;
rem   - the clip engine: "villomillo run" starts it if nothing answers, starts it again if it stops, and saves its output
rem     in data\villomillo\engine.log. Settings in the dashboard shows its state with Start, Restart and Open log.
rem Close this window (or press Ctrl+C) to stop the app. The clip engine it started stops with it.
setlocal
cd /d "%~dp0"

rem serve = dashboard only. run = dashboard plus the background worker that looks at pasted links, makes clips and posts.
set MODE=run
set URL=http://127.0.0.1:8780

rem Prefer the project's own virtual environment (.venv): it already has the right Python and packages.
rem Otherwise look for Python 3.11 or newer, newest first. An old Python on PATH (3.8, 3.9)
rem cannot install the packages, so it is skipped.
set PY=
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
for %%V in (3.13 3.12 3.11) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>nul && set PY=py -%%V
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul && set PY=python
)
if not defined PY (
    echo.
    echo This app needs Python 3.11 or newer, and only an older one was found.
    echo Install Python 3.12 from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    echo Keep your old Python; both can stay installed. Then run this again.
    pause
    exit /b 1
)
echo Using: %PY%
%PY% --version

rem The dashboard needs these packages; install whatever is missing.
%PY% -c "import fastapi, uvicorn, jinja2, yaml, yt_dlp, cv2, numpy, PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing the packages the app needs. This only happens once...
    %PY% -m pip install -r requirements-villomillo.txt
    if errorlevel 1 (
        echo.
        echo The install failed. Copy the text above and send it to Claude.
        pause
        exit /b 1
    )
)

rem The clip engine needs more packages than the dashboard (speech recognition, vision). This is large and happens once.
%PY% scripts\engine_check.py deps
if errorlevel 1 (
    echo Installing the clip engine's packages. This is large and can take a while. It only happens once...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo The clip engine's install failed. Copy the text above and send it to Claude.
        pause
        exit /b 1
    )
)

echo.
echo Starting VilloMillo ^(%MODE%^). The clip engine can take a minute or two to load the first time. The browser opens in a few seconds.
start "" cmd /c "timeout /t 4 /nobreak >nul & start %URL%"

%PY% -m villomillo %MODE%

echo.
echo The app stopped. If that was not on purpose, copy the text above and send it to Claude.
pause
