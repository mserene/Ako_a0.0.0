@echo off
setlocal

set "SCRIPT_DIR=%~dp0"

if exist "%SCRIPT_DIR%Ako-ai_launcher.vbs" (
    wscript.exe //nologo "%SCRIPT_DIR%Ako-ai_launcher.vbs"
    exit /b %ERRORLEVEL%
)

if exist "%SCRIPT_DIR%bootstrap_runtime.bat" (
    call "%SCRIPT_DIR%bootstrap_runtime.bat" --no-pause
    if errorlevel 1 exit /b 1
)

if exist "%SCRIPT_DIR%Ako-ai.exe" (
    start "" "%SCRIPT_DIR%Ako-ai.exe"
    exit /b 0
)

echo Ako-ai.exe not found.
pause
exit /b 1
