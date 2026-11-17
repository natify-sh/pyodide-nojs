"""Offline: the README's table of contents and the relative links of the README and docs/HOST_CONTRACT.md are true."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")


def headings(text: str) -> list[tuple[int, str, str]]:
    """(level, title, GitHub's anchor) of every heading outside code fences; a repeated anchor gets -1, -2..."""
    out, seen, fenced = [], {}, False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        m = None if fenced else HEADING.match(line)
        if m:
            title = m.group(2)
            base = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
            n = seen.get(base, 0)
            seen[base] = n + 1
            out.append((len(m.group(1)), title, base if n == 0 else f"{base}-{n}"))
    return out


def links(text: str) -> list[str]:
    return re.findall(r"\]\(([^)\s]+)\)", re.sub(r"```.*?```", "", text, flags=re.S))


def test_anchor_links_resolve_and_the_toc_lists_every_section():
    text = README.read_text(encoding="utf-8")
    anchors = {a for _, _, a in headings(text)}
    used = [x[1:] for x in links(text) if x.startswith("#")]
    assert [a for a in used if a not in anchors] == []
    sections = [a for level, _, a in headings(text) if level == 2]
    assert [a for a in sections if a not in used] == []


def test_relative_file_links_exist():
    for doc in (README, ROOT / "docs" / "HOST_CONTRACT.md"):
        for link in links(doc.read_text(encoding="utf-8")):
            if not re.match(r"[a-z]+:|#", link):
                target = (doc.parent / link.split("#")[0]).resolve()
                assert target.exists(), f"{doc.name}: {link}"
