#!/bin/sh
# Re-copies design/drivedeck.css into every vendored location. Run after
# editing the canonical file; the vendored copies must stay byte-identical.
set -eu
cd "$(dirname "$0")/.."

cp design/drivedeck.css toolkit/src/gdrive_toolkit/downloader/static/drivedeck.css
cp design/drivedeck.css toolkit/src/gdrive_toolkit/uploader/static/drivedeck.css
cp design/drivedeck.css drivecast/drivecast/static/drivedeck.css

echo "synced design/drivedeck.css -> 3 vendored copies"
