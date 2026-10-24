@echo off
rem Pulls the files Claude edited in the cloud down to this desktop folder.
rem Run it from anywhere: it works in the folder this file is in.
setlocal
cd /d "%~dp0"

set BRANCH=claude/charming-fermi-y9hz49

echo.
echo === Checking for unsaved changes on this desktop ===
git status --porcelain > "%TEMP%\villomillo-status.txt"
for %%A in ("%TEMP%\villomillo-status.txt") do set SIZE=%%~zA
if not "%SIZE%"=="0" (
    echo.
    echo You have changes here that are not saved yet. Run send-to-github.bat first,
    echo then run this again. Nothing was downloaded.
    del "%TEMP%\villomillo-status.txt"
    goto :fail
)
del "%TEMP%\villomillo-status.txt"

echo.
echo === Downloading Claude's changes ===
git pull --no-rebase --no-edit origin %BRANCH%
if errorlevel 1 (
    echo.
    echo The pull hit a conflict. Tell Claude "merge conflict" and it will help.
    goto :fail
)

echo.
echo Done. This folder now has the latest files. Restart the app if it is running.
pause
exit /b 0

:fail
echo.
echo Stopped. Copy the text above and send it to Claude if you need help.
pause
exit /b 1
