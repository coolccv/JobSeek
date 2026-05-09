#!/usr/bin/env bash
# Clone the JobSeek repo and run the APS Jobs scraper.
# Run this in Git Bash on Windows.

set -e

REPO_URL="https://github.com/coolccv/JobSeek.git"
BRANCH="claude/review-job-listings-PvzYo"
DIR="JobSeek"

# --- Clone (skip if already cloned) ---
if [ ! -d "$DIR" ]; then
    echo "Cloning $REPO_URL ..."
    git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$DIR"
else
    echo "Directory '$DIR' already exists — pulling latest ..."
    git -C "$DIR" fetch origin "$BRANCH"
    git -C "$DIR" checkout "$BRANCH"
    git -C "$DIR" pull origin "$BRANCH"
fi

cd "$DIR"

# --- Python check ---
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo "ERROR: Python not found. Install Python 3 from https://python.org" >&2
    exit 1
fi

PYTHON=$(command -v python3 || command -v python)
echo "Using Python: $PYTHON ($($PYTHON --version))"

# --- Install dependencies ---
echo "Installing dependencies ..."
$PYTHON -m pip install --quiet -r requirements.txt

# --- Install Playwright browser ---
echo "Installing Playwright Chromium browser ..."
$PYTHON -m playwright install chromium

# --- Run ---
echo ""
echo "Running scraper ..."
$PYTHON scraper.py "$@"
