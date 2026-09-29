@echo off
REM ==========================================================================
REM  Запуск КЛИЕНТА (машина с камерой) — Windows
REM ==========================================================================
REM    run-client.bat --server-ip 192.168.1.10
REM    run-client.bat --list-cameras        - какие камеры доступны
REM    run-client.bat --check-audio         - проверка звука (тестовый сигнал)
REM    run-client.bat --no-debug --fps 5    - headless; любые аргументы client.py
REM
REM  Адрес сервера можно задать переменной окружения SERVER_HOST или в файле .env
REM  (SERVER_HOST=192.168.1.10). Звук играет встроенный в Python winsound,
REM  внешние плееры не требуются.
REM ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "VENV_DIR=.venv"
if not "%FACEGATE_VENV%"=="" set "VENV_DIR=%FACEGATE_VENV%"
set "PY="

if exist "%VENV_DIR%\Scripts\python.exe" (
  set "PY=%VENV_DIR%\Scripts\python.exe"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo !!! Python не найден. Установите Python 3.10+ и выполните install.bat --client
    exit /b 1
  )
  set "PY=python"
  echo >>> ВНИМАНИЕ: окружение %VENV_DIR% не найдено - запуск системным Python.
  echo     Установите зависимости клиента: install.bat --client
)

if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%B"=="" set "%%A=%%B"
  )
)

if "%SERVER_HOST%"=="" (set "SRV=127.0.0.1") else (set "SRV=%SERVER_HOST%")
echo ^>^>^> Запуск клиента: "%PY%" -^> сервер %SRV%
"%PY%" -u client.py %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
