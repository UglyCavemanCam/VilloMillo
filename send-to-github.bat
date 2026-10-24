@echo off
rem Sends your desktop changes to GitHub so the cloud session can see them.
rem Run it from anywhere: it works in the folder this file is in.
setlocal
cd /d "%~dp0"

set BRANCH=claude/charming-fermi-y9hz49

echo.
echo === Saving your changes ===
git add -A
git diff --cached --quiet
if errorlevel 1 (
    git commit -m "Desktop sync %DATE% %TIME%"
    if errorlevel 1 goto :fail
) else (
    echo Nothing new to save.
)

echo.
echo === Bringing in anything new from GitHub first ===
git pull --no-rebase --no-edit origin %BRANCH%
if errorlevel 1 (
    echo.
    echo The pull hit a conflict: the same file was changed here and in the cloud.
    echo Nothing was sent. Tell Claude "merge conflict" and it will help.
    goto :fail
)

echo.
echo === Sending to GitHub ===
git push origin HEAD:%BRANCH%
if errorlevel 1 goto :fail

echo.
echo Done. GitHub now has your latest files on branch %BRANCH%.
pause
exit /b 0

:fail
echo.
echo Something went wrong. Copy the text above and send it to Claude.
pause
exit /b 1
