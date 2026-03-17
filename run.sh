#!/bin/bash
# run.sh — Launch the Job Engine Streamlit app
# Double-click this file (or run: bash run.sh) to start

cd "$(dirname "$0")"

# Check Python and Streamlit are available
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.12+ and try again."
    exit 1
fi

if ! python3 -c "import streamlit" &>/dev/null; then
    echo "Installing dependencies..."
    pip3 install -r requirements.txt
fi

echo "Starting Job Engine..."
echo "Opening http://localhost:8501"

# Open browser after a short delay
(sleep 2 && open "http://localhost:8501") &

streamlit run app.py \
    --server.headless true \
    --server.port 8501 \
    --browser.gatherUsageStats false
