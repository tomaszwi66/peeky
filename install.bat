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

echo [2/3] Pulling models...
echo       - gemma4:e4b  ^(multimodal vision model, ~3 GB^)
ollama pull gemma4:e4b
if errorlevel 1 (
    echo.
    echo Model pull failed. Check your internet connection and try again.
    pause
    exit /b 1
)
echo.
echo       - nomic-embed-text  ^(embedding model for document RAG, ~270 MB^)
ollama pull nomic-embed-text
if errorlevel 1 (
    echo   Warning: embedding model could not be pulled. Document RAG will use
    echo   keyword matching as a fallback. You can pull it later with:
    echo     ollama pull nomic-embed-text
)
echo.

echo [3/3] Creating desktop shortcut...
powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell; ^
   $s = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Peeky.lnk'); ^
   $s.TargetPath = '%~dp0run.bat'; ^
   $s.WorkingDirectory = '%~dp0'; ^
   $s.IconLocation = '%~dp0peeky_icon.ico'; ^
   $s.Description = 'Peeky - AI desktop sidekick'; ^
   $s.Save()"
if errorlevel 1 (
    echo   Warning: could not create desktop shortcut. Run run.bat manually.
) else (
    echo   Shortcut created on the desktop.
)
echo.

echo Done. Double-click Peeky on your desktop to launch.
echo.
pause
endlocal
