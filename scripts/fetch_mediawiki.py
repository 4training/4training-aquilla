#!/usr/bin/env python3
"""
Download English worksheet wikitext from 4training.net into mediawiki/.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "mediawiki"
DEFAULT_API_URL = "https://www.4training.net/api.php"

# Keep in sync with ForTrainingLib.get_worksheet_list():
# https://github.com/4training/pywikitools/blob/main/pywikitools/fortraininglib.py
WORKSHEET_LIST = [
    "God's_Story_(five_fingers)",
    "God's_Story_(first_and_last_sacrifice)",
    "Baptism",
    "Prayer",
    "Forgiving_Step_by_Step",
    "Confessing_Sins_and_Repenting",
    "Time_with_God",
    "Hearing_from_God",
    "Church",
    "Healing",
    "Dealing_with_Money",
    "My_Story_with_God",
    "Bible_Reading_Hints",
    "Bible_Reading_Hints_(Seven_Stories_full_of_Hope)",
    "Bible_Reading_Hints_(Starting_with_the_Creation)",
    "The_Three-Thirds_Process",
    "Training_Meeting_Outline",
    "A_Daily_Prayer",
    "Overcoming_Fear_and_Anger",
    "Getting_Rid_of_Colored_Lenses",
    "Family_and_our_Relationship_with_God",
    "Overcoming_Pride_and_Rebellion",
    "Overcoming_Negative_Inheritance",
    "Forgiving_Step_by_Step:_Training_Notes",
    "Leading_Others_Through_Forgiveness",
    "The_Role_of_a_Helper_in_Prayer",
    "Leading_a_Prayer_Time",
    "How_to_Continue_After_a_Prayer_Time",
    "Four_Kinds_of_Disciples",
]


def fetch_page_source(api_url: str, page: str) -> Optional[str]:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "rvlimit": "1",
            "titles": page,
            "format": "json",
        }
    )
    url = f"{api_url}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "4training-aquilla/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        page_data = next(iter(data["query"]["pages"].values()))
        if "missing" in page_data:
            return None
        return page_data["revisions"][0]["slots"]["main"]["*"]
    except (KeyError, IndexError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "pages",
        nargs="*",
        help="Worksheet page names to fetch (default: all known worksheets)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"MediaWiki API URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing .wikitext files (default: skip existing)",
    )
    args = parser.parse_args()

    pages = args.pages or WORKSHEET_LIST
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    fetched = skipped = failed = 0
    for page in pages:
        dest = output_dir / f"{page}.wikitext"
        rel = dest.relative_to(REPO_ROOT) if dest.is_relative_to(REPO_ROOT) else dest
        if dest.exists() and not args.force:
            print(f"skip {rel} (exists; use --force to overwrite)")
            skipped += 1
            continue

        print(f"fetch {page}…")
        source = fetch_page_source(args.api_url, page)
        if source is None:
            print(f"  failed: no source for {page}", file=sys.stderr)
            failed += 1
            continue

        if not source.endswith("\n"):
            source += "\n"
        dest.write_text(source, encoding="utf-8")
        print(f"wrote {rel} ({len(source)} chars)")
        fetched += 1

    print(f"done: {fetched} fetched, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
