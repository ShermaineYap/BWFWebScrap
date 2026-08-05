#!/usr/bin/env python3
"""Collect and clean BWF individual-event match results."""

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests

API_BASE = "https://extranet-lv.bwfbadminton.com/api"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)
DISCIPLINES = {"MS", "WS", "MD", "WD", "XD"}
DOUBLES = {"MD", "WD", "XD"}
SCORE_SPAN_RE = re.compile(r"<span[^>]*>\s*(\d+)\s*</span>", re.I)
INTEGER_RE = re.compile(r"\b\d+\b")

WORLD_TOUR_CATEGORIES = {
    "BWF Tour Super 100",
    "HSBC BWF World Tour Super 300",
    "HSBC BWF World Tour Super 500",
    "HSBC BWF World Tour Super 750",
    "HSBC BWF World Tour Super 1000",
    "HSBC BWF World Tour Finals",
}
GRADE1_SKIP_WORDS = ("junior", "youth", "senior", "university")

CSV_COLUMNS = [
    "match_id",
    "date",
    "start_datetime_utc",
    "discipline",
    "tournament_id",
    "tournament",
    "tier",
    "round",
    "is_qualification",
    "host_location",
    "team1",
    "team2",
    "team1_player1_id",
    "team1_player1",
    "team1_player1_country",
    "team1_player2_id",
    "team1_player2",
    "team1_player2_country",
    "team2_player1_id",
    "team2_player1",
    "team2_player1_country",
    "team2_player2_id",
    "team2_player2",
    "team2_player2_country",
    "winner",
    "winner_team",
    "number_of_games",
    "game1_team1",
    "game1_team2",
    "game2_team1",
    "game2_team2",
    "game3_team1",
    "game3_team2",
    "score",
    "source_endpoint",
]


@dataclass(frozen=True)
class Config:
    start_year: int
    end_year: int
    output_dir: Path
    scope: str
    delay: float
    retries: int
    timeout: int
    refresh_index: bool
    refresh_recent_days: int
    include_qualification: bool


class FetchError(RuntimeError):
    pass


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    temp.replace(path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def polite_sleep(delay: float) -> None:
    time.sleep(max(0.0, delay) + random.uniform(0.0, 0.25))


def fetch_json(
    session: requests.Session,
    endpoint: str,
    params: dict[str, Any],
    *,
    retries: int,
    timeout: int,
) -> dict[str, Any]:
    url = f"{API_BASE}/{endpoint.lstrip('/')}"
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            if response.status_code == 429:
                wait = min(60, 5 * attempt)
                print(f"HTTP 429; waiting {wait}s before retry {attempt}/{retries}")
                time.sleep(wait)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise FetchError(f"Expected a JSON object from {response.url}")
            return payload
        except (requests.RequestException, ValueError, FetchError) as exc:
            last_error = exc
            if attempt == retries:
                break
            wait = min(30, 3 * attempt)
            print(f"Request failed ({exc}); retrying in {wait}s...")
            time.sleep(wait)

    raise FetchError(f"Failed to fetch {url}: {last_error}")


def parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def tournament_in_scope(tournament: dict[str, Any], scope: str) -> bool:
    category = str(tournament.get("category") or "").strip()
    name = str(tournament.get("name") or "").strip().lower()

    if scope == "all":
        return True

    category_lower = category.lower()
    is_world_tour = category in WORLD_TOUR_CATEGORIES or (
        "world tour" in category_lower
        and any(token in category_lower for token in ("super 100", "super 300", "super 500", "super 750", "super 1000", "finals"))
    ) or "tour super 100" in category_lower

    if is_world_tour:
        return True

    if scope == "elite":
        is_grade1_individual = (
            category_lower.startswith("grade 1")
            and "individual tournament" in category_lower
        )
        return is_grade1_individual and not any(word in name for word in GRADE1_SKIP_WORDS)

    return False


def fetch_tournament_index(config: Config, session: requests.Session) -> list[dict[str, Any]]:
    index_path = config.output_dir / "raw" / f"tournaments_{config.start_year}_{config.end_year}.json"
    if index_path.exists() and not config.refresh_index:
        with index_path.open(encoding="utf-8") as handle:
            tournaments = json.load(handle)
        print(f"Using cached tournament index: {len(tournaments):,} entries")
        return tournaments

    seen: set[Any] = set()
    tournaments: list[dict[str, Any]] = []

    for year in range(config.start_year, config.end_year + 1):
        page = 1
        last_page = 1
        while page <= last_page:
            payload = fetch_json(
                session,
                "vue-tournaments-search",
                {
                    "startDate": f"{year}-01-01",
                    "endDate": f"{year}-12-31",
                    "page": page,
                    "perPage": 100,
                    "drawCount": 1,
                    "activeTab": 6,
                },
                retries=config.retries,
                timeout=config.timeout,
            )
            results = payload.get("results") or {}
            if not isinstance(results, dict):
                raise FetchError(f"Unexpected tournament response for {year}, page {page}")
            last_page = int(results.get("last_page") or 1)
            rows = results.get("data") or []
            if not isinstance(rows, list):
                raise FetchError(f"Unexpected tournament list for {year}, page {page}")

            for tournament in rows:
                if not isinstance(tournament, dict):
                    continue
                tournament_id = tournament.get("id")
                if tournament_id is None or tournament_id in seen:
                    continue
                seen.add(tournament_id)
                tournaments.append(tournament)

            print(
                f"Index {year}: page {page}/{last_page}; "
                f"{len(tournaments):,} unique tournaments so far"
            )
            page += 1
            polite_sleep(config.delay)

        json_dump(index_path, tournaments)

    print(f"Tournament index saved to {index_path}")
    return tournaments


def flatten_api_matches(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Read both BWF response layouts and deduplicate by match id."""
    results = payload.get("results")
    if not isinstance(results, dict):
        return []

    matches: list[dict[str, Any]] = []
    seen: set[Any] = set()

    by_time = results.get("by_time") or {}
    if isinstance(by_time, dict):
        time_group = by_time.get("time_group") or []
        if isinstance(time_group, list):
            for match in time_group:
                if not isinstance(match, dict):
                    continue
                match_id = match.get("id")
                dedupe_key = match_id if match_id is not None else id(match)
                if dedupe_key not in seen:
                    seen.add(dedupe_key)
                    matches.append(match)

    by_court = results.get("by_court") or {}
    if isinstance(by_court, dict):
        for section in by_court.values():
            if not isinstance(section, dict):
                continue
            for match in section.values():
                if not isinstance(match, dict):
                    continue
                match_id = match.get("id")
                dedupe_key = match_id if match_id is not None else id(match)
                if dedupe_key not in seen:
                    seen.add(dedupe_key)
                    matches.append(match)

    return matches


def should_refresh_match_cache(
    tournament: dict[str, Any], cache_path: Path, refresh_recent_days: int
) -> bool:
    if not cache_path.exists():
        return True
    end_date = parse_iso_date(tournament.get("end_date"))
    if end_date is None:
        return False
    return end_date >= date.today() - timedelta(days=max(0, refresh_recent_days))


def fetch_match_payloads(
    config: Config,
    session: requests.Session,
    tournaments: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_dir = config.output_dir / "raw" / "matches"
    raw_dir.mkdir(parents=True, exist_ok=True)
    error_log = config.output_dir / "errors.jsonl"

    selected = []
    for tournament in tournaments:
        name = str(tournament.get("name") or "")
        start = parse_iso_date(tournament.get("start_date"))
        if start and not (config.start_year <= start.year <= config.end_year):
            continue
        if "cancelled" in name.lower():
            continue
        if tournament_in_scope(tournament, config.scope):
            selected.append(tournament)

    print(f"Selected {len(selected):,} tournaments for scope={config.scope!r}")
    fetched_or_cached: list[dict[str, Any]] = []

    for position, tournament in enumerate(selected, start=1):
        tournament_id = tournament.get("id")
        if tournament_id is None:
            continue
        cache_path = raw_dir / f"{tournament_id}.json.gz"
        refresh = should_refresh_match_cache(
            tournament, cache_path, config.refresh_recent_days
        )

        if not refresh:
            fetched_or_cached.append(tournament)
            continue

        params = {
            "drawCount": 0,
            "searchKey": "",
            "tmtId": tournament_id,
            "tmtType": 0,
            "isPara": "false",
        }
        try:
            payload = fetch_json(
                session,
                "vue-tournament-matches",
                params,
                retries=config.retries,
                timeout=config.timeout,
            )
            matches = flatten_api_matches(payload)
            if not matches:
                print(
                    f"[{position}/{len(selected)}] {tournament.get('name', '')[:60]}: "
                    "no matches yet; not cached"
                )
            else:
                temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
                with gzip.open(temp_path, "wt", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                temp_path.replace(cache_path)
                fetched_or_cached.append(tournament)
                print(
                    f"[{position}/{len(selected)}] {tournament.get('name', '')[:60]}: "
                    f"{len(matches):,} match objects"
                )
        except Exception as exc:
            print(f"[{position}/{len(selected)}] tournament {tournament_id} FAILED: {exc}")
            append_jsonl(
                error_log,
                {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "tournament_id": tournament_id,
                    "tournament": tournament.get("name"),
                    "error": repr(exc),
                },
            )
        polite_sleep(config.delay)

    return selected


def parse_discipline(draw_name: Any) -> tuple[str | None, bool]:
    text = str(draw_name or "").strip()
    if not text:
        return None, False
    head = text.split()[0].split("-")[0].strip().upper()
    if head not in DISCIPLINES:
        return None, False
    return head, "qual" in text.lower()


def parse_scores(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for item in value:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                pass
        return out
    if isinstance(value, dict):
        for key in ("scores", "games", "values"):
            if key in value:
                return parse_scores(value[key])
        return []

    text = str(value)
    spans = SCORE_SPAN_RE.findall(text)
    if spans:
        return [int(number) for number in spans]
    if "<" not in text and len(text) < 100:
        return [int(number) for number in INTEGER_RE.findall(text)]
    return []


def player_fields(player: Any) -> tuple[Any, str, str]:
    if not isinstance(player, dict):
        return None, "", ""
    player_id = player.get("id")
    name = str(player.get("name_display") or player.get("name") or "").strip()
    country = str(player.get("nationality") or player.get("country_code") or "").strip()
    return player_id, name, country


def epoch_to_utc(value: Any) -> tuple[str, str]:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return "", ""
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.date().isoformat(), dt.isoformat()


def match_to_row(
    match: dict[str, Any], tournament: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    discipline, is_qualification = parse_discipline(match.get("draw_name"))
    if discipline is None:
        return None, issues

    match_id = match.get("id")
    t1p1 = player_fields(match.get("t1p1_detail"))
    t1p2 = player_fields(match.get("t1p2_detail"))
    t2p1 = player_fields(match.get("t2p1_detail"))
    t2p2 = player_fields(match.get("t2p2_detail"))

    serious_context = {
        "match_id": match_id,
        "tournament_id": tournament.get("id"),
        "tournament": tournament.get("name"),
        "discipline": discipline,
    }

    required = [t1p1, t2p1]
    if discipline in DOUBLES:
        required.extend([t1p2, t2p2])
    if any(not player[0] or not player[1] for player in required):
        issues.append({**serious_context, "issue": "missing_required_player"})
        return None, issues

    if discipline in DOUBLES:
        if t1p1[0] == t1p2[0]:
            issues.append({**serious_context, "issue": "team1_duplicate_partner_id"})
        if t2p1[0] == t2p2[0]:
            issues.append({**serious_context, "issue": "team2_duplicate_partner_id"})
        if t1p1[1].casefold() == t1p2[1].casefold():
            issues.append({**serious_context, "issue": "team1_duplicate_partner_name"})
        if t2p1[1].casefold() == t2p2[1].casefold():
            issues.append({**serious_context, "issue": "team2_duplicate_partner_name"})

    winner = match.get("winner")
    try:
        winner = int(winner)
    except (TypeError, ValueError):
        winner = 0
    if winner not in (1, 2):
        issues.append({**serious_context, "issue": "missing_or_invalid_winner"})
        return None, issues

    scores1 = parse_scores(match.get("team1Score"))
    scores2 = parse_scores(match.get("team2Score"))
    if not scores1 or len(scores1) != len(scores2):
        issues.append({**serious_context, "issue": "walkover_or_invalid_score"})
        return None, issues

    date_text, datetime_text = epoch_to_utc(match.get("start_time"))
    team1_names = [t1p1[1]] + ([t1p2[1]] if discipline in DOUBLES else [])
    team2_names = [t2p1[1]] + ([t2p2[1]] if discipline in DOUBLES else [])
    team1 = " / ".join(team1_names)
    team2 = " / ".join(team2_names)

    padded1 = scores1[:3] + [None] * max(0, 3 - len(scores1))
    padded2 = scores2[:3] + [None] * max(0, 3 - len(scores2))
    source_endpoint = (
        f"{API_BASE}/vue-tournament-matches?drawCount=0&searchKey="
        f"&tmtId={tournament.get('id')}&tmtType=0&isPara=false"
    )

    row = {
        "match_id": match_id,
        "date": date_text,
        "start_datetime_utc": datetime_text,
        "discipline": discipline,
        "tournament_id": tournament.get("id"),
        "tournament": str(tournament.get("name") or "").strip(),
        "tier": str(tournament.get("category") or "").strip(),
        "round": str(match.get("round_name") or "").strip(),
        "is_qualification": is_qualification,
        "host_location": str(tournament.get("location") or "").strip(),
        "team1": team1,
        "team2": team2,
        "team1_player1_id": t1p1[0],
        "team1_player1": t1p1[1],
        "team1_player1_country": t1p1[2],
        "team1_player2_id": t1p2[0] if discipline in DOUBLES else None,
        "team1_player2": t1p2[1] if discipline in DOUBLES else "",
        "team1_player2_country": t1p2[2] if discipline in DOUBLES else "",
        "team2_player1_id": t2p1[0],
        "team2_player1": t2p1[1],
        "team2_player1_country": t2p1[2],
        "team2_player2_id": t2p2[0] if discipline in DOUBLES else None,
        "team2_player2": t2p2[1] if discipline in DOUBLES else "",
        "team2_player2_country": t2p2[2] if discipline in DOUBLES else "",
        "winner": winner,
        "winner_team": team1 if winner == 1 else team2,
        "number_of_games": len(scores1),
        "game1_team1": padded1[0],
        "game1_team2": padded2[0],
        "game2_team1": padded1[1],
        "game2_team2": padded2[1],
        "game3_team1": padded1[2],
        "game3_team2": padded2[2],
        "score": " ".join(f"{a}-{b}" for a, b in zip(scores1, scores2)),
        "source_endpoint": source_endpoint,
    }
    return row, issues


def build_dataset(
    config: Config, tournaments: list[dict[str, Any]]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    tournament_by_id = {t.get("id"): t for t in tournaments if t.get("id") is not None}
    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_match_ids: set[Any] = set()
    raw_dir = config.output_dir / "raw" / "matches"

    for cache_path in sorted(raw_dir.glob("*.json.gz")):
        try:
            tournament_id = int(cache_path.name.split(".", 1)[0])
        except ValueError:
            continue
        tournament = tournament_by_id.get(tournament_id)
        if not tournament:
            continue
        if not tournament_in_scope(tournament, config.scope):
            continue

        with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)

        for match in flatten_api_matches(payload):
            row, row_issues = match_to_row(match, tournament)
            issues.extend(row_issues)
            if row is None:
                continue
            if row["is_qualification"] and not config.include_qualification:
                continue
            match_id = row["match_id"]
            if match_id in seen_match_ids:
                issues.append(
                    {
                        "match_id": match_id,
                        "tournament_id": tournament_id,
                        "tournament": tournament.get("name"),
                        "discipline": row["discipline"],
                        "issue": "duplicate_match_id_removed",
                    }
                )
                continue
            seen_match_ids.add(match_id)
            rows.append(row)

    dataframe = pd.DataFrame(rows, columns=CSV_COLUMNS)
    if not dataframe.empty:
        dataframe["date"] = pd.to_datetime(dataframe["date"], errors="coerce")
        dataframe = dataframe[
            dataframe["date"].dt.year.between(config.start_year, config.end_year)
        ]
        dataframe = dataframe.sort_values(
            ["date", "tournament", "discipline", "round", "match_id"],
            kind="stable",
        ).reset_index(drop=True)
        dataframe["date"] = dataframe["date"].dt.strftime("%Y-%m-%d")

    issues_df = pd.DataFrame(issues)
    return dataframe, issues_df


def write_outputs(
    config: Config,
    tournaments: list[dict[str, Any]],
    dataframe: pd.DataFrame,
    issues: pd.DataFrame,
) -> None:
    clean_dir = config.output_dir / "clean"
    clean_dir.mkdir(parents=True, exist_ok=True)

    all_path = clean_dir / f"bwf_matches_{config.start_year}_{config.end_year}.csv"
    dataframe.to_csv(all_path, index=False, encoding="utf-8")

    for code in sorted(DISCIPLINES):
        path = clean_dir / f"{code.lower()}_{config.start_year}_{config.end_year}.csv"
        dataframe[dataframe["discipline"] == code].to_csv(
            path, index=False, encoding="utf-8"
        )

    tournament_rows = []
    for tournament in tournaments:
        if not tournament_in_scope(tournament, config.scope):
            continue
        tournament_rows.append(
            {
                "tournament_id": tournament.get("id"),
                "name": str(tournament.get("name") or "").strip(),
                "start_date": str(tournament.get("start_date") or "")[:10],
                "end_date": str(tournament.get("end_date") or "")[:10],
                "location": str(tournament.get("location") or "").strip(),
                "country": str(tournament.get("country") or "").strip(),
                "category": str(tournament.get("category") or "").strip(),
                "url": str(tournament.get("url") or "").strip(),
            }
        )
    pd.DataFrame(tournament_rows).to_csv(
        clean_dir / f"tournaments_{config.start_year}_{config.end_year}.csv",
        index=False,
        encoding="utf-8",
    )

    issues_path = clean_dir / "validation_issues.csv"
    issues.to_csv(issues_path, index=False, encoding="utf-8")

    serious_issue_names = {
        "missing_required_player",
        "team1_duplicate_partner_id",
        "team2_duplicate_partner_id",
        "team1_duplicate_partner_name",
        "team2_duplicate_partner_name",
        "duplicate_match_id_removed",
    }
    serious_count = (
        int(issues["issue"].isin(serious_issue_names).sum())
        if not issues.empty and "issue" in issues.columns
        else 0
    )
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "start_year": config.start_year,
        "end_year": config.end_year,
        "scope": config.scope,
        "include_qualification": config.include_qualification,
        "matches": int(len(dataframe)),
        "matches_by_discipline": {
            key: int(value)
            for key, value in dataframe["discipline"].value_counts().sort_index().items()
        }
        if not dataframe.empty
        else {},
        "validation_issue_rows": int(len(issues)),
        "serious_validation_issues": serious_count,
        "issue_counts": (
            {key: int(value) for key, value in issues["issue"].value_counts().items()}
            if not issues.empty and "issue" in issues.columns
            else {}
        ),
        "output_csv": str(all_path),
    }
    json_dump(clean_dir / "validation_summary.json", summary)

    print("\nFinished")
    print(f"  Matches: {len(dataframe):,}")
    if not dataframe.empty:
        for code, count in dataframe["discipline"].value_counts().sort_index().items():
            print(f"  {code}: {count:,}")
    print(f"  Main CSV: {all_path}")
    print(f"  Validation issues: {issues_path} ({len(issues):,} rows)")
    if serious_count:
        print(f"  WARNING: {serious_count} serious validation issue(s) found.")
    else:
        print("  Doubles partner validation: no serious duplication/missing-player issue found.")


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        description="Scrape BWF match results from the frontend JSON API."
    )
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("bwf_data_2021_2026"),
    )
    parser.add_argument(
        "--scope",
        choices=("world-tour", "elite", "all"),
        default="world-tour",
        help=(
            "world-tour = Super 100/300/500/750/1000/Finals; "
            "elite also includes senior Worlds/Olympics; all disables category filtering"
        ),
    )
    parser.add_argument("--delay", type=float, default=1.3)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--refresh-index", action="store_true")
    parser.add_argument(
        "--refresh-recent-days",
        type=int,
        default=21,
        help="Re-fetch tournaments ending within this many days of today.",
    )
    parser.add_argument(
        "--exclude-qualification",
        action="store_true",
        help="Remove qualification matches from the clean CSVs.",
    )
    args = parser.parse_args(argv)

    current_year = date.today().year
    if args.start_year < 2007:
        parser.error("start-year must be 2007 or later")
    if args.end_year < args.start_year:
        parser.error("end-year must be greater than or equal to start-year")
    if args.end_year > current_year:
        print(
            f"Note: end-year {args.end_year} is in the future relative to {current_year}; "
            "only published results can be downloaded.",
            file=sys.stderr,
        )

    return Config(
        start_year=args.start_year,
        end_year=args.end_year,
        output_dir=args.output_dir.expanduser().resolve(),
        scope=args.scope,
        delay=args.delay,
        retries=max(1, args.retries),
        timeout=max(10, args.timeout),
        refresh_index=args.refresh_index,
        refresh_recent_days=max(0, args.refresh_recent_days),
        include_qualification=not args.exclude_qualification,
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://bwfbadminton.com/",
        }
    )

    print(
        f"BWF scraper: {config.start_year}-{config.end_year}, "
        f"scope={config.scope}, output={config.output_dir}"
    )
    tournaments = fetch_tournament_index(config, session)
    fetch_match_payloads(config, session, tournaments)
    dataframe, issues = build_dataset(config, tournaments)
    write_outputs(config, tournaments, dataframe, issues)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
