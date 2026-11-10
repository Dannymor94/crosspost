"""Submitted remains visibly unconfirmed in both interfaces."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).parents[2] / "src/crosspost/web/frontend"


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
@pytest.mark.parametrize(
    "statuses", [["submitted"], ["done", "submitted"], ["done"], ["failed"], ["needs_relogin"]]
)
def test_client_feedback(statuses):
    source = (FRONTEND / "client.html").read_text()
    function = re.search(
        r"function publicationFeedback\(outcomes\) \{.*?\n\}", source, re.S
    ).group()
    program = (
        function
        + "\nconsole.log(JSON.stringify(publicationFeedback("
        + json.dumps([{"status": s} for s in statuses])
        + ")));"
    )
    result = json.loads(subprocess.check_output(["node", "-e", program], text=True))
    if any(s in {"failed", "needs_relogin"} for s in statuses):
        assert result["error"]
    elif "submitted" in statuses:
        assert result["unconfirmed"]
        assert result["msg"] == "Отправлено, подтверждение недоступно"
    else:
        assert not result["unconfirmed"]
        assert result["msg"] == "Пост опубликован."


def test_admin_submitted_label_is_neutral():
    source = (FRONTEND / "index.html").read_text()
    assert (
        'submitted:     { cls: "warning", label: "Отправлено, подтверждение недоступно" }' in source
    )
    assert 'r.status === "done" || r.status === "submitted" ? "✓ опубликовано"' not in source
