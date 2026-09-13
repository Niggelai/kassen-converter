@echo off
setlocal
cd /d "%~dp0"
python -m PyInstaller --clean --onefile --name KassenConverter kassen_converter.py
if errorlevel 1 (
  echo.
  echo Build fehlgeschlagen.
  pause
  exit /b 1
)
echo.
echo Fertig: dist\KassenConverter.exe
pause
