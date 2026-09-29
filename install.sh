#!/usr/bin/env bash
# ==========================================================================
#  Установка зависимостей сервера/клиента распознавания лиц (Linux/macOS)
# ==========================================================================
#  ./install.sh                 — авто: виртуальное окружение .venv +
#                                 сначала предкомпилированный dlib-bin,
#                                 при неудаче сборка dlib из исходников
#  ./install.sh --prebuilt      — только dlib-bin (быстро, без cmake/компилятора)
#  ./install.sh --source        — только сборка dlib из исходников
#  ./install.sh --client        — только клиент (opencv+pyzmq+numpy, БЕЗ dlib)
#  ./install.sh --tts           — дополнительно: нейросетевая русская озвучка
#                                 Silero TTS (torch CPU + пакет silero + модель)
#  ./install.sh --venv PATH     — имя/путь виртуального окружения (default: .venv)
#  ./install.sh --system        — НЕ создавать venv (ставить в системный python)
#  PYTHON=python3.12 ./install.sh — выбрать конкретный интерпретатор
#
#  ВИРТУАЛЬНОЕ ОКРУЖЕНИЕ создаётся ВСЕГДА (кроме --system): на Ubuntu 24.04+,
#  Debian 12+ и новых Fedora действует PEP 668 (EXTERNALLY-MANAGED) — pip
#  отказывается ставить пакеты в системный Python с ошибкой
#  «error: externally-managed-environment». venv решает это штатно.
#
#  Запуск после установки:
#      ./run-server.sh                 # сервер + админ-панель http://localhost:8080
#      ./run-client.sh --server-ip IP  # клиент на машине с камерой
#
#  В Docker ничего этого не нужно: образ сервера
#  (docker/Dockerfile.server) уже содержит dlib-bin и Silero TTS.
# ==========================================================================
set -euo pipefail

cd "$(dirname "$0")"

MODE="auto"
VENV_DIR="${FACEGATE_VENV:-.venv}"
USE_VENV=1
WITH_TTS=0
CLIENT_ONLY=0
PYTHON_BIN="${PYTHON:-python3}"

while [ $# -gt 0 ]; do
  case "$1" in
    --prebuilt) MODE="prebuilt"; shift ;;
    --source)   MODE="source"; shift ;;
    --client)   CLIENT_ONLY=1; shift ;;
    --tts)      WITH_TTS=1; shift ;;
    --venv)     VENV_DIR="${2:-.venv}"; USE_VENV=1; shift 2 ;;
    --system|--no-venv) USE_VENV=0; shift ;;
    --python)   PYTHON_BIN="${2:-python3}"; shift 2 ;;
    -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "Неизвестный аргумент: $1 (см. ./install.sh --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
fail() { printf '!!! %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- интерпретатор
command -v "$PYTHON_BIN" >/dev/null 2>&1 || \
  fail "Не найден интерпретатор '$PYTHON_BIN'. Установите python3 (Ubuntu/Debian: \
sudo apt install python3 python3-venv python3-pip; Fedora: sudo dnf install python3; \
macOS: brew install python)"

say ">>> Интерпретатор: $("$PYTHON_BIN" -V 2>&1) — $("$PYTHON_BIN" -c 'import sys;print(sys.executable)')"

# ------------------------------------------------------- виртуальное окружение
if [ "$USE_VENV" = "1" ]; then
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    say ">>> Создаю виртуальное окружение $VENV_DIR"
    if ! "$PYTHON_BIN" -m venv "$VENV_DIR" 2>/tmp/venv_err_$$; then
      cat /tmp/venv_err_$$ >&2 || true
      rm -f /tmp/venv_err_$$
      fail "Не удалось создать venv. Обычно не хватает пакета python3-venv:
       Ubuntu/Debian: sudo apt install python3-venv python3-pip
       Fedora:        sudo dnf install python3-virtualenv
       macOS:         brew install python
     Либо установите зависимости в системный python: ./install.sh --system"
    fi
    rm -f /tmp/venv_err_$$
  else
    say ">>> Виртуальное окружение уже существует: $VENV_DIR"
  fi
  # shellcheck disable=SC1091
  . "$VENV_DIR/bin/activate"
  PYTHON_BIN="python"
  say ">>> Окружение активировано: $VENV_DIR ($("$PYTHON_BIN" -V 2>&1))"
else
  say ">>> venv отключён (--system): ставлю в $("$PYTHON_BIN" -c 'import sys;print(sys.prefix)')"
  if [ -f "$("$PYTHON_BIN" -c 'import sysconfig,os;print(os.path.join(sysconfig.get_paths()["stdlib"],"EXTERNALLY-MANAGED"))')" ]; then
    say "    Обнаружен маркер PEP 668 (EXTERNALLY-MANAGED) — pip может отказать."
    say "    Рекомендуется: ./install.sh   (с виртуальным окружением)"
    PIP_SYS_FLAG="--break-system-packages"
  fi
fi
PIP_SYS_FLAG="${PIP_SYS_FLAG:-}"

pip() { "$PYTHON_BIN" -m pip ${PIP_SYS_FLAG:+$PIP_SYS_FLAG} "$@"; }

say
say "=== Шаг 1/5. pip и setuptools<70 (обязательно для сборки dlib) ==="
pip install --upgrade pip
pip install --force-reinstall "setuptools<70" wheel

# ---------------------------------------------------------------- зависимости
install_client() {
  say
  echo "=== Шаг 2/5. Клиент: opencv + pyzmq + numpy (без dlib) ==="
  pip install "numpy>=1.24,<3" "opencv-python>=4.5.0" "pyzmq>=22.0.0" || return 1
  return 0
}

install_prebuilt() {
  say
  echo "=== Шаг 2/5. Предкомпилированный dlib-bin ==="
  pip install -r requirements-prebuilt.txt || return 1
  pip install --no-deps "face_recognition>=1.3.0" || return 1
  return 0
}

install_source() {
  say
  echo "=== Шаг 2/5. dlib из исходников (нужны cmake и C++-компилятор) ==="
  if ! command -v cmake >/dev/null 2>&1; then
    echo "    cmake не найден. Ubuntu/Debian: sudo apt install build-essential cmake"
    echo "    macOS: brew install cmake | Fedora: sudo dnf install cmake gcc-c++"
  fi
  pip install -r requirements.txt || return 1
  return 0
}

OK=0
if [ "$CLIENT_ONLY" = "1" ]; then
  install_client || fail "Установка зависимостей клиента не удалась"
  OK=1
else
  case "$MODE" in
    prebuilt) install_prebuilt || fail "Установка dlib-bin не удалась" ; OK=1 ;;
    source)   install_source   || fail "Сборка dlib не удалась" ; OK=1 ;;
    auto)
      if install_prebuilt; then OK=1; else
        say ">>> dlib-bin не подошёл — пробую собрать dlib из исходников"
        install_source && OK=1 || true
      fi
      ;;
  esac
fi
[ "$OK" = "1" ] || fail "Установка зависимостей не удалась"

# ------------------------------------------------------------------- озвучка
say
echo "=== Шаг 3/5. Озвучка: Silero TTS (нейросетевой русский голос) ==="
if [ "$CLIENT_ONLY" = "1" ]; then
  echo "    Пропущено: озвучка синтезируется на сервере, клиент её только играет."
elif [ "$WITH_TTS" = "1" ]; then
  pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio
  pip install "silero>=0.5" scipy
  "$PYTHON_BIN" - <<'PY'
from silero import silero_tts
model, _ = silero_tts(language="ru", speaker="v5_ru")
model.save_wav(text="Озвучка готова к работе.", speaker="baya",
               audio_path="/tmp/silero_warmup.wav", sample_rate=48000)
print("    OK: модель Silero (v5_ru) загружена и закэширована")
PY
else
  echo "    Пропущено (установка на хосте опциональна; в Docker Silero уже есть)."
  echo "    Поставить:  ./install.sh --tts   — и в настройках панели выбрать движок silero."
fi

say
echo "=== Шаг 4/5. Системный TTS (запасной движок espeak-ng) ==="
if command -v espeak-ng >/dev/null 2>&1 || command -v espeak >/dev/null 2>&1; then
  echo "    OK: espeak найден (запасной движок озвучки)"
else
  echo "    Не найден espeak-ng — запасной движок озвучки будет недоступен."
  echo "    Ubuntu/Debian: sudo apt install espeak-ng    Fedora: sudo dnf install espeak-ng"
  echo "    macOS: brew install espeak-ng                Windows: используется SAPI5"
fi

# ------------------------------------------------------------------- проверка
say
echo "=== Шаг 5/5. Проверка окружения ==="
if [ "$CLIENT_ONLY" = "1" ]; then
  "$PYTHON_BIN" tools/check_env.py --client || fail "Проверка не прошла — см. подсказки выше."
else
  "$PYTHON_BIN" tools/check_env.py || fail "Проверка не прошла — см. подсказки выше."
fi

say
echo "Готово. Запуск (скрипты сами активируют $VENV_DIR):"
if [ "$CLIENT_ONLY" = "1" ]; then
  echo "  клиент:  ./run-client.sh --server-ip <IP сервера>"
else
  echo "  сервер:  ./run-server.sh                → админ-панель http://localhost:8080"
  echo "  клиент:  ./run-client.sh --server-ip <IP сервера>"
  echo "  docker:  docker compose up -d server    (озвучка Silero — внутри контейнера)"
  echo "  тесты:   $VENV_DIR/bin/python -m pytest"
fi
