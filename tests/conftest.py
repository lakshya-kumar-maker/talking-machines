"""Shared fixtures: simulated runs written to a temporary folder (never to the project's data/)."""

import csv
import json
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import decoder  # noqa: E402
import simulator  # noqa: E402

START = datetime(2026, 11, 27, 9, 0, 0)  # the same start time simulator.main() uses


def _write_run(folder: Path, seed: int, minutes: int = 45) -> None:
    """What `python simulator.py --fast --seed N` does, but into folder/data/."""
    old = os.getcwd()
    os.chdir(folder)  # write_answers() and plc_programs.render() use relative data/ paths
    try:
        os.makedirs("data", exist_ok=True)
        simulator.CALM = False
        events, cols = {}, None
        with open("data/stream.csv", "w", newline="") as out:
            writer = csv.writer(out)
            for t, row, tags in simulator.simulate(minutes, seed, None, events, False):
                if cols is None:
                    cols = list(row)
                    random.Random(seed).shuffle(cols)  # column order changes with the seed, as in main()
                    simulator.write_answers(tags, seed, START)
                    writer.writerow(["timestamp"] + cols)
                writer.writerow([str(START + timedelta(seconds=t))] + [row[c] for c in cols])
        if "filler_trip" in events:  # main() records the real moment the PLC rule tripped
            with open("data/answers.json", encoding="utf-8") as f:
                ans = json.load(f)
            ans["events"]["filler_fails"] = str(START + timedelta(seconds=events["filler_trip"]))
            with open("data/answers.json", "w", encoding="utf-8") as f:
                json.dump(ans, f, indent=1)
    finally:
        os.chdir(old)


class Run:
    """One simulated run on disk, with its data and decoded tags loaded lazily."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.csv = folder / "data" / "stream.csv"
        self.answers = folder / "data" / "answers.json"
        self.programs = folder / "data" / "plc_programs"
        self._df = self._dec = None

    @property
    def df(self) -> pd.DataFrame:
        if self._df is None:
            self._df = pd.read_csv(self.csv, parse_dates=["timestamp"])
        return self._df

    @property
    def dec(self) -> dict:
        if self._dec is None:
            self._dec, _ = decoder.decode(self.df)
        return self._dec

    def answer_key(self) -> dict:
        with open(self.answers, encoding="utf-8") as f:
            return json.load(f)


@pytest.fixture(scope="session")
def make_run(tmp_path_factory):
    """make_run(seed) -> Run; each seed is simulated once per test session."""
    cache = {}

    def get(seed: int) -> Run:
        if seed not in cache:
            folder = tmp_path_factory.mktemp(f"seed{seed}")
            _write_run(folder, seed)
            cache[seed] = Run(folder)
        return cache[seed]

    return get
