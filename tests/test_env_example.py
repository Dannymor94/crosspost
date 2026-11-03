"""Test that .env.example declares exactly the env vars the code reads.

Fail modes:
- new var added to src/ but missing from .env.example → test fails
- old var removed from src/ but still in .env.example → test fails
"""

import ast
import re
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
_EXAMPLE = Path(__file__).parent.parent / ".env.example"


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _env_key_from_node(node: ast.AST) -> str | None:
    """Return the env var name if this AST node is an env-var access, else None.

    Covers:
      os.environ["KEY"], os.environ.get("KEY"),
      os.getenv("KEY"),
      cfg["KEY"], cfg.get("KEY"),
      <any_call>.get("KEY")  — catches load_config().get("KEY")
    """
    if isinstance(node, ast.Subscript):
        # os.environ["KEY"] or cfg["KEY"]
        if _is_os_environ(node.value) or (
            isinstance(node.value, ast.Name) and node.value.id == "cfg"
        ):
            return _const_str(node.slice)

    if isinstance(node, ast.Call) and node.args:
        first = _const_str(node.args[0])
        if first is None:
            return None
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "get":
                # os.environ.get("KEY"), cfg.get("KEY"), load_config().get("KEY")
                if (
                    _is_os_environ(func.value)
                    or (isinstance(func.value, ast.Name) and func.value.id == "cfg")
                    or isinstance(func.value, ast.Call)
                ):
                    return first
            if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
                # os.getenv("KEY")
                return first

    return None


def _code_keys() -> set[str]:
    keys: set[str] = set()
    for f in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            key = _env_key_from_node(node)
            if key and re.match(r"^[A-Z][A-Z0-9_]+$", key):
                keys.add(key)
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
