"""PLC Structured Text is translated into the right English and the right trip limits."""

import translator


def test_filler_program_mentions_its_trip_rule(make_run):
    run = make_run(42)
    progs = translator.translate_all(str(run.programs), run.dec)
    filler = [t for t in progs.values() if t["machine"] == "filler"]
    assert len(filler) == 1
    text = " ".join(r["english"] for r in filler[0]["rules"])
    assert "vibration is above 7.2 mm/s for 10 seconds" in text


def test_trip_limits(make_run):
    run = make_run(42)
    limits = translator.trip_limits(run.dec, str(run.programs))

    filler = limits["filler"]
    assert any(lim["op"] == ">" and lim["value"] == 7.2 and lim["hold_s"] == 10 for lim in filler)

    capper = limits["capper"]
    assert any(lim["op"] == "<" and lim["value"] == 0.8 and lim["hold_s"] == 2 for lim in capper)

    labeller = limits["labeller"]
    assert any(lim["value"] == 150 for lim in labeller)
