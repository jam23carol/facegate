#!/usr/bin/env bash
# ==========================================================================
#  Запуск СЕРВЕРА распознавания лиц (Linux/macOS)
# ==========================================================================
#  ./run-server.sh                      # панель: http://localhost:8080
#  ./run-server.sh --web-port 9000      # любые аргументы server.py
#  ./run-server.sh --help               # все параметры сервера
#  FACEGATE_VENV=/path/to/venv ./run-server.sh   # другое окружение
#
#  Скрипт сам находит виртуальное окружение (созданное ./install.sh),
#  подхватывает переменные из .env (если файл есть) и запускает server.py.
# ==========================================================================
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR="${FACEGATE_VENV:-.venv}"
PY=""

if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "${VIRTUAL_ENV}/bin/python" ]; then
  PY="${VIRTUAL_ENV}/bin/python"
elif [ -x "${VENV_DIR}/bin/python" ]; then
  PY="${VENV_DIR}/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
  echo ">>> ВНИМАНИЕ: окружение ${VENV_DIR} не найдено — запуск системным python3."
  echo "    Установите зависимости: ./install.sh"
else
  echo "!!! Не найден python. Установите python3 и выполните ./install.sh" >&2
  exit 1
fi

# .env — те же переменные, что использует docker-compose (SERVER_HOST, PUID, ...)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env || echo ">>> Не удалось разобрать .env — запускаю без него"
  set +a
fi

# Один поток OpenMP для dlib: защита от дедлока параллельного рантайма
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
# Синтез озвучки не должен съедать все ядра (поток распознавания и ZeroMQ)
export SILERO_THREADS="${SILERO_THREADS:-2}"

echo ">>> $($PY -V) · $($PY -c 'import sys;print(sys.executable)')"
exec "$PY" -u server.py "$@"
