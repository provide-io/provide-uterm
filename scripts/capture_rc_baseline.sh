#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-artifacts/rc-baseline}"
mkdir -p "${OUT_DIR}"

python --version > "${OUT_DIR}/python-version.txt"
uv --version > "${OUT_DIR}/uv-version.txt"
uname -a > "${OUT_DIR}/os-image.txt"

if uv run playwright --version > "${OUT_DIR}/playwright-version.txt" 2>/dev/null; then
  :
else
  echo "playwright not available" > "${OUT_DIR}/playwright-version.txt"
fi

uv lock --check
cp uv.lock "${OUT_DIR}/uv.lock"
shasum -a 256 uv.lock > "${OUT_DIR}/uv.lock.sha256"

uv run ruff check . > "${OUT_DIR}/ruff.txt"
uv run mypy src/ > "${OUT_DIR}/mypy.txt"
uv run ty check src/ > "${OUT_DIR}/ty.txt"
uv run bandit -r src/ -ll > "${OUT_DIR}/bandit.txt"
uv run pytest -q > "${OUT_DIR}/pytest.txt"

cat > "${OUT_DIR}/pass-fail-matrix.txt" <<'EOF'
ruff: pass if ruff.txt contains no diagnostics
mypy: pass if mypy.txt ends with "Success: no issues found"
ty: pass if ty.txt reports 0 diagnostics
bandit: pass if no findings at configured severity/threshold
pytest: pass if all tests passed
EOF

echo "Baseline captured at ${OUT_DIR}"
