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

echo [INFO] Getting site-packages path...
for /f "delims=" %%i in ('.venv\Scripts\python.exe -c "import site; print(site.getsitepackages()[0])"') do set AKO_BUILD_SITE_PACKAGES=%%i
echo [INFO] site-packages: %AKO_BUILD_SITE_PACKAGES%

echo [INFO] Cleaning old build...
if exist build       rmdir /s /q build
if exist dist\Ako-ai rmdir /s /q dist\Ako-ai

echo [INFO] Building...
.venv\Scripts\pyinstaller.exe --noconfirm --clean Ako-ai.spec > %LOG% 2>&1

echo.
if exist dist\Ako-ai\Ako-ai.exe (
    echo [OK] Build succeeded: dist\Ako-ai\Ako-ai.exe
) else (
    echo [FAIL] Build failed. See build_log.txt
    echo.
    type %LOG%
)

pause