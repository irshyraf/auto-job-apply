#!/bin/bash
# run_daily.sh — runs the automated pipeline every morning
# Cron calls this at 8am. Logs go to data/pipeline.log.

cd "$(dirname "$0")"

LOG="data/pipeline.log"
echo "" >> "$LOG"
echo "========================================" >> "$LOG"
echo "  Run started: $(date)" >> "$LOG"
echo "========================================" >> "$LOG"

# Load .env so ANTHROPIC_API_KEY is available
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

python3 main.py --auto --skip-checks >> "$LOG" 2>&1

echo "  Run finished: $(date)" >> "$LOG"
