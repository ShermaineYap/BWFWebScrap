#!/usr/bin/env python3
"""Check a generated BWF CSV file."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    args = parser.parse_args()

    df = pd.read_csv(args.csv)
    doubles = df[df["discipline"].isin(["MD", "WD", "XD"])].copy()

    team1_duplicate = doubles[
        doubles["team1_player1_id"].notna()
        & (doubles["team1_player1_id"] == doubles["team1_player2_id"])
    ]
    team2_duplicate = doubles[
        doubles["team2_player1_id"].notna()
        & (doubles["team2_player1_id"] == doubles["team2_player2_id"])
    ]
    duplicate_match_ids = df[df.duplicated("match_id", keep=False)]

    print(f"Rows: {len(df):,}")
    print("By discipline:")
    print(df["discipline"].value_counts().sort_index().to_string())
    print(f"Doubles rows: {len(doubles):,}")
    print(f"Team 1 duplicate partner IDs: {len(team1_duplicate):,}")
    print(f"Team 2 duplicate partner IDs: {len(team2_duplicate):,}")
    print(f"Duplicate match IDs: {len(duplicate_match_ids):,}")
    print(f"Date range: {df['date'].min()} to {df['date'].max()}")


if __name__ == "__main__":
    main()
