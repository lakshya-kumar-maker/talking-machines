"""Talking Machines — collector: reads the PLC over Modbus TCP (read-only) and keeps a history file.

  python collector.py                 read 127.0.0.1:5020 and write data/stream.csv
  python collector.py --test-write    prove the link is read-only: try a write and show it is refused

This is the same job a plant's data collector ("historian") does. It only ever sends READ requests.
"""

import argparse
import csv
import os
import time
from datetime import datetime, timedelta

import plc

START = datetime(2026, 11, 27, 9, 0, 0)


def connect(host, port):
    while True:
        try:
            return plc.PLCClient(host, port)
        except OSError:
            print("waiting for the PLC…")
            time.sleep(1)


def _tags(regmap):
    return [t for t, s in regmap.items() if not t.startswith("__") and not s.get("internal")]


def run(host, port, rate, out="data/stream.csv", map_path="data/plc_map.json"):
    while not os.path.exists(map_path):
        time.sleep(0.5)
    regmap = plc.load_map(map_path)
    tags = _tags(regmap)
    client = connect(host, port)
    print(f"connected to PLC {host}:{port} · reading {len(tags)} tags, read-only")
    f = writer = None
    last_clock, last_row = None, None
    try:
        while True:
            try:
                regs = plc.read_map(client, regmap)
            except (OSError, ConnectionError, IOError):
                client.close()
                client = connect(host, port)
                regmap = plc.load_map(map_path)  # a restarted PLC may have a new tag list
                tags = _tags(regmap)
                continue
            clock, values = plc.decode(regs, regmap)
            if last_clock is not None and clock < last_clock:
                last_clock = None
            if last_clock is None:
                if f:
                    f.close()
                f = open(out, "w", newline="")
                writer = csv.writer(f)
                writer.writerow(["timestamp"] + tags)
                last_clock, last_row = clock - 1, None
            if clock > last_clock:
                row = [values[t] for t in tags]
                for missing in range(last_clock + 1, clock):  # fill any second we were too slow to see
                    writer.writerow([str(START + timedelta(seconds=missing))] + (last_row or row))
                writer.writerow([str(START + timedelta(seconds=clock))] + row)
                f.flush()
                last_clock, last_row = clock, row
            time.sleep(1 / rate)
    except KeyboardInterrupt:
        pass
    finally:
        if f:
            f.close()
        client.close()


def test_write(host, port, map_path="data/plc_map.json"):
    regmap = plc.load_map(map_path)
    tag, spec = next((t, s) for t, s in regmap.items() if not t.startswith("__"))
    c = connect(host, port)
    print(f"trying to WRITE 1 to {tag} (register {spec['addr']})… the PLC says: {c.try_write(spec['addr'], 1)}")
    c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--rate", type=float, default=100, help="reads per second")
    ap.add_argument("--test-write", action="store_true")
    ap.add_argument("--map", default="data/plc_map.json", help="register map (OpenPLC: data/openplc/map.json)")
    a = ap.parse_args()
    if a.test_write:
        test_write(a.host, a.port, a.map)
    else:
        run(a.host, a.port, a.rate, map_path=a.map)
