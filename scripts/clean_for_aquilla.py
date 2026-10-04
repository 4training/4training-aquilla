#!/usr/bin/env python3
"""
Clean MediaWiki Translate wikitext into Aquilla-ready CSV and/or Markdown.

Reads *.wikitext from mediawiki/, writes *.csv / *.md to aquilla/.
Each Translate unit becomes one cell (CSV row or Markdown block).
Inline markup is limited to TipTap-friendly tags (<i>, <b>, <br>, …).

CSV format (Aquilla bilingual importer):
  id,source,target
  Worksheet_Name/Page_display_title,English Title,Translated Title
  Worksheet_Name/1,"<i>…</i>",…

CSV output live-fetches translations from 4training.net and writes
one CSV per language under aquilla/{lang}/, only for worksheets resourcesbot
counts as a translation in that language. The default language list is
LANGUAGE_LIST; pass --languages de,es to limit a run to that subset.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = REPO_ROOT / "mediawiki"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "aquilla"
DEFAULT_API_URL = "https://www.4training.net/api.php"

INLINE_KEEP = ("i", "b", "strong", "em", "u", "s", "br", "p", "code")
PAGE_DISPLAY_TITLE_KEY = "Page_display_title"

# Snapshot of languages with at least one listed worksheet
# (resourcesbot show_in_list: PDF + same major version as English).
# Omitted: en (source), tr-tanri, ku-sinj, uz-cyrl,
# and languages with nothing listed.
LANGUAGE_LIST = [
    "af", "ar", "az", "bg", "ckb", "cs", "de", "es", "fa", "fr",
    "ha", "hi", "hu", "id", "it", "kn", "ko", "ku", "ky", "lg",
    "ml", "ms", "nb", "nl", "pl", "pt-br", "rn", "ro", "ru", "sk",
    "sq", "sr", "ss", "sv", "sw", "ta", "te", "th", "ti", "tr",
    "uz", "vi", "xh", "zh",
]

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


def clean_translation(text: str) -> str:
    """Light cleanup for live-fetched translation strings."""
    text = convert_wiki_links(text.strip())
    text = keep_only_tiptap_tags(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


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

    # {{Translatable template|Italic|…}} — structural only; Translate unit is plain text
    # (allow junk tags between | and <translate>, and stray closers before }}).
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
        return Unit(t_id=t_id, text=plain, kind="text")

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

    # Outer <b>/<i>/… around a whole unit is structural (not part of the Translate unit)
    wrap = re.search(r"<(b|strong|i|em)>\s*$", before, flags=re.IGNORECASE)
    if wrap:
        tag = wrap.group(1).lower()
        if re.match(rf"^\s*</{tag}>", after, flags=re.IGNORECASE):
            return Unit(t_id=t_id, text=body, kind="text")

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


# ── MediaWiki API (live, no cache) ───────────────────────────────────────────


def api_get(api_url: str, params: dict) -> dict:
    query = urllib.parse.urlencode({**params, "format": "json"})
    url = f"{api_url}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "4training-aquilla/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_page_display_title(api_url: str, page: str, language: str = "en") -> Optional[str]:
    """English (or other) Page display title translation unit."""
    title = f"Translations:{page}/Page display title/{language}"
    try:
        data = api_get(
            api_url,
            {
                "action": "query",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "rvlimit": "1",
                "titles": title,
            },
        )
        pages = data["query"]["pages"]
        page_data = next(iter(pages.values()))
        if "missing" in page_data:
            return None
        return page_data["revisions"][0]["slots"]["main"]["*"].strip()
    except (KeyError, IndexError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
        print(f"  warning: could not fetch title for {page}: {err}", file=sys.stderr)
        return None


def language_json_url(api_url: str, language: str) -> str:
    """Raw URL of the LanguageInfo page resourcesbot writes for one language."""
    base = api_url.rsplit("/", 1)[0]
    title = urllib.parse.quote(f"4training:{language}.json", safe="")
    return f"{base}/index.php?title={title}&action=raw"


def fetch_counted_worksheets(api_url: str, language: str) -> set[str]:
    """English page names resourcesbot counts as a translation in this language.

    A worksheet is included in 4training:{language}.json when messagegroupstats
    reports at least one translated unit and both the page display title and the
    version are translated. Unfinished translations are included; worksheets
    that fail the title or version check are not.
    """
    url = language_json_url(api_url, language)
    req = urllib.request.Request(url, headers={"User-Agent": "4training-aquilla/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            str(worksheet["page"])
            for worksheet in data.get("worksheets") or []
            if worksheet.get("page")
        }
    except (KeyError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
        print(
            f"  warning: could not load counted translations for {language}: {err}",
            file=sys.stderr,
        )
        return set()


def load_counted_worksheets(api_url: str, languages: list[str]) -> dict[str, set[str]]:
    """Map each language code to the worksheets resourcesbot counts for it."""
    counted: dict[str, set[str]] = {}
    for language in languages:
        print(f"loading counted translations for {language}…")
        counted[language] = fetch_counted_worksheets(api_url, language)
        print(f"  {language}: {len(counted[language])} worksheets")
    return counted


def fetch_translations(api_url: str, page: str, language: str) -> dict[str, str]:
    """
    Map unit suffix → translation text.
    Keys look like 'Page_display_title' or '1', '2', …
    """
    try:
        data = api_get(
            api_url,
            {
                "action": "query",
                "list": "messagecollection",
                "mcgroup": f"page-{page}",
                "mclanguage": language,
                "mclimit": "500",
            },
        )
        if "error" in data:
            print(
                f"  warning: messagecollection error for {page}/{language}: "
                f"{data['error'].get('info', data['error'])}",
                file=sys.stderr,
            )
            return {}
        result: dict[str, str] = {}
        for tu in data["query"]["messagecollection"]:
            key = str(tu["key"])
            # key is "PageName/1" or "PageName/Page_display_title"
            suffix = key.split("/", 1)[-1] if "/" in key else key
            translation = tu.get("translation")
            if translation:
                result[suffix] = clean_translation(str(translation))
        return result
    except (KeyError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
        print(f"  warning: could not fetch translations for {page}/{language}: {err}", file=sys.stderr)
        return {}


def title_from_stem(stem: str) -> str:
    return stem.replace("_", " ")


# ── Renderers ────────────────────────────────────────────────────────────────


def render_markdown(units: list[Unit], page_title: Optional[str] = None) -> str:
    blocks: list[str] = []
    if page_title:
        blocks.append(f"# {page_title}")
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


def render_csv(
    stem: str,
    units: list[Unit],
    page_title: str,
    targets: Optional[dict[str, str]] = None,
) -> str:
    """targets maps 'Page_display_title' / '1' / '2' → translated string."""
    targets = targets or {}
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "source", "target"])
    writer.writerow(
        [
            f"{stem}/{PAGE_DISPLAY_TITLE_KEY}",
            page_title,
            targets.get(PAGE_DISPLAY_TITLE_KEY, ""),
        ]
    )
    for unit in units:
        writer.writerow(
            [
                f"{stem}/{unit.t_id}",
                unit.text,
                targets.get(str(unit.t_id), ""),
            ]
        )
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


def process_file(
    src: Path,
    output_dir: Path,
    fmt: str,
    api_url: str,
    languages: list[str],
    counted_worksheets: Optional[dict[str, set[str]]] = None,
) -> None:
    raw = src.read_text(encoding="utf-8")
    units = extract_units(raw)
    warn_unconverted(raw, src, units)
    stem = src.stem

    fetched = fetch_page_display_title(api_url, stem, "en")
    page_title = fetched or title_from_stem(stem)
    if not fetched:
        print(f"  warning ({stem}): using filename-derived title {page_title!r}", file=sys.stderr)

    if fmt in ("md", "both"):
        output_dir.mkdir(parents=True, exist_ok=True)
        dest = output_dir / f"{stem}.md"
        dest.write_text(render_markdown(units, page_title), encoding="utf-8")
        print(f"wrote {dest.relative_to(REPO_ROOT)} ({len(units) + 1} cells incl. title)")

    if fmt in ("csv", "both"):
        if languages:
            counted_worksheets = counted_worksheets or {}
            for lang in languages:
                if stem not in counted_worksheets.get(lang, set()):
                    print(f"  skip {stem}/{lang} (not counted as a translation)")
                    continue
                print(f"  fetching translations {stem}/{lang}…")
                targets = fetch_translations(api_url, stem, lang)
                lang_dir = output_dir / lang
                lang_dir.mkdir(parents=True, exist_ok=True)
                dest = lang_dir / f"{stem}.csv"
                dest.write_text(
                    render_csv(stem, units, page_title, targets),
                    encoding="utf-8",
                )
                filled = sum(
                    1
                    for key in [PAGE_DISPLAY_TITLE_KEY, *[str(u.t_id) for u in units]]
                    if targets.get(key)
                )
                total = len(units) + 1
                print(f"wrote {dest.relative_to(REPO_ROOT)} ({total} rows, {filled} with target)")
        else:
            output_dir.mkdir(parents=True, exist_ok=True)
            dest = output_dir / f"{stem}.csv"
            dest.write_text(render_csv(stem, units, page_title), encoding="utf-8")
            print(f"wrote {dest.relative_to(REPO_ROOT)} ({len(units) + 1} rows)")


def parse_languages(value: str) -> list[str]:
    langs = [part.strip() for part in value.split(",") if part.strip()]
    return langs


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
    parser.add_argument(
        "-l",
        "--languages",
        type=parse_languages,
        default=LANGUAGE_LIST,
        help=(
            "Comma-separated language codes; write aquilla/{lang}/*.csv "
            "with live-fetched targets (default: LANGUAGE_LIST)"
        ),
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"MediaWiki API URL (default: {DEFAULT_API_URL})",
    )
    args = parser.parse_args()

    sources = args.files or sorted(args.input_dir.glob("*.wikitext"))
    if not sources:
        print(f"No .wikitext files found in {args.input_dir}", file=sys.stderr)
        return 1

    counted_worksheets: dict[str, set[str]] = {}
    if args.languages and args.format in ("csv", "both"):
        counted_worksheets = load_counted_worksheets(args.api_url, args.languages)

    for src in sources:
        src = src if src.is_absolute() else Path.cwd() / src
        if not src.exists():
            print(f"missing: {src}", file=sys.stderr)
            return 1
        process_file(
            src,
            args.output_dir,
            args.format,
            args.api_url,
            args.languages,
            counted_worksheets,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
