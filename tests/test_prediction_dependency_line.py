"""Forecast accuracy, the learned dependency map, and rupee formatting."""

import pandas as pd

import dependency
import line
import prediction
import translator


def test_filler_trip_forecast_within_2_minutes(make_run):
    run = make_run(42)
    fail = pd.Timestamp(run.answer_key()["events"]["filler_fails"])
    cut = run.df[run.df["timestamp"] <= pd.Timestamp("2026-11-27 09:27:00")]
    limits = translator.trip_limits(run.dec, str(run.programs))
    filler = [p for p in prediction.forecasts(cut, run.dec, limits) if p["machine"] == "filler"]
    assert filler, "no forecast for the filler at 09:27"
    assert abs((filler[0]["at"] - fail).total_seconds()) <= 120


def test_capper_slows_filler_is_confirmed(make_run, monkeypatch):
    run = make_run(42)
    monkeypatch.chdir(run.folder)  # dependency.from_code reads data/plc_programs relative to the cwd
    edges = dependency.build(run.df, run.dec)
    edge = next(e for e in edges if e["source"] == "capper" and e["target"] == "filler")
    assert edge["effect"] == "slows"
    assert edge["status"] == "confirmed"


def test_rupees_indian_grouping():
    assert line.rupees(1234567) == "₹12,34,567"
