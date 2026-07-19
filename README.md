# 3:2:1 Crack Spread Tracker

A small chemical-engineering and commercial-analysis project that downloads daily EIA spot prices, converts them to consistent units, and tracks the standard **3:2:1 crack spread**.

The resulting spread is a useful proxy for the gross value of turning crude oil into transportation fuels. Refiners use margins like this to inform run rates; traders use them to compare crude and product markets. It is deliberately a proxy, not a refinery P&L: it omits crude quality, energy and hydrogen, operating costs, RIN/LCFS credits, freight, hedging, inventory, and the full product slate.

## What it calculates

The conventional 3:2:1 crack spread assumes three barrels of crude are transformed into two barrels of gasoline and one barrel of diesel:

```text
3:2:1 crack spread ($/bbl)
  = [(2 × gasoline $/bbl) + (1 × diesel $/bbl) − (3 × WTI $/bbl)] / 3
```

The EIA publishes WTI in dollars per barrel, but the New York Harbor gasoline and ultra-low-sulfur diesel (ULSD) spot prices in dollars per gallon. The script converts each product price with **1 barrel = 42 gallons** before it applies the formula.

| Input | EIA APIv2 route | Series ID | Returned unit |
|---|---|---|---|
| WTI crude | `petroleum/pri/spt` | `RWTC` | $/BBL |
| NY Harbor conventional regular gasoline | `petroleum/pri/spt` | `EER_EPMRU_PF4_Y35NY_DPG` | $/GAL |
| NY Harbor ULSD | `petroleum/pri/spt` | `EER_EPD2DXL0_PF4_Y35NY_DPG` | $/GAL |

## Included refinery configuration model

As a chemical-engineering extension, the tracker also adds two transparent, illustrative gross-margin models. A simple refinery has a 40% gasoline / 25% diesel / 35% other-product yield. A complex refinery has 48% gasoline / 32% diesel / 20% other-product yield. The unpriced `other` product bucket is given an explicit assumed netback (70% or 85% of WTI, respectively).

This lets you compare how yield mix alone can change gross value from the same input crude. These are **teaching assumptions**, not calibrated process simulations or real refinery economics; all assumptions are defined in `src/crack_spread.py`.

## Setup

1. Register for a free EIA API key at [EIA Open Data](https://www.eia.gov/opendata/).
2. Create and activate a virtual environment (recommended):

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install the dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and replace its placeholder with the key. Do not commit `.env`.

5. Run the tracker (the default start date is 2015-01-01, capturing the
   2015–16 oil-price collapse, the 2020 demand shock, and later volatility):

   ```powershell
   python src/crack_spread.py
   ```

   Or select a different history window:

   ```powershell
   python src/crack_spread.py --start-date 2018-01-01
   ```

The script prints mean/min/max/standard deviation for the conventional spread and writes:

```text
output/
├── crack_spread_data.csv
├── crack_spread.html
├── monitoring_signal_episodes.csv
└── monitoring_backtest_summary.csv
```

`crack_spread.html` is a self-contained interactive Plotly chart: WTI uses the left axis, while the 3:2:1 spread and two transparent configuration models use the right axis.

## Rules-based spread monitoring (not a trading strategy)

The tracker adds an intentionally simple monitoring signal to identify unusually wide refining margins. For each daily observation, it calculates the mean and population standard deviation of the **previous 90 trading-day observations** of the 3:2:1 spread. It then calculates:

```text
z-score = (today's spread − prior 90-observation mean) / prior 90-observation standard deviation
```

An **extended-spread monitoring signal** is flagged only when the z-score is above `+2.0`. The baseline is shifted back one day, so the day being assessed cannot influence its own threshold. Purple triangles in the chart show the first day of each consecutive signal episode. The raw signal columns are retained in `crack_spread_data.csv`; the distinct episode dates and their later observations are in `monitoring_signal_episodes.csv`.

The diagnostic review does not calculate returns, execute trades, model costs, or claim predictability. It asks only whether, 20 trading observations after each episode began, the spread was (a) lower and (b) closer to its pre-signal 90-day mean. Consecutive flag days are collapsed into a single episode so a persistent dislocation is not counted as many independent signals. The console output and `monitoring_backtest_summary.csv` report the frequency and these two descriptive outcomes.

This rule must be read cautiously. A wide spread can persist or widen further when the underlying physical shock continues. In the current 2015-to-latest sample generated on 15 July 2026, there were 238 flagged days (8.52% of eligible observations) and 65 episodes. After 20 observations, 52.3% of eligible episodes had a lower spread and 50.8% were closer to their original mean; the median spread change was -$0.28/bbl. That is not evidence of a usable trading edge—only a record of how often this monitoring condition appeared and what followed in this particular history.

### Event sense-checks

The flags align with periods that have plausible physical-market explanations, but this is an event cross-check, not a causal validation:

| Signal episode in the output | What it helps surface | What happened next |
|---|---|---|
| 24 August 2017 | The first Harvey-era flag appeared just before the storm’s 25 August landfall, consistent with markets anticipating Gulf Coast disruption. | EIA reports Gulf Coast refinery inputs fell 34% in the week ending 1 September and U.S. average gasoline rose 28¢/gal; this episode did **not** revert within 20 observations. |
| February–April 2022 | The monitor began flagging elevated spreads on 7 February and again on 24 February and through spring, around Russia’s full-scale invasion of Ukraine. | EIA reports New York Harbor ULSD rose to $4.44/gal on 8 March amid low inventories, high demand, and concern about Russian distillate exports. The early episodes continued to widen, demonstrating why a signal is not a short recommendation. |
| October–November 2022 | Further flags captured exceptional diesel margins. | EIA reports New York Harbor ULSD averaged $4.36/gal in October amid tight inventories, reduced refinery production, and winter demand; several of these later episodes did move back toward their baselines within 20 observations. |

Sources: [EIA on Hurricane Harvey](https://www.eia.gov/todayinenergy/detail.php?id=32852), [EIA on the March 2022 diesel shock](https://www.eia.gov/todayinenergy/detail.php?id=51578), and [EIA on October 2022 diesel tightness](https://www.eia.gov/todayinenergy/detail.php?id=54619).

## How the data pull is made reliable

`fetch_series(route, series_id, start_date)` requests daily values with an explicit EIA series facet and follows the API's `offset` pagination until all rows are collected (EIA caps JSON pages at 5,000 rows). The three series are inner-merged by date, so no spread is calculated from mismatched observations.

If EIA returns no data, an API error, or changes a series, the program gives a direct link to the [EIA API browser for this route](https://www.eia.gov/opendata/browser/petroleum/pri/spt). Use it to verify the current series ID and update the `SERIES` mapping if necessary.

## A real-world margin shock: Hurricane Harvey

Hurricane Harvey made landfall in Texas in August 2017 and disrupted a major refining centre. EIA reported that U.S. Gulf Coast gross refinery inputs fell 3.2 million barrels per day (34%) in the week ending September 1, with regional utilisation dropping from 96% to 63%. Restricted refinery output tightened product supply: the U.S. average regular gasoline price rose 28 cents per gallon from August 28 to September 4. This is the commercial logic behind a crack spread: if gasoline and diesel become scarcer relative to crude, product prices can rise relative to feedstock and the spread can widen. See EIA's [event analysis](https://www.eia.gov/todayinenergy/detail.php?id=32852).

The tracker uses New York Harbor product prices and Cushing WTI, so it will not reproduce a Gulf Coast refinery's exact margin. It is a consistent, explainable market benchmark for studying the mechanism.

## Tests

Run the formula and refinery-model regression tests with:

```powershell
python -m unittest discover -s tests -v
```
