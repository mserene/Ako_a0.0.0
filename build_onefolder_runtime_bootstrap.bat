@echo off
cd /d "%~dp0"

set PYTHON=.venv\Scripts\python.exe
set PYINSTALLER=.venv\Scripts\pyinstaller.exe
set LOG=build_log.txt

echo [INFO] Checking .venv...
if not exist "%PYTHON%" (
    echo [ERROR] .venv not found. Run: py -3.12 -m venv .venv
    pause
    exit /b 1
)

echo [INFO] Checking PyInstaller...
if not exist "%PYINSTALLER%" (
    echo [INFO] Installing PyInstaller...
    .venv\Scripts\pip.exe install pyinstaller
)

echo [INFO] Ensuring runtime dependencies (pyautogui stack)...
.venv\Scripts\pip.exe install -r requirements.txt

echo [INFO] Getting site-packages path...
for /f "delims=" %%i in ('.venv\Scripts\python.exe -c "import site; print(site.getsitepackages()[0])"') do set AKO_BUILD_SITE_PACKAGES=%%i
echo [INFO] site-packages: %AKO_BUILD_SITE_PACKAGES%

echo [INFO] Stopping running Ako instances (unlock dist folder)...
taskkill /IM Ako-ai.exe /F >nul 2>nul

echo [INFO] Cleaning old build...
if exist build       rmdir /s /q build
if exist dist\Ako-ai rmdir /s /q dist\Ako-ai

echo [INFO] Building...
.venv\Scripts\pyinstaller.exe --noconfirm --clean Ako-ai.spec > %LOG% 2>&1

echo [INFO] Copying config JSON next to exe (PyInstaller _internal fallback)...
if exist app_commands.json copy /Y app_commands.json dist\Ako-ai\app_commands.json >nul
if exist search_sites.json copy /Y search_sites.json dist\Ako-ai\search_sites.json >nul
if exist app_commands.json copy /Y app_commands.json dist\Ako-ai\_internal\app_commands.json >nul 2>nul
if exist search_sites.json copy /Y search_sites.json dist\Ako-ai\_internal\search_sites.json >nul 2>nul

echo.
if exist dist\Ako-ai\Ako-ai.exe (
    echo [OK] Build succeeded: dist\Ako-ai\Ako-ai.exe
) else (
    echo [FAIL] Build failed. See build_log.txt
    echo.
    type %LOG%
)

pause