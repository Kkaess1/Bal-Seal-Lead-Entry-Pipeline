#!/bin/bash
# ============================================================================
# Lead Input Pipeline - Linux Crontab Setup
# 
# Schedules:
#   1. Lead Ingestion  (pipeline_ingest.py) -> 8:00 AM & 4:00 PM daily
#   2. Oracle Submit   (pipeline_submit.py) -> 5:00 PM daily
# ============================================================================

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PYTHON_EXE="$SCRIPT_DIR/.venv/bin/python"

if [ ! -f "$PYTHON_EXE" ]; then
    PYTHON_EXE="python3"
fi

CRON_AM="0 8 * * * cd $SCRIPT_DIR && $PYTHON_EXE pipeline_ingest.py >> logs/cron_ingest.log 2>&1"
CRON_PM="0 16 * * * cd $SCRIPT_DIR && $PYTHON_EXE pipeline_ingest.py >> logs/cron_ingest.log 2>&1"
CRON_SUBMIT="0 17 * * * cd $SCRIPT_DIR && $PYTHON_EXE pipeline_submit.py >> logs/cron_submit.log 2>&1"

# Append to crontab if not already present
( crontab -l 2>/dev/null | grep -v "pipeline_ingest.py" | grep -v "pipeline_submit.py" ; echo "$CRON_AM" ; echo "$CRON_PM" ; echo "$CRON_SUBMIT" ) | crontab -

echo "============================================================================"
echo "Crontab scheduled tasks registered successfully!"
echo "  - Ingest: 8:00 AM & 4:00 PM Daily"
echo "  - Oracle Submit: 5:00 PM Daily"
echo "============================================================================"
