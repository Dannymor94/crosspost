"""Test that .env.example declares exactly the env vars the code reads.

Fail modes:
- new var added to src/ but missing from .env.example → test fails
- old var removed from src/ but still in .env.example → test fails
"""

import re
from pathlib import Path

# Regex: captures the key name from all env-var access patterns in src/:
#   os.environ["KEY"], os.environ.get("KEY"), os.getenv("KEY"),
#   cfg["KEY"], cfg.get("KEY"), load_config().get("KEY")
_ENV_ACCESS = re.compile(
    r"""(?:os\.environ(?:\.get)?\s*[\[(]|os\.getenv\s*\(|(?:cfg|load_config\(\))(?:\.get\s*)?\s*[\[(])['"]([A-Z][A-Z0-9_]+)['"]"""
)

_SRC = Path(__file__).parent.parent / "src"
_EXAMPLE = Path(__file__).parent.parent / ".env.example"


def _code_keys() -> set[str]:
    keys: set[str] = set()
    for f in _SRC.rglob("*.py"):
        for m in _ENV_ACCESS.finditer(f.read_text()):
            keys.add(m.group(1))
    return keys


def _example_keys() -> set[str]:
    keys: set[str] = set()
    for line in _EXAMPLE.read_text("utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if re.match(r"^[A-Z][A-Z0-9_]+$", key):
            keys.add(key)
    return keys


def test_env_example_matches_code() -> None:
    code = _code_keys()
    example = _example_keys()

    missing_from_example = code - example
    obsolete_in_example = example - code

    errors: list[str] = []
    if missing_from_example:
        errors.append(
            "Keys used in src/ but missing from .env.example:\n  "
            + "\n  ".join(sorted(missing_from_example))
        )
    if obsolete_in_example:
        errors.append(
            "Keys in .env.example not found in src/:\n  " + "\n  ".join(sorted(obsolete_in_example))
        )

    assert not errors, "\n\n".join(errors)
