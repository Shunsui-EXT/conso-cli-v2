#!/usr/bin/env bash
# Download + unpack the Conso extension CRX for local analysis.
# The unpacked tree is gitignored (third-party, copyrighted) — this script
# reproduces it from the public Chrome Web Store.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/extension_original"
EXT_ID="bjibbmkefnaamkenamdppfengeepadpi"
mkdir -p "$DEST"

CRX="$DEST/conso.crx"
URL="https://clients2.google.com/service/update2/crx?response=redirect&os=linux&arch=x64&os_arch=x86_64&nacl_arch=x86-64&prod=chromiumcrx&prodchannel=unknown&prodversion=131.0.0.0&acceptformat=crx2,crx3&x=id%3D${EXT_ID}%26uc"

echo "[fetch] downloading CRX..."
curl -sSL "$URL" -o "$CRX"
echo "[fetch] $(du -h "$CRX" | cut -f1) -> $CRX"

echo "[fetch] stripping CRX3 header..."
python3 - "$CRX" "$DEST/conso.zip" <<'PY'
import struct, sys
data = open(sys.argv[1], "rb").read()
assert data[:4] == b"Cr24", "not a CRX file"
version = struct.unpack("<I", data[4:8])[0]
if version == 3:
    header = struct.unpack("<I", data[8:12])[0]
    start = 12 + header
else:
    pub = struct.unpack("<I", data[8:12])[0]
    sig = struct.unpack("<I", data[12:16])[0]
    start = 16 + pub + sig
open(sys.argv[2], "wb").write(data[start:])
print(f"[fetch] crx v{version}, zip from offset {start}")
PY

echo "[fetch] unpacking..."
unzip -o -q "$DEST/conso.zip" -d "$DEST/unpacked"
echo "[fetch] done -> $DEST/unpacked"
