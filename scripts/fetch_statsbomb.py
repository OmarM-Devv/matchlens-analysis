"""Download one team's season from StatsBomb open data into data/statsbomb.

    python scripts/fetch_statsbomb.py                  # Leicester City, Premier League 2015/16
    python scripts/fetch_statsbomb.py --competition-id 2 --season-id 27 --team-id 22 --out data/statsbomb

Writes the full season match list (the loader filters it to the team) and the
event file of every match the team played, about 105 MB for Leicester's 38.

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
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as response:
        tmp.write_bytes(response.read())
    tmp.replace(dest)  # never leave a truncated file under the final name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--competition-id", type=int, default=2)
    parser.add_argument("--season-id", type=int, default=27)
    parser.add_argument("--team-id", type=int, default=22)
    parser.add_argument("--out", type=Path, default=Path("data/statsbomb"))
    parser.add_argument("--force", action="store_true", help="re-download files that already exist")
    args = parser.parse_args()

    matches_path = args.out / "matches" / str(args.competition_id) / f"{args.season_id}.json"
    fetch(f"{BASE_URL}/matches/{args.competition_id}/{args.season_id}.json", matches_path)
    season = json.loads(matches_path.read_text(encoding="utf-8"))
    match_ids = [
        m["match_id"]
        for m in season
        if args.team_id in (m["home_team"]["home_team_id"], m["away_team"]["away_team_id"])
    ]
    print(f"{len(season)} matches in season, {len(match_ids)} involve team {args.team_id}")

    for n, match_id in enumerate(match_ids, 1):
        dest = args.out / "events" / f"{match_id}.json"
        if dest.exists() and not args.force:
            continue
        fetch(f"{BASE_URL}/events/{match_id}.json", dest)
        print(f"[{n}/{len(match_ids)}] events {match_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
