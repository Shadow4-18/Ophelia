#!/data/data/com.termux/files/usr/bin/bash
# Ophelia on Termux (S21 / non-root). Run from project root after git clone.
set -euo pipefail

pkg update -y
pkg install -y python git tmux termux-api

pip install --upgrade pip
pip install -e .

mkdir -p ~/.ophelia
if [[ ! -f ~/.ophelia/.env ]]; then
  cp config.example.env ~/.ophelia/.env
  echo "Edit ~/.ophelia/.env with your tokens."
fi

echo ""
echo "Next steps:"
echo "  1. termux-wake-lock   # keep agent alive when screen off"
echo "  2. grok login OR set XAI_API_KEY in ~/.ophelia/.env"
echo "  3. ophelia auth import-grok   # if using grok CLI"
echo "  4. ophelia doctor"
echo "  5. tmux new -s ophelia && ophelia run"
echo ""
echo "Optional persistence: install Termux:Boot + termux-services (see README)."
