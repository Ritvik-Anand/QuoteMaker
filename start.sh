#!/bin/bash
cd "$(dirname "$0")"

# Load .env if present
if [ -f ".env" ]; then
  export $(grep -v '^#' .env | xargs)
fi

# Create venv if needed
if [ ! -d ".venv" ]; then
  echo "Setting up environment (first time only)..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

# Check if DEEPSEEK_API_KEY is set
if [ -z "$DEEPSEEK_API_KEY" ]; then
  echo ""
  echo "⚠️  DEEPSEEK_API_KEY is not set."
  echo "   PDF catalog parsing requires a DeepSeek API key."
  echo "   Set it with: export DEEPSEEK_API_KEY=your-key-here"
  echo "   (You can still use the app — just set the key before uploading catalogs)"
  echo ""
fi

echo "Starting Bagula Mukhi Quotation Maker..."
echo "Open: http://localhost:5050"
echo ""
.venv/bin/python app.py
