#!/usr/bin/env bash
# HellAPI — API + Telegram Bot ek saath start (Linux/VPS/Git Bash)
cd "$(dirname "$0")"

echo "[1/2] Starting API on port 8000..."
(./venv/Scripts/python -m uvicorn main:app --host 0.0.0.0 --port 8000 > api.log 2>&1 &)
# Linux pe: (python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 > api.log 2>&1 &)

sleep 5

echo "[2/2] Starting Telegram Bot..."
./venv/Scripts/python tgbot.py
