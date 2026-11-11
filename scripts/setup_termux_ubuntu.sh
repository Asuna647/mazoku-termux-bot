#!/usr/bin/env bash
# Run inside Termux Ubuntu (proot-distro login ubuntu)
set -euo pipefail

echo "[1/4] Updating apt packages and installing system dependencies..."
apt update && apt install -y python3-pip python3-venv tesseract-ocr curl tmux

echo "[2/4] Creating Python virtual environment at ~/mazoku-env..."
python3 -m venv ~/mazoku-env
source ~/mazoku-env/bin/activate

echo "[3/4] Installing Python libraries and Playwright ARM64 Chromium..."
pip install --upgrade pip
pip install playwright pillow pytesseract
playwright install --with-deps chromium

echo "[4/4] Creating ~/mazoku-bot workspace..."
mkdir -p ~/mazoku-bot
echo "Done. Transfer mazoku_bot.py, discord_state.json, and .env into ~/mazoku-bot and run:"
echo "  source ~/mazoku-env/bin/activate && cd ~/mazoku-bot && python3 mazoku_bot.py --headless"
