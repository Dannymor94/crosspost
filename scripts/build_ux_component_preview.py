"""Build a standalone offline preview from the actual shared UX assets."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "src/crosspost/web/frontend"
TEMPLATE = ROOT / "tests/fixtures/ux-component-preview.html"


def build() -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    assets = {
        "/* CP_SYSTEM_CSS */": FRONTEND / "styles/ux-system.css",
        "/* CP_THEME_JS */": FRONTEND / "shared/ux-theme.js",
        "/* CP_NAV_JS */": FRONTEND / "shared/ux-navigation.js",
    }
    for marker, path in assets.items():
        if html.count(marker) != 1:
            raise ValueError(f"Missing or duplicated preview marker: {marker}")
        value = path.read_text(encoding="utf-8")
        if "</script" in value.lower() or "</style" in value.lower():
            raise ValueError(f"Unsafe inline asset: {path.name}")
        html = html.replace(marker, value)
    return html


if __name__ == "__main__":
    output = ROOT / "build/ux-preview/crosspost-ux-components.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build(), encoding="utf-8")
