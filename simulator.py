import argparse
import csv
import json
import os
import random
import time
from datetime import datetime, timedelta

MACHINES = ["depalletiser", "filler", "capper", "labeller", "case_packer"]
COMMON = [
    ("running", "bool", ""), ("fault", "bool", ""), ("speed", "real", "bottles/min"),
    ("count", "int", "bottles"), ("motor_current", "real", "A"), ("motor_temp", "real", "°C"),
    ("vibration", "real", "mm/s"),
]
EXTRA = {"filler": [("fill_pressure", "real", "bar")], "capper": [("cap_torque", "real", "Nm")],
         "labeller": [("glue_temp", "real", "°C")], "case_packer": [("case_count", "int", "cases")],
         "depalletiser": [("layer_count", "int", "layers")]}
ABBR = {"running": "RUN", "fault": "FLT", "speed": "SPD", "count": "CNT", "motor_current": "I_AMP",
        "motor_temp": "T_MOT", "vibration": "VIB", "fill_pressure": "P_FILL", "cap_torque": "TRQ",
        "glue_temp": "T_GLUE", "case_count": "CASE", "layer_count": "LYR"}



def timeline(seed):
    """When things go wrong in this run. Different for every seed, always in this order:
    healthy start (so the baseline can be learned) -> capper jams -> filler bearing wears -> filler fails."""
    r = random.Random(seed * 7919 + 1)
    jam_start = r.randint(8, 11) * 60
    jam = (jam_start, jam_start + r.randint(5, 7) * 60)
    n = r.randint(3, 5)                                   # always 3-5 short capper jams in that window
    slot = (jam[1] - jam[0]) // n
    jams = [(jam[0] + i * slot + r.randint(0, max(slot - 40, 1)), r.randint(15, 35)) for i in range(n)]
    wear = r.randint(18, 24) * 60
    fail = wear + r.randint(12, 17) * 60
    return jam, jams, wear, fail


def make_tags(rng):
    """Give every signal a cryptic name; each machine gets one vendor style, like a real mixed plant."""
    styles = ["siemens", "ab", "legacy"]
    tags, n = {}, {"db": 100, "F": 8, "N": 7, "B": 3}
    for mi, m in enumerate(MACHINES, 1):
        style = rng.choice(styles)
        db = n["db"] + mi
        sigs = COMMON + EXTRA[m]
        for si, (sig, kind, unit) in enumerate(sigs):
            if style == "siemens":
                name = (f"DB{db}.DBX{si // 8}.{si % 8}" if kind == "bool" else
                        f"DB{db}.DBW{si * 2}" if kind == "int" else f"DB{db}.DBD{si * 4}")
            elif style == "ab":
                name = (f"B3:{mi}/{si}" if kind == "bool" else f"N7:{mi * 10 + si}" if kind == "int"
                        else f"F8:{mi * 10 + si}")
            else:
                name = f"M{mi}_{ABBR[sig]}"
            tags[(m, sig)] = {"tag": name, "machine": m, "signal": sig, "kind": kind, "unit": unit,
                              "vendor_style": style}
    return tags


def simulate(minutes, seed):
    rng = random.Random(seed)
    tags = make_tags(rng)
    JAM, JAMS, WEAR, FAIL = timeline(seed)
    st = {m: {"temp": 38.0, "count": 0.0, "cases": 0.0, "layers": 0.0, "jam_left": 0} for m in MACHINES}
    for t in range(minutes * 60):
        wear = min(1.0, max(0.0, (t - WEAR) / (FAIL - WEAR)))
        filler_failed = t >= FAIL
        capper_jammed = any(s0 <= t < s0 + d for s0, d in JAMS)
        line_down = filler_failed
        row = {}
        for m in MACHINES:
            s = st[m]
            running = not line_down
            fault = False
            if m == "filler" and filler_failed:
                fault = True
            if m == "capper" and capper_jammed:
                running, fault = False, True
            if m == "filler" and capper_jammed:
                speed_target = 120.0                    
            elif m in ("labeller", "case_packer") and capper_jammed:
                speed_target = 60.0                     
            else:
                speed_target = 300.0
            speed = (speed_target + rng.gauss(0, 3)) if running else 0.0
            load = speed / 300.0
            extra_i = 3.0 * wear if m == "filler" else 0.0
            current = (2.0 + 9.0 * load + extra_i + rng.gauss(0, 0.15)) if running else rng.gauss(0.3, 0.05)
            temp_target = 35 + 25 * load + (28 * wear if m == "filler" else 0)
            s["temp"] += (temp_target - s["temp"]) / 240 + rng.gauss(0, 0.05)
            vib = (1.4 + 0.6 * load + (6 * wear ** 1.5 if m == "filler" else 0) + abs(rng.gauss(0, 0.2))) \
                if running else abs(rng.gauss(0.1, 0.05))
            s["count"] += speed / 60
            vals = {"running": int(running), "fault": int(fault), "speed": round(speed, 1),
                    "count": int(s["count"]), "motor_current": round(max(current, 0), 2),
                    "motor_temp": round(s["temp"], 2), "vibration": round(vib, 2)}
            if m == "filler":
                vals["fill_pressure"] = round((2.5 + rng.gauss(0, 0.03)) if running else 0.2, 3)
            if m == "capper":
                vals["cap_torque"] = round((1.2 + rng.gauss(0, 0.04)) if running else 0.0, 3)
            if m == "labeller":
                vals["glue_temp"] = round(165 + rng.gauss(0, 0.6), 2)
            if m == "case_packer":
                s["cases"] += speed / 60 / 24
                vals["case_count"] = int(s["cases"])
            if m == "depalletiser":
                s["layers"] += speed / 60 / 120
                vals["layer_count"] = int(s["layers"])
            for sig, v in vals.items():
                row[tags[(m, sig)]["tag"]] = v
        yield t, row, tags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=60, help="simulated seconds per real second")
    ap.add_argument("--minutes", type=int, default=45)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--fast", action="store_true", help="no waiting: write the whole run at once")
    a = ap.parse_args()
    os.makedirs("data", exist_ok=True)
    start = datetime(2026, 11, 27, 9, 0, 0)
    out, writer, tags = None, None, None
    try:
        for t, row, tags in simulate(a.minutes, a.seed):
            if writer is None:
                out = open("data/stream.csv", "w", newline="")
                writer = csv.writer(out)
                cols = list(row)
                random.Random(a.seed).shuffle(cols)            
                writer.writerow(["timestamp"] + cols)
                JAM, JAMS, WEAR, FAIL = timeline(a.seed)
                key = {v["tag"]: {k: v[k] for k in ("machine", "signal", "kind", "unit", "vendor_style")}
                       for v in tags.values()}
                with open("data/answers.json", "w") as f:   
                    json.dump({"tags": key, "events": {
                        "capper_jams": [str(start + timedelta(seconds=JAM[0])),
                                        str(start + timedelta(seconds=JAM[1]))],
                        "filler_wear_starts": str(start + timedelta(seconds=WEAR)),
                        "filler_fails": str(start + timedelta(seconds=FAIL))}}, f, indent=1)
            writer.writerow([str(start + timedelta(seconds=t))] + [row[c] for c in cols])
            if not a.fast:
                out.flush()
                time.sleep(1 / a.speed)
                if t % 60 == 0:
                    print(f"sim minute {t // 60}")
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.close()
    print(f"done: data/stream.csv ({len(tags)} signals)")


if __name__ == "__main__":
    main()