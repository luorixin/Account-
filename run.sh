#!/bin/bash
# Exit on error
set -e

# Use python3 if available, otherwise python
PYTHON="python3"
if ! command -v python3 &> /dev/null; then
    if command -v python &> /dev/null; then
        PYTHON="python"
    else
        echo "Error: Python is not installed or not in PATH."
        exit 1
    fi
fi

# Run the app server, forwarding all arguments (e.g. --port 8080)
exec "$PYTHON" -m app.server "$@"
