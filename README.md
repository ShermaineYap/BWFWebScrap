# BWF Match Data Scraper

This repository contains the Python script and notebook used to collect BWF match results for my final year project.

The scraper reads tournament and match data from the JSON endpoints used by the BWF tournament website. It stores the raw responses, converts them into CSV format, and checks the output for common data problems such as duplicate match IDs and duplicated doubles partners.

## Files

- `bwf_scraper_2021_2026.py` - main scraping and data-cleaning script
- `BWF_Scraper_2021_2026.ipynb` - notebook for running and checking the scraper
- `inspect_results.py` - quick validation script for a generated CSV file
- `requirements.txt` - Python packages used by the project
- `data/bwf_matches_2021_2025.csv` - collected match dataset used for analysis

## Included dataset

The CSV file in the `data` folder contains:

- 26,341 match records
- match dates from 12 January 2021 to 21 December 2025
- men's singles, women's singles, men's doubles, women's doubles, and mixed doubles
- tournament, round, player, country, winner, game score, and match source fields

The scraper can also be run for 2026, but the included CSV currently ends in 2025.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

For Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run the scraper

A small one-year test:

```bash
python bwf_scraper_2021_2026.py \
  --start-year 2025 \
  --end-year 2025 \
  --output-dir bwf_test_2025
```

Full collection from 2021 to 2026:

```bash
python bwf_scraper_2021_2026.py \
  --start-year 2021 \
  --end-year 2026 \
  --scope world-tour \
  --output-dir bwf_data_2021_2026
```

The default `world-tour` scope includes Super 100, 300, 500, 750, 1000, and World Tour Finals tournaments.

## Main output

The main CSV is saved inside the selected output folder:

```text
<output-folder>/clean/bwf_matches_<start-year>_<end-year>.csv
```

Separate CSV files are also created for MS, WS, MD, WD, and XD.

For doubles matches, each player is stored in a separate column:

- `team1_player1` and `team1_player2`
- `team2_player1` and `team2_player2`

## Check the output

```bash
python inspect_results.py data/bwf_matches_2021_2025.csv
```

This prints the row count, discipline counts, date range, duplicate match IDs, and duplicated doubles partner IDs.

## Updating the data

Use `--refresh-index` to request the latest tournament list:

```bash
python bwf_scraper_2021_2026.py \
  --start-year 2021 \
  --end-year 2026 \
  --refresh-index
```

The endpoint used by the BWF website is not formally documented and may change. Requests should be kept at a reasonable rate, and the source website's terms should be reviewed before redistributing the data.
