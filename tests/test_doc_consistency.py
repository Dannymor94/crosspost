"""Test documentation consistency with code.

Checks:
- Every channel key in VALIDATORS appears in docs/STATUS.md §1 table.
- Every relative link in AGENTS.md and docs/*.md (excluding docs/archive/)
  resolves to an existing file.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_STATUS = _ROOT / "docs" / "STATUS.md"
_AGENTS = _ROOT / "AGENTS.md"
_DOCS = _ROOT / "docs"

_LINK_RE = re.compile(r"\[(?:[^\]]*)\]\(([^)]+)\)")


def _status_section1_keys() -> set[str]:
    """Parse channel keys from the §1 table in STATUS.md."""
    in_section = False
    keys: set[str] = set()
    for line in _STATUS.read_text("utf-8").splitlines():
        if re.match(r"^## 1\.", line):
            in_section = True
            continue
        if in_section and re.match(r"^## ", line):
            break
        if in_section and line.startswith("|") and "`" in line:
            # First column: | `key` | ...
            first_col = line.split("|")[1].strip()
            for m in re.findall(r"`([^`]+)`", first_col):
                keys.add(m)
    return keys


def _doc_files() -> list[Path]:
    files = [_AGENTS]
    for f in _DOCS.rglob("*.md"):
        if "archive" not in f.parts:
            files.append(f)
    return files


def _broken_links(doc: Path) -> list[str]:
    text = doc.read_text("utf-8")
    broken: list[str] = []
    for m in _LINK_RE.finditer(text):
        target = m.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        # Strip anchor fragment for existence check
        path_part = target.split("#")[0]
        if not path_part:
            continue  # anchor-only link
        resolved = (doc.parent / path_part).resolve()
        if not resolved.exists():
            broken.append(f"{doc.relative_to(_ROOT)}: [{target}] → not found")
    return broken


def test_all_validators_in_status() -> None:
    from crosspost.channels.validators import VALIDATORS

    validator_keys = set(VALIDATORS.keys())
    status_keys = _status_section1_keys()
    missing = validator_keys - status_keys
    assert not missing, (
        "Channels in VALIDATORS but missing from docs/STATUS.md §1:\n  "
        + "\n  ".join(sorted(missing))
    )


def test_doc_links_resolve() -> None:
    all_broken: list[str] = []
    for doc in _doc_files():
        all_broken.extend(_broken_links(doc))
    assert not all_broken, "Broken relative links in docs:\n  " + "\n  ".join(all_broken)
