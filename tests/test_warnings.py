"""Early warnings: the same lines from a DataFrame or a CSV path, heater drops included."""

import csv
import random
from datetime import timedelta

import pandas as pd
from conftest import START

import decoder
import line
import simulator


def _glue_heater_run(path, seed=42, stop_min=17):
    """Seed 42 with the glue heater failing at 09:12, written to `path` up to minute `stop_min`."""
    cols = None
    with open(path, "w", newline="") as out:
        writer = csv.writer(out)
        inject = lambda t: ["glue_heater"] if t == 720 else []  # noqa: E731
        for t, row, _ in simulator.simulate(45, seed, inject=inject):
            if t >= stop_min * 60:
                break
            if cols is None:
                cols = list(row)
                random.Random(seed).shuffle(cols)
                writer.writerow(["timestamp"] + cols)
            writer.writerow([str(START + timedelta(seconds=t))] + [row[c] for c in cols])


def test_heater_warning_from_dataframe_and_path(tmp_path, monkeypatch):
    csv_path = tmp_path / "stream.csv"
    _glue_heater_run(csv_path)
    monkeypatch.setattr(line, "SNAP", str(tmp_path / "no_snapshot.csv"))  # no snapshot file to fall back on

    df = pd.read_csv(csv_path, parse_dates=["timestamp"])
    dec, _ = decoder.decode(df)

    from_df = line.warning_lines(df, dec)
    assert any("glue heater temperature down" in w for w in from_df), from_df
    assert from_df == line.warning_lines(str(csv_path), dec)
