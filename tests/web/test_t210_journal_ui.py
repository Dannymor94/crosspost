"""T-210: execute the actual journal JS predicate without a browser or React."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).parents[2] / "src/crosspost/web/frontend/index.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_submitted_journal_filter_and_badge_semantics():
    source = INDEX.read_text(encoding="utf-8")
    meta = re.search(r"(?ms)^const REPORT_META = \{.*?^\};", source)
    predicate = re.search(r"(?ms)^const isReportProblem = .*?;", source)
    assert meta is not None and predicate is not None

    js = "\n".join(
        [
            meta.group(0),
            predicate.group(0),
            "const states = ['submitted', 'partial', 'failed', 'done', 'in_progress', 'unknown'];",
            "console.log(JSON.stringify({states: states.map(overall => isReportProblem({overall})), "
            "meta: REPORT_META}));",
        ]
    )
    run = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    result = json.loads(run.stdout)
    assert result["states"] == [True, True, True, False, False, False]
    assert result["meta"]["done"]["label"] == "Опубликовано"
    assert "без квитанции" in result["meta"]["submitted"]["label"]
    assert result["meta"]["in_progress"]["label"] == "Выполняется"
    assert "Требуют внимания" in source
    assert 'const canEdit = r.overall === "failed";' in source
