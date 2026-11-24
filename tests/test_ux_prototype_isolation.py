"""Design-only UX prototype: no runtime integration or network permissions."""
from pathlib import Path

PROTOTYPE = Path(__file__).resolve().parents[1] / "prototypes" / "ux-00" / "index.html"


def test_prototype_network_isolation_contract() -> None:
    html = PROTOTYPE.read_text(encoding="utf-8")
    assert "connect-src 'none'" in html
    assert "form-action 'none'" in html
    assert "<script src=" not in html
    assert "<link rel=" not in html
    assert "Демонстрационный прототип" in html
