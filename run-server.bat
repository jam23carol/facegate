@echo off
REM ==========================================================================
REM  Запуск СЕРВЕРА распознавания лиц (Windows)
REM ==========================================================================
REM    run-server.bat                     - панель: http://localhost:8080
REM    run-server.bat --web-port 9000     - любые аргументы server.py
REM    run-server.bat --help              - все параметры сервера
REM
REM  Скрипт сам находит виртуальное окружение (.venv), подхватывает .env
REM  и ограничивает потоки OpenMP/dlib (защита от зависания при параллельных
REM  вызовах распознавания из панели и из потока видео).
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
    echo !!! Python не найден. Установите Python 3.10+ и выполните install.bat
    exit /b 1
  )
  set "PY=python"
  echo >>> ВНИМАНИЕ: окружение %VENV_DIR% не найдено - запуск системным Python.
  echo     Установите зависимости: install.bat
)

if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%B"=="" set "%%A=%%B"
  )
)

REM Один поток OpenMP для dlib + ограничение torch (см. facegate/__init__.py)
if "%OMP_NUM_THREADS%"=="" set "OMP_NUM_THREADS=1"
if "%OPENBLAS_NUM_THREADS%"=="" set "OPENBLAS_NUM_THREADS=1"
if "%MKL_NUM_THREADS%"=="" set "MKL_NUM_THREADS=1"
if "%SILERO_THREADS%"=="" set "SILERO_THREADS=2"

echo ^>^>^> Запуск сервера: "%PY%"
"%PY%" -u server.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo !!! Сервер завершился с кодом %RC%. Диагностика: "%PY%" tools\check_env.py
)
endlocal & exit /b %RC%
