@echo off
REM One-click setup for Peeky on Windows.
REM Requires Python 3.10+ and Ollama already installed and running.

setlocal

echo.
echo ====================================================
echo   Peeky setup
echo ====================================================
echo.

REM ---- Check Python ----------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo Python is not on PATH.
    echo Install it from https://www.python.org/downloads/  ^(check "Add to PATH"^)
    echo Then re-run this script.
    pause
    exit /b 1
)

echo [1/3] Installing Python dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency install failed. See messages above.
    pause
    exit /b 1
)
echo.

REM ---- Check Ollama ----------------------------------------------------
where ollama >nul 2>nul
if errorlevel 1 (
    echo Ollama was not found.
    echo Install it from https://ollama.com  then re-run this script.
    pause
    exit /b 1
)

echo [2/3] Pulling the multimodal model ^(about 3 GB, this may take a while^)...
ollama pull gemma3n:e4b
if errorlevel 1 (
    echo.
    echo Model pull failed. Check your internet connection and try again.
    pause
    exit /b 1
)
echo.

echo [3/3] Done.
echo.
echo Launch Peeky with: run.bat
echo.
pause
endlocal
