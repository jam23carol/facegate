#!/usr/bin/env bash
# ==========================================================================
#  Запуск КЛИЕНТА (машина с камерой) — Linux/macOS
# ==========================================================================
#  ./run-client.sh --server-ip 192.168.1.10
#  ./run-client.sh --list-cameras            # какие камеры доступны
#  ./run-client.sh --check-audio             # проверка звука (тестовый сигнал)
#  ./run-client.sh --no-debug --fps 5        # headless, любые аргументы client.py
#
#  Адрес сервера можно задать и переменной окружения:
#      SERVER_HOST=192.168.1.10 ./run-client.sh
#  Скрипт сам находит виртуальное окружение (./install.sh [--client])
#  и подхватывает .env, если он есть.
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
  echo "    Установите зависимости клиента: ./install.sh --client"
else
  echo "!!! Не найден python. Установите python3 и выполните ./install.sh --client" >&2
  exit 1
fi

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env || echo ">>> Не удалось разобрать .env — запускаю без него"
  set +a
fi

echo ">>> $($PY -V) · сервер: ${SERVER_HOST:-127.0.0.1}:${SERVER_VIDEO_PORT:-5555}"
exec "$PY" -u client.py "$@"
