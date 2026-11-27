"""Talking Machines — the plant model for running against a real soft PLC (OpenPLC).

In a real factory the PLC reads sensors through I/O modules and its program decides what the machines do.
Here this script plays the machines and their sensors:
  every simulated second it READS what the PLC decided (running, speed, fault, cobot speed setting),
  works out what the motors, bearings, caps and glue heater do as a result, and WRITES the sensor values
  into the PLC's input registers (%MW), the way an I/O module would.
The PLC program (OpenPLC, running our Structured Text) does everything else: its own TON timers trip the
filler on vibration and the capper on a jam, and its rules stop and slow the other machines.
Talking Machines reads the PLC with collector.py, read-only, exactly as before.

  python openplc_export.py --seed 7          once: make the program, upload it to OpenPLC, start the PLC
  python plant.py --port 502                 run the machines (OpenPLC's Modbus port is 502)
  python collector.py --port 502 --map data/openplc/map.json --rate 4

--speed: simulated seconds per real second. The PLC's timers run in real time (the jam timer is 2 s, the
vibration timer 10 s), so keep it low (default 5): at 5x a 15 s jam lasts 3 real seconds, long enough for
the 2 s jam timer to see it.
"""
import argparse
import json
import math
import os
import random
import socket
import struct
import time
from datetime import datetime, timedelta

import simulator as S

START = datetime(2026, 11, 27, 9, 0, 0)


class FieldIO:
    """The I/O side: reads PLC outputs and WRITES sensor values (Modbus 03 and 16). This is the plant's
    connection, not Talking Machines': the collector never writes."""

    def __init__(self, host, port, timeout=3.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.tid = 0

    def _ask(self, pdu):
        self.tid = (self.tid + 1) & 0xFFFF
        self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, 1) + pdu)
        head = self._recv(7)
        _, _, length, _ = struct.unpack(">HHHB", head)
        body = self._recv(length - 1)
        if body[0] & 0x80:
            raise IOError(f"PLC refused function {body[0] & 0x7F} (exception {body[1]})")
        return body

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("PLC closed the connection")
            buf += chunk
        return buf

    def read(self, addr, n):
        out = []
        while n > 0:
            k = min(n, 125)
            r = self._ask(struct.pack(">BHH", 3, addr, k))
            out += list(struct.unpack(f">{k}H", r[2:2 + 2 * k]))
            addr, n = addr + k, n - k
        return out

    def write(self, addr, values):
        while values:
            part, values = values[:120], values[120:]
            self._ask(struct.pack(">BHHB", 16, addr, len(part), 2 * len(part)) + struct.pack(f">{len(part)}H", *part))
            addr += len(part)

    def close(self):
        self.sock.close()


def to_reg(value, spec):
    if spec["type"] == "bool":
        return 1 if value else 0
    v = int(round(value * spec["scale"]))
    return max(-32768, min(32767, v)) & 0xFFFF


def from_reg(raw, spec):
    v = struct.unpack(">h", struct.pack(">H", raw & 0xFFFF))[0]
    return bool(v) if spec["type"] == "bool" else v / spec["scale"]


def run(host, port, speed, minutes, map_path, calm=False):
    with open(map_path, encoding="utf-8") as f:
        doc = json.load(f)
    seed, regmap = doc["seed"], doc["map"]
    tags = S.make_tags(random.Random(seed), seed)
    S.CALM = calm
    S.write_answers(tags, seed, START)                    # answer key + data/plc_programs for the translator
    tag = {k: v["tag"] for k, v in tags.items()}          # (machine, signal) -> tag name
    outs = {t: e for t, e in regmap.items() if e.get("dir") == "out"}
    ins = {t: e for t, e in regmap.items() if e.get("dir") == "in"}
    out_lo = min(e["addr"] for e in outs.values())
    out_n = max(e["addr"] for e in outs.values()) - out_lo + 1
    in_lo = regmap["__clock__"]["addr"]
    in_n = max(e["addr"] for e in ins.values()) - in_lo + 1

    rng = random.Random(seed + 99)
    JAM, JAMS, WEAR, FAIL, ZONES = S.timeline(seed)
    JAMS, ZONES = list(JAMS), list(ZONES)
    DIST = [] if calm else S.disturbances(seed)
    inject = S.Injector(START)
    glue_from = None
    st = {m: {"temp": 38.0, "count": 0.0} for m in S.MACHINES}
    cb = {"buffer": 0.0, "done": 0.0, "phase": 0.0}
    recorded = set()

    io = None
    while io is None:
        try:
            io = FieldIO(host, port)
        except OSError:
            print(f"waiting for the PLC at {host}:{port}… (is OpenPLC running and its Modbus server enabled?)",
                  flush=True)
            time.sleep(2)
    print(f"connected to the PLC at {host}:{port} · seed {seed} · {speed}x speed", flush=True)
    try:
        # warm-up: write sensible sensor values for 1 s (10 PLC scans) before the clock starts, so the PLC
        # doesn't act on empty inputs (a glue temperature of 0 would look like a dead heater)
        for i, t in enumerate([0] * 10 + list(range(1, minutes * 60))):
            warm = i < 10
            t0 = time.time()
            for what in inject(t):
                if what == "filler_wear" and FAIL > t + S.WEAR_FAST:
                    WEAR, FAIL = (t, t + S.WEAR_FAST) if t < WEAR else (WEAR, t + S.WEAR_FAST)
                elif what == "glue_heater" and glue_from is None:
                    glue_from = t
                elif what == "capper_jam":
                    JAMS.append((t, 30))
                elif what == "cobot_zone":
                    ZONES.append((t, 60))
            # 1. what the PLC decided
            raw = io.read(out_lo, out_n)
            plc = {t_: from_reg(raw[e["addr"] - out_lo], e) for t_, e in outs.items()}
            get = lambda m, s, d=0: plc.get(tag[(m, s)], d)
            for m, what in (("filler", "filler_fails"), ("labeller", "labeller_fails")):
                if get(m, "fault") and what not in recorded and t >= 30:   # the moment the PLC really tripped it
                    recorded.add(what)
                    _note(what, t)
            # 2. what the machines and sensors do
            field = {}
            wear = min(1.0, max(0.0, (t - WEAR) / (FAIL - WEAR)))
            jam_now = any(s0 <= t < s0 + d for s0, d in JAMS)
            glue = 165.0 if glue_from is None else max(120.0, 165.0 - (t - glue_from) * S.GLUE_DROP)
            case_rate = 0.0
            for m in S.MACHINES:
                s = st[m]
                running = bool(get(m, "running"))
                spd = float(get(m, "speed")) if running else 0.0
                load = spd / 300.0
                extra_i = (3.0 * wear if m == "filler" else 0.0) + 1.5 * S._dist(DIST, "heavy_batch", m, t)
                current = (2.0 + 9.0 * load + extra_i + rng.gauss(0, 0.15)) if running else rng.gauss(0.3, 0.05)
                target = 35 + 25 * load + (28 * wear if m == "filler" else 0) + 7 * S._dist(DIST, "warm", m, t)
                s["temp"] += (target - s["temp"]) / 240 + rng.gauss(0, 0.05)
                vib = (1.4 + 0.6 * load + (5.4 * wear ** 1.5 if m == "filler" else 0) + abs(rng.gauss(0, 0.2))
                       + 2.0 * S._dist(DIST, "bump", m, t)) if running else abs(rng.gauss(0.1, 0.05))
                s["count"] += 0 if warm else spd / 60
                field[tag[(m, "count")]] = int(s["count"])
                field[tag[(m, "motor_current")]] = max(current, 0)
                field[tag[(m, "motor_temp")]] = s["temp"]
                field[tag[(m, "vibration")]] = vib
                if m == "capper":       # a jam: the cap no longer grips, the torque drops; the PLC's timer sees it
                    field[tag[(m, "cap_torque")]] = (0.3 if jam_now else 1.2 + rng.gauss(0, 0.04)) if running else 0.0
                if m == "labeller":
                    field[tag[(m, "glue_temp")]] = glue + rng.gauss(0, 0.6)
                if m in ("depalletiser", "case_packer"):
                    field[tag[(m, "fault")]] = False                # their programs never raise a fault
                if m == "case_packer" and running:
                    case_rate = spd / 60 / 24
            field["JAM_CLEARED"] = bool(get("capper", "fault")) and not jam_now   # the operator clears the jam
            # the cobot palletises the cases, at the speed setting the PLC gives it
            zone = any(s0 <= t < s0 + d for s0, d in ZONES)
            override = float(get("cobot", "speed_override", 100.0)) or 100.0
            cb["buffer"] += case_rate
            capacity = (15 / 60) * override / 100
            working = cb["buffer"] > 0.01
            moved = min(cb["buffer"], capacity) if working else 0.0
            cb["buffer"] -= moved
            cb["done"] += moved
            cb["phase"] = (cb["phase"] + (moved / (15 / 60)) * 0.22) % 1.0 if working else cb["phase"]
            gripper = working and 0.2 <= cb["phase"] < 0.7
            motion = math.sin(2 * math.pi * cb["phase"])
            tcp = (1000 * override / 100 * (0.75 + 0.25 * abs(motion)) + rng.gauss(0, 12)) if working else 0.0
            field.update({tag[("cobot", "running")]: working, tag[("cobot", "fault")]: False,
                          tag[("cobot", "gripper_closed")]: gripper, tag[("cobot", "zone_occupied")]: zone,
                          tag[("cobot", "cycle_count")]: int(cb["done"]), tag[("cobot", "tcp_speed")]: max(tcp, 0)})
            for j in range(1, 7):
                dyn = (0.25 * S.GRAVITY[j] * motion * override / 100) if working else 0.0
                field[tag[("cobot", f"j{j}_torque")]] = (S.GRAVITY[j] + (S.PAYLOAD[j] if gripper else 0) + dyn
                                                        + rng.gauss(0, 0.03 * S.GRAVITY[j] + 0.1))
            # 3. write the sensor values into the PLC's input registers, with the clock
            regs = [0] * in_n
            regs[0], regs[1] = (t >> 16) & 0xFFFF, t & 0xFFFF
            for t_, e in ins.items():
                if t_ in field:
                    regs[e["addr"] - in_lo] = to_reg(field[t_], e)
            io.write(in_lo, regs)
            if i == 0:                                     # check the PLC really keeps what we write there
                back = io.read(in_lo, in_n)
                bad = [e["ident"] for t_, e in ins.items() if t_ in field and back[e["addr"] - in_lo] != regs[e["addr"] - in_lo]]
                if bad:
                    raise SystemExit(
                        f"The PLC did not keep the sensor values written at Modbus registers {in_lo}.. "
                        f"({len(bad)} of {len(ins)} differ, e.g. {', '.join(bad[:3])}). Usually this means the "
                        "uploaded program is not running (press Start PLC) or this OpenPLC maps %MW0 to another "
                        "address: re-export with  python openplc_export.py --seed N --mw-base ADDRESS  and upload again.")
                print(f"check OK: the PLC keeps the sensor values written at registers {in_lo}..{in_lo + in_n - 1}",
                      flush=True)
            if t % 60 == 0 and not (warm and i):
                print(f"sim minute {t // 60}", flush=True)
            time.sleep(0.1 if warm else max(0.0, 1 / speed - (time.time() - t0)))
        print("run finished", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        io.close()


def _note(what, t):
    try:
        with open("data/answers.json", encoding="utf-8") as f:
            ans = json.load(f)
        ans["events"][what] = str(START + timedelta(seconds=t))
        with open("data/answers.json", "w", encoding="utf-8") as f:
            json.dump(ans, f, indent=1)
    except (OSError, ValueError, KeyError):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--speed", type=float, default=5, help="simulated seconds per real second (keep it low)")
    ap.add_argument("--minutes", type=int, default=45)
    ap.add_argument("--map", default="data/openplc/map.json")
    ap.add_argument("--calm", action="store_true", help="no harmless disturbances")
    a = ap.parse_args()
    os.makedirs("data", exist_ok=True)
    run(a.host, a.port, a.speed, a.minutes, a.map, a.calm)
