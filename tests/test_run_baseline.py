import unittest

import numpy as np

from types import SimpleNamespace

from nbs_agents.cli.core import Beamline


class Arr:
    def __init__(self, values):
        self.values = values

    def read(self):
        return np.asarray(self.values)


BL = SimpleNamespace(config=SimpleNamespace(baseline_keys=["ring_current", "es_pressure"]))


class TestRunBaseline(unittest.TestCase):
    def test_start_and_end_ring_current(self):
        run = {"baseline": {"ring_current": Arr([480.123, 0.0]), "other": Arr([1, 2]),
                            "es_pressure": Arr([1.23456e-8, 2e-6])}}
        self.assertEqual(Beamline.run_baseline(BL, run),
                         {"ring_current": [480.1, 0.0], "es_pressure": [1.235e-8, 2e-6]})

    def test_missing_or_broken_baseline_is_skipped(self):
        self.assertEqual(Beamline.run_baseline(BL, {}), {})
        self.assertEqual(Beamline.run_baseline(BL, {"baseline": {}}), {})
        self.assertEqual(Beamline.run_baseline(BL, {"baseline": {"ring_current": None}}), {})


if __name__ == "__main__":
    unittest.main()
