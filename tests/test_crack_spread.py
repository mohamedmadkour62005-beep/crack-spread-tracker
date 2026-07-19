"""Small regression tests for the transparent calculation layer."""

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crack_spread import (  # noqa: E402
    add_refinery_configuration_models,
    add_zscore_monitoring_signal,
    backtest_monitoring_signal,
    compute_crack_spread,
    fetch_series,
)


class CrackSpreadCalculationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prices = pd.DataFrame(
            {
                "wti_usd_per_bbl": [80.0],
                "gasoline_usd_per_bbl": [120.0],
                "diesel_usd_per_bbl": [130.0],
            },
            index=pd.to_datetime(["2024-01-02"]),
        )

    def test_321_formula(self) -> None:
        result = compute_crack_spread(self.prices)
        # ((2 * 120) + 130 - (3 * 80)) / 3 = 43.33... $/bbl
        self.assertAlmostEqual(
            result.iloc[0]["crack_spread_usd_per_bbl"], 43.3333333333
        )

    def test_refinery_models_are_added(self) -> None:
        result = add_refinery_configuration_models(self.prices)
        self.assertIn("simple_refinery_gross_margin_usd_per_bbl", result.columns)
        self.assertIn("complex_refinery_gross_margin_usd_per_bbl", result.columns)
        self.assertGreater(
            result.iloc[0]["complex_refinery_gross_margin_usd_per_bbl"],
            result.iloc[0]["simple_refinery_gross_margin_usd_per_bbl"],
        )

    @patch("crack_spread.PAGE_SIZE", 2)
    @patch("crack_spread.requests.get")
    def test_fetch_series_uses_offset_pagination(self, mock_get: Mock) -> None:
        def api_page(rows: list[dict[str, str]]) -> Mock:
            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"response": {"total": "3", "data": rows}}
            return response

        mock_get.side_effect = [
            api_page(
                [
                    {"period": "2024-01-01", "value": "70", "units": "$/BBL"},
                    {"period": "2024-01-02", "value": "71", "units": "$/BBL"},
                ]
            ),
            api_page(
                [{"period": "2024-01-03", "value": "72", "units": "$/BBL"}]
            ),
        ]

        result = fetch_series(
            "petroleum/pri/spt",
            "RWTC",
            "2024-01-01",
            api_key="test-key",
            expected_unit="$/BBL",
        )

        self.assertEqual(result.tolist(), [70.0, 71.0, 72.0])
        self.assertEqual(mock_get.call_count, 2)
        second_request_params = mock_get.call_args_list[1].kwargs["params"]
        self.assertIn(("offset", "2"), second_request_params)

    def test_monitoring_rule_uses_prior_window_and_evaluates_reversion(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=120)
        # The first 90 observations have mean 10 and standard deviation 1.
        # The next reading is 14: a +4 z-score using the prior window only.
        spread = [9.0, 11.0] * 45 + [14.0] + [10.0] * 29
        monitored = add_zscore_monitoring_signal(
            pd.DataFrame({"crack_spread_usd_per_bbl": spread}, index=dates)
        )
        summary, episodes = backtest_monitoring_signal(monitored, forward_days=20)

        self.assertTrue(monitored.iloc[90]["extended_spread_signal"])
        self.assertTrue(monitored.iloc[90]["extended_spread_episode_start"])
        self.assertEqual(summary["signal_days"], 1)
        self.assertEqual(summary["episode_count"], 1)
        self.assertEqual(summary["spread_lower_after_forward_window_pct"], 100.0)
        self.assertEqual(summary["closer_to_pre_signal_mean_pct"], 100.0)
        self.assertTrue(episodes.iloc[0]["closer_to_pre_signal_mean"])


if __name__ == "__main__":
    unittest.main()
