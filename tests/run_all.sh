#!/bin/sh
# Run the controller's assertion suites.
#
# The suites locate `server.py`, `adapters.py` and `fake_strip.py` relative to
# their own directory (HERE = dirname(__file__)), so they only run when all five
# files sit in the same folder. That is how they were written, against a flat
# directory on Karim's box.
#
# Splitting them here - runtime in tonly_mttl_w01/rootfs/, tests in tests/ - is
# deliberate: the add-on payload must not carry test code into the image. So this
# script assembles a scratch directory, runs everything there, and cleans up.
#
# The suite files themselves are left byte-identical to the originals. Editing
# the tests to suit this layout would quietly weaken the only thing standing
# between this controller and a regression like the one that made three of four
# outlets drive the wrong socket.
#
# Summaries are parsed by a helper in python rather than by grep pipelines.
# Under `set -e`, a `p=$(... | grep ... | grep ...)` chain aborts the whole
# script the moment any stage matches nothing - which happens on a perfectly
# healthy suite whose output format shifts. A test runner that dies on a clean
# run is worse than no runner.
#
# Usage:  sh tests/run_all.sh
set -eu

TESTS_DIR=$(cd "$(dirname "$0")" && pwd)
ADDON_DIR=$(dirname "$TESTS_DIR")
RUNTIME_DIR="$ADDON_DIR/tonly_mttl_w01/rootfs"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/tonly-mttl-tests.XXXXXX")
OUTDIR="$WORK/.out"
mkdir -p "$OUTDIR"
trap 'rm -rf "$WORK"' EXIT INT TERM

for f in server.py adapters.py; do
  cp "$RUNTIME_DIR/$f" "$WORK/$f"
done
for f in api_test.py two_strip_test.py probe_test.py protect_test.py fake_strip.py; do
  cp "$TESTS_DIR/$f" "$WORK/$f"
done

echo "scratch dir: $WORK"
echo

SUITES="api_test two_strip_test probe_test protect_test"
CONTRACT="test_addon_contract"

for suite in $SUITES; do
  printf '%-24s ' "$suite.py"
  rc=0
  ( cd "$WORK" && python3 "$suite.py" ) >"$OUTDIR/$suite.txt" 2>&1 || rc=$?
  tail -n 1 "$OUTDIR/$suite.txt" | sed "s/^/exit=$rc  /"
  echo "exit=$rc" >>"$OUTDIR/$suite.rc"
done

printf '%-24s ' "$CONTRACT.py"
rc=0
( cd "$TESTS_DIR" && python3 "$CONTRACT.py" ) >"$OUTDIR/$CONTRACT.txt" 2>&1 || rc=$?
tail -n 1 "$OUTDIR/$CONTRACT.txt"
echo "exit=$rc" >>"$OUTDIR/$CONTRACT.rc"

echo "------------------------------------------------------------"

python3 - "$OUTDIR" <<'PY'
import re
import sys
from pathlib import Path

outdir = Path(sys.argv[1])
total_pass = total_fail = 0
bad = []

for txt in sorted(outdir.glob("*.txt")):
    name = txt.stem
    rc_file = outdir / f"{name}.rc"
    rc = 0
    if rc_file.exists():
        m = re.search(r"exit=(\d+)", rc_file.read_text())
        rc = int(m.group(1)) if m else 0

    text = txt.read_text(errors="replace")
    m = re.findall(r"^(\d+) passed, (\d+) failed", text, re.M)
    if m:
        p, f = m[-1]
        total_pass += int(p)
        total_fail += int(f)
    else:
        total_fail += 1
        bad.append(f"{name}: no 'N passed, M failed' summary (exit={rc})")

    if rc != 0:
        bad.append(f"{name}: exited {rc}")

print(f"TOTAL: {total_pass} passed, {total_fail} failed")
if bad:
    print("\nproblems:")
    for b in bad:
        print(f"  - {b}")
    sys.exit(1)
sys.exit(0)
PY