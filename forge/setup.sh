#!/bin/bash
# ── Forge Setup ───────────────────────────────────────────────────────────────
set -e

echo ""
echo "🔥  Forge — Mobile AI Coder"
echo "    Setting up your environment…"
echo "────────────────────────────────────"

# 1. Python check
if ! command -v python3 &>/dev/null; then echo "❌  Python 3 required"; exit 1; fi
PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "✅  Python $PYVER"

# 2. Create venv
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  echo "✅  Virtual environment created"
fi
source .venv/bin/activate

# 3. Install deps
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "✅  Dependencies installed"

# 4. .env setup
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "✅  .env created from template"
  echo ""
  echo "⚠️   ACTION REQUIRED:"
  echo "    Open .env and set your TOGETHER_API_KEY"
  echo "    Get one free at https://api.together.xyz"
else
  echo "✅  .env already exists"
fi

# 5. Create dirs
mkdir -p models data/generated logs
echo "✅  Directories ready"

# 6. Install forge package
pip install -q -e .
echo "✅  Forge package installed"

echo ""
echo "────────────────────────────────────"
echo "🚀  Ready! Next steps:"
echo ""
echo "    1. Set TOGETHER_API_KEY in .env"
echo "    2. Start the server:"
echo "       source .venv/bin/activate"
echo "       python -m forge.server.app"
echo ""
echo "    3. Open http://localhost:8000"
echo ""
echo "    To generate training data (later):"
echo "       python -m forge.data.generate --n 1000"
echo ""
echo "    To fine-tune Phi-3.5 Mini (Colab/RunPod):"
echo "       python forge/training/train.py"
echo ""
