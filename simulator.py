"""Talking Machines — bottling-line simulator (5 machines + a palletising cobot).

  python simulator.py --fast              write a whole run to data/stream.csv at once
  python simulator.py --speed 20          live run written to data/stream.csv
  python simulator.py --plc --speed 20    act as a PLC: serve the live values over Modbus TCP (port 5020)
                                          and let collector.py read them (this is how a real plant works)

The answer key (what every tag really is, and when things go wrong) goes to data/answers.json.
"""
import argparse
import csv
import json
import math
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

# the palletising cobot at the end of the line
COBOT = [("running", "bool", ""), ("fault", "bool", ""), ("gripper_closed", "bool", ""),
         ("zone_occupied", "bool", ""), ("cycle_count", "int", "cases"), ("tcp_speed", "real", "mm/s"),
         ("speed_override", "real", "%")] + [(f"j{i}_torque", "real", "Nm") for i in range(1, 7)]
GRAVITY = {1: 12, 2: 88, 3: 46, 4: 9, 5: 6, 6: 3}          # holding torque per joint (shoulder carries most)
PAYLOAD = {1: 3, 2: 18, 3: 10, 4: 2, 5: 1.5, 6: 1}          # extra torque while carrying a case


def timeline(seed):
    """When things happen in this run. Different for every seed, always in this order:
    healthy start (so normal can be learned) -> capper jams -> filler bearing wears -> filler fails.
    People also step into the cobot's safety zone 2-3 times."""
    r = random.Random(seed * 7919 + 1)
    jam_start = r.randint(8, 11) * 60
    jam = (jam_start, jam_start + r.randint(5, 7) * 60)
    n = r.randint(3, 5)                                   # always 3-5 short capper jams in that window
    slot = (jam[1] - jam[0]) // n
    jams = [(jam[0] + i * slot + r.randint(0, max(slot - 40, 1)), r.randint(15, 35)) for i in range(n)]
    wear = r.randint(18, 24) * 60
    fail = wear + r.randint(12, 17) * 60
    k = r.randint(2, 3)                                   # safety-zone visits, between minute 9 and 30
    zslot = (30 - 9) * 60 // k
    zones = [(9 * 60 + i * zslot + r.randint(0, zslot - 100), r.randint(40, 90)) for i in range(k)]
    return jam, jams, wear, fail, zones


def make_tags(rng, seed):
    """Give every signal a cryptic name; each machine gets one vendor style, like a real mixed plant."""
    styles = ["siemens", "ab", "legacy"]
    tags = {}
    for mi, m in enumerate(MACHINES, 1):
        style = rng.choice(styles)
        db = 100 + mi
        for si, (sig, kind, unit) in enumerate(COMMON + EXTRA[m]):
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
    # the robot controller names its I/O by number only; the numbering is shuffled per seed
    r2 = random.Random(seed + 17)
    di, ai = r2.sample(range(4), 4), r2.sample(range(8), 8)
    for sig, kind, unit in COBOT:
        if kind == "bool":
            name = f"ROB1.DI{di.pop()}"
        elif kind == "int":
            name = "ROB1.CNT0"
        else:
            name = f"ROB1.AI{ai.pop()}"
        tags[("cobot", sig)] = {"tag": name, "machine": "cobot", "signal": sig, "kind": kind, "unit": unit,
                                "vendor_style": "robot"}
    return tags


def simulate(minutes, seed):
    rng = random.Random(seed)
    tags = make_tags(rng, seed)
    JAM, JAMS, WEAR, FAIL, ZONES = timeline(seed)
    st = {m: {"temp": 38.0, "count": 0.0, "cases": 0.0, "layers": 0.0} for m in MACHINES}
    cb = {"buffer": 0.0, "done": 0.0, "phase": 0.0}
    for t in range(minutes * 60):
        wear = min(1.0, max(0.0, (t - WEAR) / (FAIL - WEAR)))
        filler_failed = t >= FAIL
        capper_jammed = any(s0 <= t < s0 + d for s0, d in JAMS)
        line_down = filler_failed
        row = {}
        case_rate = 0.0
        for m in MACHINES:
            s = st[m]
            running = not line_down
            fault = False
            if m == "filler" and filler_failed:
                fault = True
            if m == "capper" and capper_jammed:
                running, fault = False, True
            if m == "filler" and capper_jammed:
                speed_target = 120.0                    # blocked: capper isn't taking bottles
            elif m in ("labeller", "case_packer") and capper_jammed:
                speed_target = 60.0                     # starved downstream
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
                case_rate = speed / 60 / 24                      # cases per second sent to the cobot
                s["cases"] += case_rate
                vals["case_count"] = int(s["cases"])
            if m == "depalletiser":
                s["layers"] += speed / 60 / 120
                vals["layer_count"] = int(s["layers"])
            for sig, v in vals.items():
                row[tags[(m, sig)]["tag"]] = v

        # ── cobot: palletises the cases; slows to collaborative speed when a person is near ──
        zone = any(s0 <= t < s0 + d for s0, d in ZONES)
        override = 30.0 if zone else 100.0
        cb["buffer"] += case_rate
        capacity = (15 / 60) * override / 100                   # cases per second it can place
        working = cb["buffer"] > 0.01                            # something to palletise (stops when line is down)
        moved = min(cb["buffer"], capacity) if working else 0.0
        cb["buffer"] -= moved
        cb["done"] += moved
        cb["phase"] = (cb["phase"] + (moved / (15 / 60)) * 0.22) % 1.0 if working else cb["phase"]
        gripper = working and 0.2 <= cb["phase"] < 0.7
        motion = math.sin(2 * math.pi * cb["phase"])
        tcp = (1000 * override / 100 * (0.75 + 0.25 * abs(motion)) + rng.gauss(0, 12)) if working else 0.0
        cv = {"running": int(working), "fault": 0, "gripper_closed": int(gripper), "zone_occupied": int(zone),
              "cycle_count": int(cb["done"]), "tcp_speed": round(max(tcp, 0), 1),
              "speed_override": override}
        for j in range(1, 7):
            dyn = (0.25 * GRAVITY[j] * motion * override / 100) if working else 0.0
            tq = GRAVITY[j] + (PAYLOAD[j] if gripper else 0) + dyn + rng.gauss(0, 0.03 * GRAVITY[j] + 0.1)
            cv[f"j{j}_torque"] = round(tq, 2)
        for sig, v in cv.items():
            row[tags[("cobot", sig)]["tag"]] = v
        yield t, row, tags


def write_answers(tags, seed, start, path="data/answers.json"):
    import plc_programs
    plc_programs.render(tags)                                   # the machines' PLC code, with this run's tags
    JAM, JAMS, WEAR, FAIL, ZONES = timeline(seed)
    at = lambda s: str(start + timedelta(seconds=s))
    key = {v["tag"]: {k: v[k] for k in ("machine", "signal", "kind", "unit", "vendor_style")} for v in tags.values()}
    with open(path, "w", encoding="utf-8") as f:              # the decoder person must NOT open this
        json.dump({"tags": key, "events": {
            "capper_jams": [at(JAM[0]), at(JAM[1])],
            "filler_wear_starts": at(WEAR), "filler_fails": at(FAIL),
            "cobot_zone_visits": [[at(s0), at(s0 + d)] for s0, d in ZONES]}}, f, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=60, help="simulated seconds per real second")
    ap.add_argument("--minutes", type=int, default=45)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--fast", action="store_true", help="no waiting: write the whole run at once")
    ap.add_argument("--plc", action="store_true", help="serve the values over Modbus TCP instead of a file")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5020)
    a = ap.parse_args()
    os.makedirs("data", exist_ok=True)
    start = datetime(2026, 11, 27, 9, 0, 0)
    out = writer = tags = cols = server = regmap = None
    try:
        for t, row, tags in simulate(a.minutes, a.seed):
            if cols is None:
                cols = list(row)
                random.Random(a.seed).shuffle(cols)            # column/register order changes with the seed
                write_answers(tags, a.seed, start)
                if a.plc:
                    import plc
                    kinds = {v["tag"]: v["kind"] for v in tags.values()}
                    regmap = plc.build_map(cols, kinds)
                    plc.save_map(regmap)
                    server = plc.PLCServer(plc.map_size(regmap), a.host, a.port)
                    print(f"PLC ready on {a.host}:{a.port} (Modbus TCP, read-only) · {len(cols)} tags")
                else:
                    out = open("data/stream.csv", "w", newline="")
                    writer = csv.writer(out)
                    writer.writerow(["timestamp"] + cols)
            if a.plc:
                server.update(plc.encode(row, regmap, t, plc.map_size(regmap)))
            else:
                writer.writerow([str(start + timedelta(seconds=t))] + [row[c] for c in cols])
            if not a.fast:
                if out:
                    out.flush()
                time.sleep(1 / a.speed)
                if t % 60 == 0:
                    print(f"sim minute {t // 60}")
        if a.plc:                                              # keep serving the last values until stopped
            print("run finished; PLC keeps serving the final values (Ctrl+C to stop)")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.close()
        if server:
            server.close()
    print(f"done ({len(tags or {})} signals)")


if __name__ == "__main__":
    main()