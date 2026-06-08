#!/usr/bin/env bash
# Ophelia Project — PC install (macOS / Linux). Run from repo root.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo ""
echo "=== Ophelia Project — install ==="
echo ""

echo "[1/3] Installing Python package..."
pip install -e .
pip install "uvicorn[standard]>=0.32"

echo "[2/3] Auto-setup (~/.ophelia, .env)..."
ophelia setup --do

echo "[3/3] Full setup guide..."
ophelia setup

echo ""
echo "Done. Next: ophelia check --chat-only"
