@echo off
chcp 65001 >nul
title PS5 Directo
cd /d "%~dp0"
if exist "%~dp0python\python.exe" (
  "%~dp0python\python.exe" app.py %*
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 app.py %*
  ) else (
    python app.py %*
  )
)
if %errorlevel%==3 exit /b 0
echo.
echo PS5 Directo se ha cerrado.
pause
