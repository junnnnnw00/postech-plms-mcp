#!/usr/bin/env bash
set -euo pipefail

# Installs only this checked-out project; no shell profile or client config edits.
project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
python_bin="${PYTHON:-python3}"
"$python_bin" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'
"$python_bin" -m venv "$project_dir/.venv"
"$project_dir/.venv/bin/python" -m pip install --upgrade 'pip>=26.2.1' 'setuptools>=83.0.0'
"$project_dir/.venv/bin/python" -m pip install "$project_dir"
printf 'Installed. Sign in with:\n  %s login\n' "$project_dir/.venv/bin/plms-mcp"
printf 'Then check:\n  %s doctor\n' "$project_dir/.venv/bin/plms-mcp"
