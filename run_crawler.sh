#!/bin/bash
set -e

LOCK_FILE="/tmp/manga-crawler.lock"
ARGS=("$@")

for ((i = 0; i < ${#ARGS[@]}; i++)); do
  if [[ "${ARGS[$i]}" == "--chapter-id" && -n "${ARGS[$((i + 1))]:-}" ]]; then
    LOCK_FILE="/tmp/manga-crawler-chapter-${ARGS[$((i + 1))]}.lock"
    break
  fi

  if [[ "${ARGS[$i]}" == "--manga-id" && -n "${ARGS[$((i + 1))]:-}" ]]; then
    LOCK_FILE="/tmp/manga-crawler-title-${ARGS[$((i + 1))]}.lock"
    break
  fi
done
LOG_FILE="/home/opc/manga-crawler/crawler.log"

exec 9>"$LOCK_FILE"

if ! flock -n 9; then
  echo "$(date) - crawler already running" >> "$LOG_FILE"
  exit 1
fi

cd /home/opc/manga-crawler
source .venv/bin/activate
source .env

echo "$(date) - crawler started" >> "$LOG_FILE"
python mangarw.py "$@" >> "$LOG_FILE" 2>&1
echo "$(date) - crawler finished" >> "$LOG_FILE"
