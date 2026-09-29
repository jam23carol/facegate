@echo off
REM ==========================================================================
REM  Установка зависимостей (Windows) — ВИРТУАЛЬНОЕ ОКРУЖЕНИЕ создаётся всегда
REM ==========================================================================
REM    install.bat                - авто: venv .venv + dlib-bin (иначе сборка)
REM    install.bat --prebuilt     - только предкомпилированный dlib-bin
REM    install.bat --source       - только сборка dlib из исходников
REM                                   (нужны VS Build Tools + CMake)
REM    install.bat --client       - только клиент: opencv + pyzmq + numpy
REM    install.bat --tts          - дополнительно Silero TTS (torch CPU)
REM    install.bat --venv PATH    - другое имя/путь окружения (default: .venv)
REM    install.bat --no-venv      - ставить в системный Python (не рекомендуется)
REM
REM  Запуск после установки:
REM      run-server.bat                 - сервер + админ-панель http://localhost:8080
REM      run-client.bat --server-ip IP  - клиент на машине с камерой
REM
REM  Озвучка в Windows работает через SAPI5 (pyttsx3) — ничего доп. ставить не
REM  нужно. Русские голоса SAPI5: «Microsoft Irina» и т.п. — выбираются в
REM  настройках панели (параметр «Голос TTS», например: Irina или ru).
REM  Звук на клиенте играет встроенный в Python winsound (внешние плееры не
REM  нужны); при желании можно указать --audio-player "C:\...\mpv.exe".
REM ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PY=python"
if not "%PYTHON%"=="" set "PY=%PYTHON%"
set "MODE=auto"
set "VENV_DIR=.venv"
set "USE_VENV=1"
set "WITH_TTS=0"
set "CLIENT_ONLY=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--prebuilt" ( set "MODE=prebuilt" & shift & goto parse_args )
if /I "%~1"=="--source"   ( set "MODE=source"   & shift & goto parse_args )
if /I "%~1"=="--client"   ( set "CLIENT_ONLY=1" & shift & goto parse_args )
if /I "%~1"=="--tts"      ( set "WITH_TTS=1"    & shift & goto parse_args )
if /I "%~1"=="--no-venv"  ( set "USE_VENV=0"    & shift & goto parse_args )
if /I "%~1"=="--system"   ( set "USE_VENV=0"    & shift & goto parse_args )
if /I "%~1"=="--venv"     ( set "VENV_DIR=%~2"  & shift & shift & goto parse_args )
if /I "%~1"=="-h"         ( goto usage )
if /I "%~1"=="--help"     ( goto usage )
echo Неизвестный аргумент: %~1
exit /b 2

:usage
echo.
echo  install.bat [--prebuilt^|--source] [--client] [--tts] [--venv PATH] [--no-venv]
echo.
exit /b 0

:args_done
echo === Интерпретатор ===
%PY% -V
if errorlevel 1 (
  echo.
  echo !!! Python не найден. Установите Python 3.10+ с https://python.org
  echo     и при установке отметьте "Add python.exe to PATH".
  exit /b 1
)
%PY% -c "import sys; print(sys.executable)"

REM ---------------------------------------------------- виртуальное окружение
if "%USE_VENV%"=="0" goto no_venv
if exist "%VENV_DIR%\Scripts\python.exe" goto venv_ready
echo.
echo === Создаю виртуальное окружение %VENV_DIR% ===
%PY% -m venv "%VENV_DIR%"
if errorlevel 1 (
  echo.
  echo !!! Не удалось создать виртуальное окружение.
  echo     Проверьте, что установлен компонент venv: %PY% -m ensurepip --upgrade
  echo     Либо ставьте в системный Python: install.bat --no-venv
  exit /b 1
)
:venv_ready
set "PY=%VENV_DIR%\Scripts\python.exe"
echo Окружение: %PY%
goto pip_upgrade

:no_venv
echo.
echo === venv отключен: установка в системный Python ===

:pip_upgrade
echo.
echo === Шаг 1/4. pip и setuptools^<70 ===
"%PY%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
"%PY%" -m pip install --force-reinstall "setuptools<70" wheel
if errorlevel 1 exit /b 1

if "%CLIENT_ONLY%"=="1" goto client_only

set "OK=0"
if /I "%MODE%"=="source" goto source

echo.
echo === Шаг 2/4. Предкомпилированный dlib-bin ===
"%PY%" -m pip install -r requirements-prebuilt.txt
if errorlevel 1 goto fallback
"%PY%" -m pip install --no-deps "face_recognition>=1.3.0"
if errorlevel 1 goto fallback
set "OK=1"
goto tts

:fallback
if /I "%MODE%"=="prebuilt" (
  echo !!! Установка dlib-bin не удалась
  exit /b 1
)
:source
echo.
echo === Шаг 2/4. dlib из исходников ^(Visual Studio Build Tools + CMake^) ===
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo !!! Сборка dlib не удалась
  exit /b 1
)
set "OK=1"
goto tts

:client_only
echo.
echo === Шаг 2/4. Клиент: opencv + pyzmq + numpy ^(без dlib^) ===
"%PY%" -m pip install "numpy>=1.24,<3" "opencv-python>=4.5.0" "pyzmq>=22.0.0"
if errorlevel 1 (
  echo !!! Установка зависимостей клиента не удалась
  exit /b 1
)
goto check

:tts
echo.
echo === Шаг 3/4. Озвучка Silero TTS ^(нейросетевой русский голос^) ===
if "%WITH_TTS%"=="1" (
  "%PY%" -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio
  "%PY%" -m pip install "silero>=0.5" scipy
  echo     Silero TTS установлен. В настройках панели выберите движок "silero".
) else (
  echo     Пропущено. По умолчанию в Windows используется SAPI5 через pyttsx3.
  echo     Поставить Silero: install.bat --tts
)

:check
echo.
echo === Шаг 4/4. Проверка окружения ===
if "%CLIENT_ONLY%"=="1" (
  "%PY%" tools\check_env.py --client
) else (
  "%PY%" tools\check_env.py
)
if errorlevel 1 (
  echo.
  echo !!! Проверка не прошла - см. подсказки выше.
  exit /b 1
)

echo.
echo Готово. Запуск:
if "%CLIENT_ONLY%"=="1" (
  echo   клиент:  run-client.bat --server-ip ^<IP сервера^>
) else (
  echo   сервер:  run-server.bat            - админ-панель http://localhost:8080
  echo   клиент:  run-client.bat --server-ip ^<IP сервера^>
  echo   тесты:   "%PY%" -m pytest
)
endlocal
