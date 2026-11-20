#!/usr/bin/env python3
"""Turn the mellow-youlan-dark fcitx5 skin into Noctalia template inputs.

The output keeps mellow's rounded SVG shapes and only replaces colours with
`{{ colors.<role>.default.hex }}` tokens.  Noctalia renders the tokens for the
current wallpaper; the result is written to
`~/.local/share/fcitx5/themes/noctalia/`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# (section, key) -> Material You role
COLOURS = {
    ("InputPanel", "NormalColor"): "on_surface",
    ("InputPanel", "HighlightCandidateColor"): "on_primary",
    ("InputPanel", "HighlightColor"): "on_primary",
    ("InputPanel", "HighlightBackgroundColor"): "primary",
    ("InputPanel/Background", "Color"): "surface_container_high",
    ("InputPanel/Highlight", "Color"): "on_primary",
    ("Menu", "NormalColor"): "on_surface",
    ("Menu", "HighlightCandidateColor"): "on_primary",
    ("Menu/Background", "Color"): "surface_container_high",
    ("Menu/Highlight", "Color"): "on_primary",
    ("Menu/Separator", "Color"): "outline",
    ("Menu/CheckBox", "Color"): "on_surface",
    ("Menu/SubMenu", "Color"): "on_surface",
}

SVG_COLOURS = {
    "highlight.svg": {
        "#015ad7": "primary",
        "#1781b5": "primary",
    },
    "panel.svg": {
        "#151515": "surface_container_high",
        "#909090": "outline",
    },
}


def token(role: str) -> str:
    return "{{ colors." + role + ".default.hex }}"


def render_theme_conf(path: Path) -> None:
    section = ""
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            lines.append(line)
            continue
        if line.startswith("Name="):
            lines.append("Name=Noctalia")
            continue
        if line.startswith("Description="):
            lines.append('Description="Noctalia Material You (mellow shape)"')
            continue
        key = line.split("=", 1)[0] if "=" in line else ""
        role = COLOURS.get((section, key))
        if role:
            lines.append(f"{key}={token(role)}")
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_svg(path: Path, mapping: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    for colour, role in mapping.items():
        text = text.replace(colour, token(role))
    path.write_text(text, encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: render.py <theme-dir>", file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    render_theme_conf(root / "theme.conf")
    for name, mapping in SVG_COLOURS.items():
        render_svg(root / name, mapping)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
