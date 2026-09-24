#!/usr/bin/env bash
# Run the portal against mock robots and a mock RMF api -- no ROS required.
# Good for UI work; see README for running with real Open-RMF.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 tools/mock_robot_server.py --port 7001 &
ROBOTS=$!
python3 tools/mock_rmf_api.py --port 8000 --robots-url http://127.0.0.1:7001 &
RMF=$!
trap 'kill $ROBOTS $RMF 2>/dev/null' EXIT

sleep 1
echo "Portal: http://127.0.0.1:8080"
python3 portal/server.py
