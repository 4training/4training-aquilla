#!/usr/bin/env python3
"""
Clean MediaWiki Translate wikitext into Aquilla-ready CSV and/or Markdown.

Reads *.wikitext from mediawiki/, writes *.csv / *.md to aquilla/.
Each Translate unit becomes one cell (CSV row or Markdown block).
Inline markup is limited to TipTap-friendly tags (<i>, <b>, <br>, …).

CSV format (Aquilla bilingual importer):
  id,source,target
  Worksheet_Name/1,"<i>Title</i>",
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = REPO_ROOT / "mediawiki"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "aquilla"

INLINE_KEEP = ("i", "b", "strong", "em", "u", "s", "br", "p", "code")

# Captures T:N and body.
TRANSLATE_UNIT = re.compile(
    r"<translate>\s*<!--T:(\d+)-->\s*(.*?)</translate>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class Unit:
    t_id: int
    text: str
    kind: str  # "heading" | "list" | "text"
    heading_level: int = 0
    list_depth: int = 0
    list_ordered: bool = False


def strip_page_chrome(text: str) -> str:
    text = re.sub(r"__NOTOC__", "", text)
    text = re.sub(r"<sidebar>.*?</sidebar>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<languages\s*/?>", "", text, flags=re.IGNORECASE)
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
    return re.sub(r"\[\[File:[^\]]+\]\]", "", text, flags=re.IGNORECASE)


def unwrap_bold_around_italic_templates(text: str) -> str:
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


def convert_wiki_links(text: str) -> str:
    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(
        r"\[\[([^\]]+)\]\]",
        lambda m: m.group(1).replace("_", " "),
        text,
    )
    return text


def keep_only_tiptap_tags(text: str) -> str:
    keep = "|".join(INLINE_KEEP)
    text = re.sub(
        rf"</?(?!(?:{keep})\b)[a-zA-Z][\w:-]*\b[^>]*>",
        "",
        text,
    )
    text = re.sub(r"<br\s*/?>", "<br/>", text, flags=re.IGNORECASE)
    return text


def _clean_unit_body(content: str) -> str:
    content = content.strip()
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
    content = convert_wiki_links(content)
    content = keep_only_tiptap_tags(content)
    return content.strip()


def _line_prefix_before(text: str, pos: int) -> str:
    """Return text from start of the line containing pos up to pos."""
    line_start = text.rfind("\n", 0, pos) + 1
    return text[line_start:pos]


def _classify_and_build(text: str, match: re.Match[str]) -> Unit:
    t_id = int(match.group(1))
    body = _clean_unit_body(match.group(2))
    start, end = match.start(), match.end()
    before = text[:start]
    after = text[end:]
    line_prefix = _line_prefix_before(text, start).rstrip()

    # {{Translatable template|Italic|…}} (allow junk tags between | and <translate>
    # and stray closers before }} from broken nestings like Colored Lenses T:1)
    italic_open = re.search(
        r"\{\{Translatable template\|Italic\|(?:(?!\{\{).)*$",
        before,
        flags=re.DOTALL,
    )
    if italic_open and re.match(
        r"^(?:\s*</(?:b|strong|i|em|span)>)*\s*\}\}",
        after,
        flags=re.IGNORECASE,
    ):
        plain = re.sub(r"<[^>]+>", "", body).strip()
        return Unit(t_id=t_id, text=f"<i>{plain}</i>", kind="text")

    # == Title == / === Title ===
    heading_prefix = re.search(r"(={2,6})\s*$", line_prefix)
    if heading_prefix:
        marks = heading_prefix.group(1)
        if re.match(rf"^\s*{re.escape(marks)}", after):
            return Unit(
                t_id=t_id,
                text=body,
                kind="heading",
                heading_level=len(marks),
            )

    # Unordered / ordered wiki lists
    list_match = re.fullmatch(r"(\*+|#+)\s*", line_prefix)
    if list_match:
        markers = list_match.group(1)
        return Unit(
            t_id=t_id,
            text=body,
            kind="list",
            list_depth=len(markers),
            list_ordered=markers.startswith("#"),
        )

    # Definition term / indent (; or :)
    if re.fullmatch(r"[;:]\s*", line_prefix):
        return Unit(t_id=t_id, text=body, kind="text")

    # <b>/<i>/… wrappers around the unit
    wrap = re.search(r"<(b|strong|i|em)>\s*$", before, flags=re.IGNORECASE)
    if wrap:
        tag = wrap.group(1).lower()
        if re.match(rf"^\s*</{tag}>", after, flags=re.IGNORECASE):
            return Unit(t_id=t_id, text=f"<{tag}>{body}</{tag}>", kind="text")

    return Unit(t_id=t_id, text=body, kind="text")


def extract_units(text: str) -> list[Unit]:
    text = strip_page_chrome(text)
    text = strip_footer_templates(text)
    text = strip_file_links(text)
    text = unwrap_bold_around_italic_templates(text)

    units: list[Unit] = []
    for match in TRANSLATE_UNIT.finditer(text):
        unit = _classify_and_build(text, match)
        if unit.text:
            units.append(unit)
    return units


def render_markdown(units: list[Unit]) -> str:
    blocks: list[str] = []
    for unit in units:
        if unit.kind == "heading":
            level = unit.heading_level or 2
            blocks.append(f"{'#' * level} {unit.text}")
        elif unit.kind == "list":
            depth = max(unit.list_depth, 1)
            indent = "  " * (depth - 1)
            marker = "1." if unit.list_ordered else "-"
            blocks.append(f"{indent}{marker} {unit.text}")
        else:
            blocks.append(unit.text)
    return "\n\n".join(blocks) + "\n"


def render_csv(stem: str, units: list[Unit]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "source", "target"])
    for unit in units:
        writer.writerow([f"{stem}/{unit.t_id}", unit.text, ""])
    return buf.getvalue()


def warn_unconverted(text: str, source: Path, units: list[Unit]) -> None:
    raw = strip_file_links(
        unwrap_bold_around_italic_templates(
            strip_footer_templates(strip_page_chrome(text))
        )
    )
    all_ids = {int(m.group(1)) for m in TRANSLATE_UNIT.finditer(raw)}
    extracted_ids = {u.t_id for u in units}
    missing = sorted(all_ids - extracted_ids)
    if missing:
        print(f"  warning ({source.name}): missing T: ids {missing}", file=sys.stderr)


def process_file(src: Path, output_dir: Path, fmt: str) -> None:
    raw = src.read_text(encoding="utf-8")
    units = extract_units(raw)
    warn_unconverted(raw, src, units)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem

    if fmt in ("md", "both"):
        dest = output_dir / f"{stem}.md"
        dest.write_text(render_markdown(units), encoding="utf-8")
        print(f"wrote {dest.relative_to(REPO_ROOT)} ({len(units)} cells)")

    if fmt in ("csv", "both"):
        dest = output_dir / f"{stem}.csv"
        dest.write_text(render_csv(stem, units), encoding="utf-8")
        print(f"wrote {dest.relative_to(REPO_ROOT)} ({len(units)} rows)")


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
    parser.add_argument(
        "-f",
        "--format",
        choices=("md", "csv", "both"),
        default="both",
        help="Output format (default: both)",
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
        process_file(src, args.output_dir, args.format)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
