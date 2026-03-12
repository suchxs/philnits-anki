#!/usr/bin/env python3
"""Convert Obsidian PhilNITS vault flashcards to an Anki-importable TSV file.

The script expects notes that follow the vault template:
- metadata lines (Created/Category/Status)
- a title line beginning with '# '
- a line with only '?' as front/back separator
- an end marker line with only '---' (optional; EOF also works)
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


OBSIDIAN_IMAGE_RE = re.compile(r"!\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
TAG_RE = re.compile(r"#([A-Za-z0-9_-]+)")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
MATH_BLOCK_RE = re.compile(r"\$\$(.+?)\$\$", flags=re.DOTALL)
MATH_INLINE_RE = re.compile(r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$(?!\$)")


@dataclass
class Card:
    note_id: str
    front_html: str
    back_html: str
    tags: str
    source_file: Path


def extract_line_value(lines: list[str], field: str) -> str:
    prefix = f"{field}:"
    for line in lines:
        if line.strip().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def extract_note_id(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].split("%%", 1)[0].strip()
    return ""


def find_title_index(lines: list[str]) -> int:
    for idx, line in enumerate(lines):
        if line.strip().startswith("# "):
            return idx
    return -1


def find_separator_index(lines: list[str], start: int) -> int:
    for idx in range(start, len(lines)):
        if lines[idx].strip() == "?":
            return idx
    return -1


def find_end_marker_index(lines: list[str], start: int) -> int:
    for idx in range(start, len(lines)):
        if lines[idx].strip() == "---":
            return idx
    return len(lines)


def markdownish_to_anki_html(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Obsidian embeds -> image tags for actual image files, text for the rest.
    def _embed_to_html(match: re.Match[str]) -> str:
        target = (match.group(1) or "").strip()
        ext = Path(target).suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            return f'<img src="{target}">'

        # Excalidraw notes are not image assets Anki can render directly.
        if target.endswith(".excalidraw") or target.endswith(".excalidraw.md"):
            return ""

        return target

    text = OBSIDIAN_IMAGE_RE.sub(_embed_to_html, text)

    # Convert wiki links to display text.
    def _wiki_to_text(match: re.Match[str]) -> str:
        target = (match.group(1) or "").strip()
        alias = (match.group(2) or "").strip()
        return alias or target

    text = WIKI_LINK_RE.sub(_wiki_to_text, text)

    # Convert $...$ and $$...$$ to explicit MathJax delimiters for Anki.
    text = MATH_BLOCK_RE.sub(lambda m: "\\[" + m.group(1).strip() + "\\]", text)
    text = MATH_INLINE_RE.sub(lambda m: "\\(" + m.group(1).strip() + "\\)", text)

    # Normalize tab/newline handling for TSV; keep visual line breaks in Anki.
    text = text.replace("\t", " ")
    lines = [line.rstrip() for line in text.split("\n")]

    # Remove trailing blank lines that add no value in cards.
    while lines and lines[-1] == "":
        lines.pop()

    return "<br>".join(lines)


def collect_tags(raw_values: Iterable[str], year_hint: str, note_id: str) -> str:
    tags: list[str] = []

    for raw in raw_values:
        for match in TAG_RE.findall(raw):
            tags.append(match)

    if year_hint.isdigit() and len(year_hint) == 4:
        tags.append(year_hint)

    if note_id:
        note_tag = re.sub(r"[^A-Za-z0-9_-]+", "_", note_id)
        tags.append(note_tag)

    # Stable dedupe while preserving insertion order.
    seen: set[str] = set()
    unique = []
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            unique.append(tag)

    return " ".join(unique)


def parse_card_from_file(path: Path) -> Card | None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    note_id = extract_note_id(lines)
    title_idx = find_title_index(lines)
    if title_idx == -1:
        return None

    sep_idx = find_separator_index(lines, title_idx + 1)
    if sep_idx == -1:
        return None

    end_idx = find_end_marker_index(lines, sep_idx + 1)

    front = "\n".join(lines[title_idx + 1 : sep_idx]).strip()
    back = "\n".join(lines[sep_idx + 1 : end_idx]).strip()

    # Remove template artifacts from the exported back field.
    back = clean_back_content(back)

    if not front or not back:
        return None

    category = extract_line_value(lines, "Category")
    status = extract_line_value(lines, "Status")
    year_hint = path.parent.name

    return Card(
        note_id=note_id or path.stem,
        front_html=markdownish_to_anki_html(front),
        back_html=markdownish_to_anki_html(back),
        tags=collect_tags([category, status], year_hint, note_id or path.stem),
        source_file=path,
    )


def clean_back_content(back: str) -> str:
    cleaned: list[str] = []
    for line in back.splitlines():
        stripped = line.strip()

        # Stop before references section if it exists.
        if stripped.lower().startswith("# references"):
            break

        # Drop Obsidian template comment markers.
        if stripped.startswith("%%") and stripped.endswith("%%"):
            continue

        cleaned.append(line)

    # Trim trailing blank lines after cleanup.
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    return "\n".join(cleaned).strip()


def iter_markdown_files(vault_root: Path) -> Iterable[Path]:
    for path in sorted(vault_root.rglob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        if path.parts and "Templates" in path.parts:
            continue
        yield path


def write_anki_tsv(cards: list[Card], out_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)

    with out_file.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("#separator:tab\n")
        handle.write("#html:true\n")
        handle.write("#columns:NoteID\tFront\tBack\tTags\n")

        for card in cards:
            row = [card.note_id, card.front_html, card.back_html, card.tags]
            handle.write("\t".join(row) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert Obsidian flashcards to an Anki-importable TSV file."
    )
    parser.add_argument(
        "--vault",
        default="philnits-vault",
        help="Path to the Obsidian vault root (default: philnits-vault).",
    )
    parser.add_argument(
        "--out",
        default="anki_import/philnits_cards.tsv",
        help="Output TSV path (default: anki_import/philnits_cards.tsv).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    vault_root = Path(args.vault).resolve()
    out_file = Path(args.out).resolve()

    if not vault_root.exists() or not vault_root.is_dir():
        parser.error(f"Vault path does not exist or is not a directory: {vault_root}")

    cards: list[Card] = []
    for md_file in iter_markdown_files(vault_root):
        card = parse_card_from_file(md_file)
        if card is not None:
            cards.append(card)

    if not cards:
        parser.error("No valid cards were found. Check the vault format and input path.")

    write_anki_tsv(cards, out_file)
    print(f"Wrote {len(cards)} cards to {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
