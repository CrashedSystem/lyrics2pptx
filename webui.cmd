@echo off
rem lyrics2pptx - start the local web UI in a VISIBLE console.
rem Close this window (or press Ctrl+C) to stop the server.
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHONPATH=%~dp0src"
set "PYTHONIOENCODING=utf-8"
if not defined LYRICS2PPTX_BIND_PORT set "LYRICS2PPTX_BIND_PORT=8765"

set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE (
    where py >nul 2>nul && set "PYEXE=py -3.12"
)
if not defined PYEXE (
    where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
    echo Python 3.12 was not found. See README section 1.
    pause
    exit /b 1
)

echo ============================================================
echo  lyrics2pptx web UI
echo    URL : http://127.0.0.1:%LYRICS2PPTX_BIND_PORT%/
echo    stop: close this window, or press Ctrl+C
echo ============================================================
echo.

rem Open the browser once the server has had a moment to bind the port.
start "" cmd /c "timeout /t 2 >nul & start "" http://127.0.0.1:%LYRICS2PPTX_BIND_PORT%/"

%PYEXE% -X utf8 -m lyrics2pptx.webapp

echo.
echo Server stopped.
pause
