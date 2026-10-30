"""Regression: index.html module script must parse without syntax errors.

Extracts the <script type="module"> block and compiles it with esbuild.
Skips if node/npx is not available.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).parents[2] / "src/crosspost/web/frontend/index.html"


def _extract_script(html: str) -> str:
    m = re.search(r'<script type="module">(.*?)</script>', html, re.DOTALL)
    assert m, "No <script type='module'> found in index.html"
    return m.group(1)


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx not available")
def test_index_html_script_parses():
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = _extract_script(html)

    with tempfile.NamedTemporaryFile(suffix=".js", mode="w", encoding="utf-8", delete=False) as f:
        f.write(script)
        tmp = f.name

    result = subprocess.run(
        ["npx", "--yes", "esbuild", tmp, "--bundle=false", "--platform=browser"],
        capture_output=True,
        text=True,
    )
    # esbuild exits non-zero and prints [ERROR] on syntax errors
    errors = [ln for ln in result.stderr.splitlines() if "[ERROR]" in ln or "✘" in ln]
    assert result.returncode == 0 and not errors, (
        f"esbuild found errors in index.html script:\n{result.stderr[:2000]}"
    )
