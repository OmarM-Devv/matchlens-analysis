"""Download one competition-season from StatsBomb open data into data/statsbomb.

    python scripts/fetch_statsbomb.py                 # Premier League 2003/04
    python scripts/fetch_statsbomb.py --competition-id 2 --season-id 44 --out data/statsbomb

StatsBomb open data is free to use with attribution; see
https://github.com/statsbomb/open-data for the licence terms.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"


def fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        dest.write_bytes(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--competition-id", type=int, default=2)
    parser.add_argument("--season-id", type=int, default=44)
    parser.add_argument("--out", type=Path, default=Path("data/statsbomb"))
    parser.add_argument("--force", action="store_true", help="re-download files that already exist")
    args = parser.parse_args()

    matches_path = args.out / "matches" / str(args.competition_id) / f"{args.season_id}.json"
    fetch(f"{BASE_URL}/matches/{args.competition_id}/{args.season_id}.json", matches_path)
    match_ids = [m["match_id"] for m in json.loads(matches_path.read_text(encoding="utf-8"))]
    print(f"{len(match_ids)} matches listed")

    for n, match_id in enumerate(match_ids, 1):
        dest = args.out / "events" / f"{match_id}.json"
        if dest.exists() and not args.force:
            continue
        fetch(f"{BASE_URL}/events/{match_id}.json", dest)
        print(f"[{n}/{len(match_ids)}] events {match_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
