"""Download EIA spot prices and calculate a transparent 3:2:1 crack spread.

The standard 3:2:1 crack spread is a market proxy, not a full refinery P&L:
it excludes operating costs, crude quality effects, hedging, transport, credits,
and the value of products other than gasoline and diesel.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import requests
from dotenv import load_dotenv
from plotly.subplots import make_subplots


API_BASE_URL = "https://api.eia.gov/v2"
DEFAULT_ROUTE = "petroleum/pri/spt"
DEFAULT_START_DATE = "2015-01-01"
GALLONS_PER_BARREL = 42
PAGE_SIZE = 5_000  # The EIA JSON API caps each response at 5,000 rows.
REQUEST_TIMEOUT_SECONDS = 30
EIA_BROWSER_URL = "https://www.eia.gov/opendata/browser/petroleum/pri/spt"
MONITORING_LOOKBACK_DAYS = 90
MONITORING_Z_THRESHOLD = 2.0
MONITORING_FORWARD_DAYS = 20


class EIADataError(RuntimeError):
    """Raised when the EIA API cannot provide a requested series."""


@dataclass(frozen=True)
class SeriesDefinition:
    """Metadata used to request and validate one EIA spot-price series."""

    series_id: str
    column: str
    expected_unit: str


SERIES = {
    "wti": SeriesDefinition("RWTC", "wti_usd_per_bbl", "$/BBL"),
    "gasoline": SeriesDefinition(
        "EER_EPMRU_PF4_Y35NY_DPG", "gasoline_usd_per_gal", "$/GAL"
    ),
    "diesel": SeriesDefinition(
        "EER_EPD2DXL0_PF4_Y35NY_DPG", "diesel_usd_per_gal", "$/GAL"
    ),
}

# The selected stretch goal. Each yield set sums to 1.0; "other" represents
# products not priced explicitly by this tracker (e.g., jet fuel, LPG, fuel oil).
# Its assumed netback is deliberately visible rather than hidden in a black box.
REFINERY_CONFIGURATIONS = {
    "simple_refinery_gross_margin_usd_per_bbl": {
        "label": "Simple refinery model",
        "gasoline_yield": 0.40,
        "diesel_yield": 0.25,
        "other_yield": 0.35,
        "other_product_netback_ratio_to_wti": 0.70,
    },
    "complex_refinery_gross_margin_usd_per_bbl": {
        "label": "Complex refinery model",
        "gasoline_yield": 0.48,
        "diesel_yield": 0.32,
        "other_yield": 0.20,
        "other_product_netback_ratio_to_wti": 0.85,
    },
}


def _resolve_api_key(api_key: str | None) -> str:
    """Return the supplied key or EIA_API_KEY from the environment."""

    key = api_key or os.getenv("EIA_API_KEY")
    if not key:
        raise EIADataError(
            "No EIA API key found. Copy .env.example to .env and set EIA_API_KEY, "
            "or pass --api-key. Register at https://www.eia.gov/opendata/."
        )
    return key


def _eia_failure_message(series_id: str, reason: str) -> str:
    """Create an actionable message for EIA API changes or empty responses."""

    return (
        f"Could not retrieve EIA series '{series_id}': {reason}. "
        f"EIA sometimes renames or retires series; confirm the route and ID in "
        f"the EIA API browser: {EIA_BROWSER_URL}"
    )


def fetch_series(
    route: str,
    series_id: str,
    start_date: str,
    *,
    api_key: str | None = None,
    expected_unit: str | None = None,
) -> pd.Series:
    """Fetch all daily observations for one EIA series, including every page.

    Parameters
    ----------
    route:
        EIA APIv2 route, for example ``petroleum/pri/spt``.
    series_id:
        The EIA series facet to fetch.
    start_date:
        Inclusive ISO date (``YYYY-MM-DD``).
    api_key:
        Optional key; defaults to ``EIA_API_KEY`` in the environment.
    expected_unit:
        Optional EIA unit guardrail (for example ``$/GAL``). A mismatch is
        treated as a data-contract change instead of silently mispricing it.

    Returns
    -------
    pandas.Series
        Numeric values indexed by a sorted, de-duplicated ``date`` index.
    """

    key = _resolve_api_key(api_key)
    endpoint = f"{API_BASE_URL}/{route.strip('/')}/data/"
    rows: list[dict[str, Any]] = []
    offset = 0
    expected_total: int | None = None

    while True:
        # Tuples preserve EIA's bracketed parameter names when requests encodes
        # the query string, including the repeated series facet syntax.
        params = [
            ("api_key", key),
            ("frequency", "daily"),
            ("data[0]", "value"),
            ("facets[series][]", series_id),
            ("start", start_date),
            ("length", str(PAGE_SIZE)),
            ("offset", str(offset)),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "asc"),
        ]

        try:
            response = requests.get(
                endpoint,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise EIADataError(_eia_failure_message(series_id, str(exc))) from exc
        except ValueError as exc:
            raise EIADataError(
                _eia_failure_message(series_id, "the API returned invalid JSON")
            ) from exc

        if payload.get("error"):
            raise EIADataError(_eia_failure_message(series_id, str(payload["error"])))

        api_response = payload.get("response", {})
        page_rows = api_response.get("data", [])
        if not isinstance(page_rows, list):
            raise EIADataError(
                _eia_failure_message(series_id, "the response did not contain a data list")
            )

        if expected_total is None:
            try:
                expected_total = int(api_response.get("total", 0))
            except (TypeError, ValueError) as exc:
                raise EIADataError(
                    _eia_failure_message(series_id, "the response had an invalid total")
                ) from exc

        rows.extend(page_rows)
        if not page_rows or len(rows) >= expected_total or len(page_rows) < PAGE_SIZE:
            break
        offset += len(page_rows)

    if not rows:
        raise EIADataError(
            _eia_failure_message(
                series_id,
                f"no daily rows were returned on or after {start_date}",
            )
        )

    frame = pd.DataFrame(rows)
    required_columns = {"period", "value", "units"}
    missing_columns = required_columns - set(frame.columns)
    if missing_columns:
        raise EIADataError(
            _eia_failure_message(
                series_id, f"required fields were missing: {sorted(missing_columns)}"
            )
        )

    frame["date"] = pd.to_datetime(frame["period"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna(subset=["date", "value"])
    if frame.empty:
        raise EIADataError(_eia_failure_message(series_id, "all returned values were invalid"))

    observed_units = set(frame["units"].dropna().astype(str))
    if expected_unit and observed_units != {expected_unit}:
        raise EIADataError(
            _eia_failure_message(
                series_id,
                f"expected unit {expected_unit}, received {sorted(observed_units)}",
            )
        )

    values = (
        frame.drop_duplicates(subset="date", keep="last")
        .sort_values("date")
        .set_index("date")["value"]
        .astype(float)
    )
    values.index.name = "date"
    values.name = series_id
    return values


def build_dataset(
    api_key: str | None = None,
    start_date: str = DEFAULT_START_DATE,
    route: str = DEFAULT_ROUTE,
) -> pd.DataFrame:
    """Fetch, inner-merge, and unit-normalise the three daily price series."""

    price_series: list[pd.Series] = []
    for definition in SERIES.values():
        series = fetch_series(
            route,
            definition.series_id,
            start_date,
            api_key=api_key,
            expected_unit=definition.expected_unit,
        ).rename(definition.column)
        price_series.append(series)

    df = pd.concat(price_series, axis=1, join="inner").dropna().sort_index()
    if df.empty:
        raise EIADataError(
            "The EIA series have no overlapping non-null dates. "
            f"Check their availability in {EIA_BROWSER_URL}"
        )

    df["gasoline_usd_per_bbl"] = df["gasoline_usd_per_gal"] * GALLONS_PER_BARREL
    df["diesel_usd_per_bbl"] = df["diesel_usd_per_gal"] * GALLONS_PER_BARREL
    return df


def compute_crack_spread(df: pd.DataFrame) -> pd.DataFrame:
    """Add the conventional 3:2:1 crack spread in USD per barrel.

    ``[(2 × gasoline $/bbl) + (1 × diesel $/bbl) − (3 × WTI $/bbl)] / 3``
    """

    required = {"wti_usd_per_bbl", "gasoline_usd_per_bbl", "diesel_usd_per_bbl"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Cannot compute crack spread; missing columns: {sorted(missing)}")

    result = df.copy()
    result["crack_spread_usd_per_bbl"] = (
        2 * result["gasoline_usd_per_bbl"]
        + result["diesel_usd_per_bbl"]
        - 3 * result["wti_usd_per_bbl"]
    ) / 3
    return result


def add_zscore_monitoring_signal(
    df: pd.DataFrame,
    lookback_days: int = MONITORING_LOOKBACK_DAYS,
    z_threshold: float = MONITORING_Z_THRESHOLD,
) -> pd.DataFrame:
    """Add an explainable, prior-window z-score monitoring signal.

    A signal is true only when today's 3:2:1 spread is more than ``z_threshold``
    standard deviations above the mean of the *previous* ``lookback_days`` daily
    observations. Using a shifted window prevents today's value from influencing
    the baseline against which it is evaluated. This is a monitoring rule, not a
    trading recommendation or a forecast of a profitable return.
    """

    if "crack_spread_usd_per_bbl" not in df:
        raise ValueError("Cannot calculate monitoring signal; crack spread is missing")
    if lookback_days < 2:
        raise ValueError("lookback_days must be at least 2")
    if z_threshold <= 0:
        raise ValueError("z_threshold must be positive")

    result = df.copy()
    spread = result["crack_spread_usd_per_bbl"]
    prior_spread = spread.shift(1)
    result["spread_90d_mean_usd_per_bbl"] = prior_spread.rolling(
        lookback_days, min_periods=lookback_days
    ).mean()
    result["spread_90d_std_usd_per_bbl"] = prior_spread.rolling(
        lookback_days, min_periods=lookback_days
    ).std(ddof=0)
    result["spread_z_score"] = (
        (spread - result["spread_90d_mean_usd_per_bbl"])
        / result["spread_90d_std_usd_per_bbl"]
    )
    # A zero-volatility baseline cannot establish a meaningful z-score.
    result.loc[result["spread_90d_std_usd_per_bbl"] == 0, "spread_z_score"] = pd.NA
    result["extended_spread_signal"] = (
        result["spread_z_score"] > z_threshold
    ).fillna(False)
    result["extended_spread_episode_start"] = result[
        "extended_spread_signal"
    ] & ~result["extended_spread_signal"].shift(1, fill_value=False)
    return result


def backtest_monitoring_signal(
    df: pd.DataFrame,
    forward_days: int = MONITORING_FORWARD_DAYS,
) -> tuple[dict[str, float | int], pd.DataFrame]:
    """Describe what happened after extended-spread episodes in the sample.

    This intentionally evaluates movement rather than P&L: it reports whether
    the spread was lower and whether it moved closer to its pre-signal 90-day
    mean after ``forward_days`` subsequent observations. Consecutive signal days
    count as one episode, preventing a prolonged dislocation from being treated
    as multiple independent observations.
    """

    required = {
        "crack_spread_usd_per_bbl",
        "spread_90d_mean_usd_per_bbl",
        "spread_90d_std_usd_per_bbl",
        "spread_z_score",
        "extended_spread_signal",
        "extended_spread_episode_start",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Cannot backtest monitoring signal; missing: {sorted(missing)}")
    if forward_days < 1:
        raise ValueError("forward_days must be at least 1")

    episodes = df.loc[
        df["extended_spread_episode_start"],
        [
            "crack_spread_usd_per_bbl",
            "spread_90d_mean_usd_per_bbl",
            "spread_90d_std_usd_per_bbl",
            "spread_z_score",
        ],
    ].copy()
    episodes.index.name = "signal_date"
    episodes["forward_spread_usd_per_bbl"] = df["crack_spread_usd_per_bbl"].shift(
        -forward_days
    ).reindex(episodes.index)
    episodes["forward_change_usd_per_bbl"] = (
        episodes["forward_spread_usd_per_bbl"] - episodes["crack_spread_usd_per_bbl"]
    )
    episodes["spread_lower_after_forward_window"] = (
        episodes["forward_spread_usd_per_bbl"]
        < episodes["crack_spread_usd_per_bbl"]
    )
    episodes["closer_to_pre_signal_mean"] = (
        (episodes["forward_spread_usd_per_bbl"] - episodes["spread_90d_mean_usd_per_bbl"])
        .abs()
        < (
            episodes["crack_spread_usd_per_bbl"]
            - episodes["spread_90d_mean_usd_per_bbl"]
        ).abs()
    )
    eligible_episodes = episodes.dropna(subset=["forward_spread_usd_per_bbl"])
    baseline_observations = int(df["spread_90d_mean_usd_per_bbl"].notna().sum())
    signal_days = int(df["extended_spread_signal"].sum())
    episode_count = int(len(episodes))

    summary: dict[str, float | int] = {
        "lookback_observations": MONITORING_LOOKBACK_DAYS,
        "z_threshold": MONITORING_Z_THRESHOLD,
        "forward_observations": forward_days,
        "baseline_observations": baseline_observations,
        "signal_days": signal_days,
        "signal_day_rate_pct": (
            100 * signal_days / baseline_observations if baseline_observations else 0.0
        ),
        "episode_count": episode_count,
        "eligible_episode_count": int(len(eligible_episodes)),
        "spread_lower_after_forward_window_pct": (
            100 * eligible_episodes["spread_lower_after_forward_window"].mean()
            if len(eligible_episodes)
            else 0.0
        ),
        "closer_to_pre_signal_mean_pct": (
            100 * eligible_episodes["closer_to_pre_signal_mean"].mean()
            if len(eligible_episodes)
            else 0.0
        ),
        "median_forward_change_usd_per_bbl": (
            float(eligible_episodes["forward_change_usd_per_bbl"].median())
            if len(eligible_episodes)
            else 0.0
        ),
    }
    return summary, episodes


def add_refinery_configuration_models(df: pd.DataFrame) -> pd.DataFrame:
    """Add simple and complex gross-margin models from explicit yield mixes.

    This is a teaching model rather than a refinery-economic model. The tracker
    does not download prices for every product, so the unpriced ``other`` yield
    receives a documented netback expressed as a fraction of WTI. No energy,
    hydrogen, RIN/LCFS, freight, or fixed/variable operating costs are included.
    """

    required = {"wti_usd_per_bbl", "gasoline_usd_per_bbl", "diesel_usd_per_bbl"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Cannot model refinery configurations; missing: {sorted(missing)}")

    result = df.copy()
    for column, config in REFINERY_CONFIGURATIONS.items():
        total_yield = (
            config["gasoline_yield"]
            + config["diesel_yield"]
            + config["other_yield"]
        )
        if abs(total_yield - 1.0) > 1e-9:
            raise ValueError(f"Configuration '{column}' yields must sum to 1.0")

        other_netback = (
            result["wti_usd_per_bbl"]
            * config["other_product_netback_ratio_to_wti"]
        )
        product_revenue = (
            config["gasoline_yield"] * result["gasoline_usd_per_bbl"]
            + config["diesel_yield"] * result["diesel_usd_per_bbl"]
            + config["other_yield"] * other_netback
        )
        result[column] = product_revenue - result["wti_usd_per_bbl"]
    return result


def plot_crack_spread(df: pd.DataFrame, output_path: Path) -> None:
    """Write a self-contained dual-axis Plotly chart to ``output_path``."""

    required = {"wti_usd_per_bbl", "crack_spread_usd_per_bbl"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Cannot plot crack spread; missing columns: {sorted(missing)}")

    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Scatter(
            x=df.index,
            y=df["wti_usd_per_bbl"],
            name="WTI crude",
            line={"color": "#1f77b4", "width": 2},
            hovertemplate="%{x|%d %b %Y}<br>WTI: $%{y:.2f}/bbl<extra></extra>",
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=df.index,
            y=df["crack_spread_usd_per_bbl"],
            name="3:2:1 crack spread",
            line={"color": "#e45756", "width": 2.5},
            hovertemplate="%{x|%d %b %Y}<br>3:2:1: $%{y:.2f}/bbl<extra></extra>",
        ),
        secondary_y=True,
    )

    if "extended_spread_episode_start" in df:
        episodes = df.loc[df["extended_spread_episode_start"]]
        figure.add_trace(
            go.Scatter(
                x=episodes.index,
                y=episodes["crack_spread_usd_per_bbl"],
                name="Extended-spread monitoring episode",
                mode="markers",
                marker={"color": "#7f3c8d", "size": 10, "symbol": "triangle-up"},
                hovertemplate=(
                    "%{x|%d %b %Y}<br>Extended spread: $%{y:.2f}/bbl"
                    "<br>Monitoring threshold breached<extra></extra>"
                ),
            ),
            secondary_y=True,
        )

    configuration_colours = ["#54a24b", "#f58518"]
    for (column, config), colour in zip(
        REFINERY_CONFIGURATIONS.items(), configuration_colours, strict=True
    ):
        if column in df:
            figure.add_trace(
                go.Scatter(
                    x=df.index,
                    y=df[column],
                    name=config["label"],
                    line={"color": colour, "width": 1.5, "dash": "dot"},
                    hovertemplate=(
                        "%{x|%d %b %Y}<br>Gross model margin: "
                        "$%{y:.2f}/bbl<extra></extra>"
                    ),
                ),
                secondary_y=True,
            )

    figure.update_layout(
        title="WTI and 3:2:1 Crack Spread",
        template="plotly_white",
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
        margin={"l": 70, "r": 70, "t": 90, "b": 60},
    )
    figure.update_xaxes(title_text="Date")
    figure.update_yaxes(title_text="WTI ($/bbl)", secondary_y=False)
    figure.update_yaxes(
        title_text="Gross margin / 3:2:1 spread ($/bbl)",
        zeroline=True,
        zerolinecolor="#888888",
        secondary_y=True,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.write_html(output_path, include_plotlyjs=True, full_html=True)


def print_summary_stats(df: pd.DataFrame) -> None:
    """Print required descriptive statistics for the conventional spread."""

    spread = df["crack_spread_usd_per_bbl"]
    summary = spread.agg(["mean", "min", "max", "std"])
    print("3:2:1 crack spread summary ($/bbl)")
    for statistic, value in summary.items():
        print(f"  {statistic:>4}: {value:,.2f}")


def print_monitoring_report(summary: dict[str, float | int]) -> None:
    """Print a compact, non-financial backtest-style monitoring report."""

    print("\nRules-based extended-spread monitoring report")
    print(
        "  Rule: spread > prior "
        f"{summary['lookback_observations']}-observation mean + "
        f"{summary['z_threshold']:.1f} standard deviations"
    )
    print(
        f"  Signal days: {summary['signal_days']:,} "
        f"({summary['signal_day_rate_pct']:.2f}% of eligible observations)"
    )
    print(f"  Distinct episodes: {summary['episode_count']:,}")
    print(
        f"  {summary['forward_observations']}-observation review "
        f"({summary['eligible_episode_count']:,} episodes with full follow-up):"
    )
    print(
        "    spread lower after window: "
        f"{summary['spread_lower_after_forward_window_pct']:.1f}%"
    )
    print(
        "    closer to pre-signal mean: "
        f"{summary['closer_to_pre_signal_mean_pct']:.1f}%"
    )
    print(
        "    median spread change: "
        f"${summary['median_forward_change_usd_per_bbl']:+.2f}/bbl"
    )
    print("  Diagnostic only: this is not a trading strategy or a return backtest.")


def _iso_date(value: str) -> str:
    """Argparse validator for an ISO calendar date."""

    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD") from exc
    return value


def parse_args() -> argparse.Namespace:
    """Parse the small command-line interface."""

    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Download EIA prices and create a 3:2:1 crack-spread tracker."
    )
    parser.add_argument(
        "--start-date",
        type=_iso_date,
        default=DEFAULT_START_DATE,
        help=f"Inclusive first date, YYYY-MM-DD (default: {DEFAULT_START_DATE}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=project_root / "output",
        help="Folder for crack_spread_data.csv and crack_spread.html.",
    )
    parser.add_argument(
        "--api-key",
        help="EIA key; takes precedence over EIA_API_KEY in .env/environment.",
    )
    return parser.parse_args()


def main() -> None:
    """Run the fetch → merge → calculate → chart → export workflow."""

    load_dotenv()
    args = parse_args()
    dataset = build_dataset(api_key=args.api_key, start_date=args.start_date)
    dataset = compute_crack_spread(dataset)
    dataset = add_zscore_monitoring_signal(dataset)
    dataset = add_refinery_configuration_models(dataset)
    monitoring_summary, monitoring_episodes = backtest_monitoring_signal(dataset)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "crack_spread_data.csv"
    html_path = args.output_dir / "crack_spread.html"
    monitoring_path = args.output_dir / "monitoring_signal_episodes.csv"
    monitoring_summary_path = args.output_dir / "monitoring_backtest_summary.csv"
    dataset.reset_index().to_csv(csv_path, index=False, float_format="%.4f")
    monitoring_episodes.reset_index().to_csv(
        monitoring_path, index=False, float_format="%.4f"
    )
    pd.DataFrame([monitoring_summary]).to_csv(
        monitoring_summary_path, index=False, float_format="%.4f"
    )
    plot_crack_spread(dataset, html_path)
    print_summary_stats(dataset)
    print_monitoring_report(monitoring_summary)
    print(f"\nSaved {len(dataset):,} observations to {csv_path}")
    print(f"Saved interactive chart to {html_path}")
    print(f"Saved monitoring episodes to {monitoring_path}")
    print(f"Saved monitoring summary to {monitoring_summary_path}")


if __name__ == "__main__":
    try:
        main()
    except EIADataError as error:
        raise SystemExit(f"ERROR: {error}") from error
