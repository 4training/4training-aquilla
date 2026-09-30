#!/usr/bin/env python3
"""
Clean MediaWiki Translate wikitext into Aquilla-ready Markdown.

Reads *.wikitext from mediawiki/, writes *.md to aquilla/.
Each Translate unit becomes its own block (blank line between cells).
Inline markup is limited to TipTap-friendly tags (<i>, <b>, <br>, …).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = REPO_ROOT / "mediawiki"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "aquilla"

# Tags that may remain inside cell content (Aquilla TipTap allowlist subset we emit).
INLINE_KEEP = ("i", "b", "strong", "em", "u", "s", "br", "p", "code")

# One Translate unit, including the T: marker.
TRANSLATE_UNIT = re.compile(
    r"<translate>\s*<!--T:\d+-->\s*(.*?)</translate>",
    re.IGNORECASE | re.DOTALL,
)


def strip_page_chrome(text: str) -> str:
    text = re.sub(r"__NOTOC__", "", text)
    text = re.sub(r"<sidebar>.*?</sidebar>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<languages\s*/?>", "", text, flags=re.IGNORECASE)
    # Zero-width spaces sometimes appear around headings in copied wikitext
    text = text.replace("\u200b", "")
    return text


def strip_footer_templates(text: str) -> str:
    for name in ("PdfDownload", "OdtDownload", "Version"):
        text = re.sub(
            rf"\{{\{{{name}\|.*?\}}\}}",
            "",
            text,
            flags=re.DOTALL,
        )
    return text


def strip_file_links(text: str) -> str:
    """Drop embedded MediaWiki images (not useful as Aquilla cells)."""
    return re.sub(r"\[\[File:[^\]]+\]\]", "", text, flags=re.IGNORECASE)


def unwrap_bold_around_italic_templates(text: str) -> str:
    """<span><b>{{Italic|…}}</b></span> → {{Italic|…}} (title lines)."""
    text = re.sub(
        r"<span\b[^>]*>\s*<b>\s*(\{\{Translatable template\|Italic\|.*?\}\})\s*</b>\s*</span>",
        r"\1",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"<b>\s*(\{\{Translatable template\|Italic\|.*?\}\})\s*</b>",
        r"\1",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return text


def _strip_all_tags(content: str) -> str:
    return re.sub(r"<[^>]+>", "", content).strip()


def _clean_unit_body(content: str) -> str:
    """Normalize text inside one Translate unit (keep TipTap-safe inline tags)."""
    content = content.strip()
    # Soft line breaks inside a unit → <br/>; collapse other internal newlines to spaces
    content = re.sub(r"<br\s*/?>\s*", "<br/>", content, flags=re.IGNORECASE)
    parts = re.split(r"(<br/>)", content)
    normalized: list[str] = []
    for part in parts:
        if part == "<br/>":
            normalized.append(part)
        else:
            normalized.append(re.sub(r"\s*\n\s*", " ", part).strip())
    content = "".join(normalized)
    content = re.sub(r"[ \t]{2,}", " ", content)
    return content.strip()


def _block(content: str) -> str:
    """Emit one Aquilla cell as its own paragraph block."""
    content = content.strip()
    if not content:
        return ""
    return f"\n\n{content}\n\n"


def convert_heading_units(text: str) -> str:
    """== <translate>Title</translate> == → ## Title (own cell)."""

    def replacer(match: re.Match[str]) -> str:
        level = len(match.group(1))
        title = _clean_unit_body(match.group(2))
        return _block(f"{'#' * level} {title}")

    return re.sub(
        r"^(={2,6})\s*" + TRANSLATE_UNIT.pattern + r"\s*\1\s*$",
        replacer,
        text,
        flags=re.MULTILINE | re.IGNORECASE | re.DOTALL,
    )


def convert_italic_template_units(text: str) -> str:
    """{{Translatable template|Italic|<translate>…</translate>}} → <i>…</i>."""

    def replacer(match: re.Match[str]) -> str:
        # Body may still contain a translate unit, or already-unwrapped / broken markup
        inner = match.group(1)
        unit = TRANSLATE_UNIT.search(inner)
        if unit:
            body = _clean_unit_body(unit.group(1))
        else:
            body = _strip_all_tags(inner)
        return _block(f"<i>{body}</i>")

    return re.sub(
        r"\{\{Translatable template\|Italic\|(.*?)\}\}",
        replacer,
        text,
        flags=re.DOTALL,
    )


def convert_list_units(text: str) -> str:
    """* / # / ; / : lines that wrap a translate unit → markdown / plain cells."""

    def bullet(match: re.Match[str]) -> str:
        depth = len(match.group(1))
        body = _clean_unit_body(match.group(2))
        return _block(f"{'  ' * (depth - 1)}- {body}")

    def numbered(match: re.Match[str]) -> str:
        depth = len(match.group(1))
        body = _clean_unit_body(match.group(2))
        return _block(f"{'  ' * (depth - 1)}1. {body}")

    def definition(match: re.Match[str]) -> str:
        body = _clean_unit_body(match.group(1))
        return _block(body)

    text = re.sub(
        r"^(\*+)\s*" + TRANSLATE_UNIT.pattern + r"\s*$",
        bullet,
        text,
        flags=re.MULTILINE | re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"^(#+)\s*" + TRANSLATE_UNIT.pattern + r"\s*$",
        numbered,
        text,
        flags=re.MULTILINE | re.IGNORECASE | re.DOTALL,
    )
    # Definition term / description (; and :) — each unit is its own cell
    text = re.sub(
        r"^[;:]\s*" + TRANSLATE_UNIT.pattern + r"\s*$",
        definition,
        text,
        flags=re.MULTILINE | re.IGNORECASE | re.DOTALL,
    )
    return text


def convert_remaining_translate_units(text: str) -> str:
    """Any leftover <translate>…</translate> → its own cell (preserve <b>/<i> wrappers)."""

    # <b><translate>…</translate></b> or <i>…</i>
    def wrapped(match: re.Match[str]) -> str:
        tag = match.group(1).lower()
        body = _clean_unit_body(match.group(2))
        return _block(f"<{tag}>{body}</{tag}>")

    text = re.sub(
        r"<(b|strong|i|em)>\s*" + TRANSLATE_UNIT.pattern + r"\s*</\1>",
        wrapped,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    def bare(match: re.Match[str]) -> str:
        return _block(_clean_unit_body(match.group(1)))

    text = re.sub(TRANSLATE_UNIT, bare, text)
    return text


def convert_wiki_links(text: str) -> str:
    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(
        r"\[\[([^\]]+)\]\]",
        lambda m: m.group(1).replace("_", " "),
        text,
    )
    return text


def unwrap_style_containers(text: str) -> str:
    text = re.sub(r"<span\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</span>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<div\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</div>", "", text, flags=re.IGNORECASE)
    # Tables: drop structure, keep any text already extracted as units
    text = re.sub(r"</?(?:table|tr|td|th)\b[^>]*>", "", text, flags=re.IGNORECASE)
    return text


def convert_leftover_wiki_lists(text: str) -> str:
    """Handle * / ; / : lines that are not Translate-wrapped (rare)."""
    lines: list[str] = []
    for line in text.splitlines():
        if re.match(r"^:+\s+\S", line):
            line = re.sub(r"^:+\s*", "", line)
            lines.append(_block(line).strip("\n"))
        elif re.match(r"^\*+\s+\S", line):
            depth = len(re.match(r"^\*+", line).group(0))
            item = re.sub(r"^\*+\s*", "", line)
            lines.append(_block(f"{'  ' * (depth - 1)}- {item}").strip("\n"))
        elif re.match(r"^;+\s+\S", line):
            lines.append(_block(re.sub(r"^;+\s*", "", line)).strip("\n"))
        else:
            # Do not touch markdown headings (## …) or already-converted cells
            lines.append(line)
    return "\n".join(lines)


def keep_only_tiptap_tags(text: str) -> str:
    keep = "|".join(INLINE_KEEP)
    text = re.sub(
        rf"</?(?!(?:{keep})\b)[a-zA-Z][\w:-]*\b[^>]*>",
        "",
        text,
    )
    text = re.sub(r"<br\s*/?>", "<br/>", text, flags=re.IGNORECASE)
    return text


def normalize_cells(text: str) -> str:
    """
    Final pass: one non-empty line (or soft-broken unit) per cell,
    always separated by a blank line.
    """
    chunks = re.split(r"\n\s*\n", text)
    cells: list[str] = []
    orphan_tag = re.compile(r"^</?[a-zA-Z][\w:-]*\s*/?>$", re.IGNORECASE)

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or orphan_tag.match(chunk):
            continue
        if "\n" in chunk and "<br/>" not in chunk:
            for line in chunk.splitlines():
                line = line.strip()
                if line and not orphan_tag.match(line):
                    cells.append(line)
        else:
            cells.append(re.sub(r"\s*\n\s*", " ", chunk).strip())

    return "\n\n".join(cells) + "\n"


def warn_leftovers(text: str, source: Path) -> None:
    patterns = [
        (r"<translate\b", "unconverted <translate>"),
        (r"<!--T:\d+-->", "leftover T: marker"),
        (r"\{\{", "unconverted {{template}}"),
        (r"\[\[", "unconverted [[wikilink]]"),
        (r"__\w+__", "magic word"),
        (r"</?(?:table|tr|td|span|div)\b", "leftover structural HTML"),
    ]
    for pattern, label in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            print(f"  warning ({source.name}): {label}", file=sys.stderr)


def clean_wikitext(text: str) -> str:
    text = strip_page_chrome(text)
    text = strip_footer_templates(text)
    text = strip_file_links(text)
    text = unwrap_bold_around_italic_templates(text)

    # Structural units first (each emits its own blank-line-separated block)
    text = convert_heading_units(text)
    text = convert_italic_template_units(text)
    text = convert_list_units(text)
    text = convert_remaining_translate_units(text)

    text = unwrap_style_containers(text)
    text = convert_leftover_wiki_lists(text)
    text = convert_wiki_links(text)
    text = keep_only_tiptap_tags(text)
    text = normalize_cells(text)
    return text


def process_file(src: Path, dest: Path) -> None:
    raw = src.read_text(encoding="utf-8")
    cleaned = clean_wikitext(raw)
    warn_leftovers(cleaned, src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(cleaned, encoding="utf-8")
    cells = [c for c in cleaned.split("\n\n") if c.strip()]
    print(f"wrote {dest.relative_to(REPO_ROOT)} ({len(cells)} cells)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Specific .wikitext files (default: all in mediawiki/)",
    )
    parser.add_argument(
        "-i",
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Input directory (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()

    sources = args.files or sorted(args.input_dir.glob("*.wikitext"))
    if not sources:
        print(f"No .wikitext files found in {args.input_dir}", file=sys.stderr)
        return 1

    for src in sources:
        src = src if src.is_absolute() else Path.cwd() / src
        if not src.exists():
            print(f"missing: {src}", file=sys.stderr)
            return 1
        dest = args.output_dir / f"{src.stem}.md"
        process_file(src, dest)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
