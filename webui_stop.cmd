@echo off
rem lyrics2pptx - stop every running local web UI instance.
rem Usage: double-click this file, or run "webui_stop.cmd [port]".
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

echo Stopping lyrics2pptx web UI (port %PORT%) ...

rem 1) Kill by listening port, if anything is bound there.
set "KILLED="
for /f "tokens=5" %%p in ('netstat -ano -p TCP ^| findstr /r /c:"LISTENING" ^| findstr /c:":%PORT% "') do (
    taskkill /f /pid %%p >nul 2>nul && (
        echo   killed pid %%p ^(listening on %PORT%^)
        set "KILLED=1"
    )
)

rem 2) Kill any hidden instance started by the .vbs (random port), by command line.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*lyrics2pptx.webapp*' -and $_.Name -like 'python*' }; if (-not $p) { exit 0 } else { foreach ($x in $p) { Write-Host ('  killed pid ' + $x.ProcessId + ' (lyrics2pptx.webapp)'); Stop-Process -Id $x.ProcessId -Force } }"

echo Done.
pause
