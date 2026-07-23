#!/bin/sh
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
./venv/bin/python -c "import gdrive_toolkit" 2>/dev/null || ./venv/bin/pip install -q -e '.[dev]'
case "$1" in
  download|upload|hub) exec ./venv/bin/gdrive-"$1" ;;
  setup) exec ./venv/bin/gdrive-setup ;;
  *) echo "usage: ./run.sh download|upload|hub|setup" ;;
esac
