#!/usr/bin/env bash
# ─────────────────────────────────────────────────────
# Monitoring Pipeline — Ubuntu VPS Setup Script
# Run once after cloning the repo on your server
# Usage: chmod +x server-setup.sh && ./server-setup.sh
# ─────────────────────────────────────────────────────
set -euo pipefail

echo "=== Monitoring Pipeline — Server Setup ==="

# 1. System dependencies
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-venv python3-pip cron

# 2. Set timezone to America/New_York
echo "[2/6] Setting timezone to America/New_York..."
sudo timedatectl set-timezone America/New_York

# 3. Create virtual environment
echo "[3/6] Creating Python virtual environment..."
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 4. Verify .env exists
echo "[4/6] Checking .env..."
if [ ! -f .env ]; then
    echo "ERROR: .env file not found!"
    echo "Create it with:"
    echo "  ANTHROPIC_API_KEY=sk-ant-..."
    echo "  TELEGRAM_BOT_TOKEN=..."
    echo "  TELEGRAM_CHAT_ID=..."
    exit 1
fi
echo ".env found"

# 5. Create required directories for each subject
echo "[5/6] Creating directories..."
MINUTE_OFFSET=0
for subject_dir in subjects/*/; do
    subject=$(basename "$subject_dir")
    # Skip _template
    if [[ "$subject" == _* ]]; then
        continue
    fi
    # Only process directories that have subject.yaml
    if [[ ! -f "${subject_dir}subject.yaml" ]]; then
        continue
    fi
    mkdir -p "logs/${subject}" "logs/failed/${subject}" "reports/${subject}"
    echo "  Created dirs for subject: ${subject}"
done

# 6. Install cron jobs — one per subject, staggered by 30 minutes
echo "[6/6] Installing cron jobs..."
# Remove any existing monitoring-pipeline cron entries
EXISTING_CRON=$(crontab -l 2>/dev/null | grep -v "monitoring-pipeline\|# monitoring-pipeline" || true)

NEW_CRON="${EXISTING_CRON}"
MINUTE_OFFSET=0

for subject_dir in subjects/*/; do
    subject=$(basename "$subject_dir")
    if [[ "$subject" == _* ]]; then
        continue
    fi
    if [[ ! -f "${subject_dir}subject.yaml" ]]; then
        continue
    fi

    CRON_MINUTE=$(( MINUTE_OFFSET % 60 ))
    CRON_HOUR=$(( 8 + MINUTE_OFFSET / 60 ))
    CRON_CMD="${CRON_MINUTE} ${CRON_HOUR} * * * cd ${PROJECT_DIR} && ${PROJECT_DIR}/venv/bin/python src/main.py --subject ${subject} >> ${PROJECT_DIR}/logs/${subject}/cron.log 2>&1"

    NEW_CRON="${NEW_CRON}
# monitoring-pipeline: ${subject} daily ${CRON_HOUR}:$(printf '%02d' $CRON_MINUTE) AM Eastern
${CRON_CMD}"

    MINUTE_OFFSET=$(( MINUTE_OFFSET + 30 ))
    echo "  Cron for ${subject}: ${CRON_HOUR}:$(printf '%02d' $CRON_MINUTE) AM Eastern"
done

echo "${NEW_CRON}" | crontab -

echo ""
echo "=== Setup Complete ==="
echo "Project:  ${PROJECT_DIR}"
echo "Python:   $(python3 --version)"
echo ""
echo "Verify with:"
echo "  crontab -l"
echo "  source venv/bin/activate && python src/main.py --list-subjects"
echo "  python src/main.py --subject rezoning --dry-run"
echo ""
