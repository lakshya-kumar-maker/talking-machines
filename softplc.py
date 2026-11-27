"""Talking Machines — a small stand-in for OpenPLC, for testing without installing it.

It does what OpenPLC does with our program: every 100 ms it reads the field inputs (%MW = holding registers
1024..), runs the machines' Structured Text (the same code as data/plc_programs), with real TON timers, and
writes the outputs (%QW = holding registers 0..). Modbus TCP: reads (03, 04) and writes (06, 16) like OpenPLC.

  python softplc.py --port 5502            then: python plant.py --port 5502

The real thing is OpenPLC Runtime (see the setup guide); this is only for testing and as a fallback.
--time-scale makes the timers run faster (for fast tests together with plant.py --speed).
"""

import argparse
import json
import re
import socketserver
import struct
import threading
import time

import plc_programs
import translator

MW = 1024


class Program:
    """Runs parsed Structured Text (the subset our PLC programs use: IF/ELSIF/ELSE, :=, maths, TON)."""

    def __init__(self, code_by_machine, tag_of, time_scale=1.0):
        self.rules = []
        for machine, code in code_by_machine.items():
            text = re.sub(r"\{(\w+)\.(\w+)\}", lambda m: tag_of[(m.group(1), m.group(2))], code)
            text = re.sub(r"[ \t]*\(\*.*?\*\)", "", text)
            p = translator.Parser(translator.tokenize(text))
            self.rules += p.program()
        self.vars, self.timers, self.scale = {}, {}, time_scale

    def scan(self, dt):
        for r in self.rules:
            self.run(r, dt)

    def run(self, s, dt):
        k = s[0]
        if k == "set":
            self.vars[s[1]] = self.ev(s[2])
        elif k == "if":
            for cond, body in s[1]:
                if self.ev(cond):
                    for x in body:
                        self.run(x, dt)
                    return
            for x in s[2] or []:
                self.run(x, dt)
        elif k == "call":  # TON: on-delay timer
            tm = self.timers.setdefault(s[1], {"et": 0.0, "q": False})
            pt = self._seconds(s[2]["PT"][1])
            if self.ev(s[2]["IN"]):
                tm["et"] = min(pt, tm["et"] + dt * self.scale)
                tm["q"] = tm["et"] >= pt
            else:
                tm["et"], tm["q"] = 0.0, False

    @staticmethod
    def _seconds(t):
        m = re.match(r"T#(\d+)(MS|S|M|H)", t, re.I)
        return int(m.group(1)) * {"MS": 0.001, "S": 1, "M": 60, "H": 3600}[m.group(2).upper()]

    def ev(self, n):
        k = n[0]
        if k == "num":
            return n[1]
        if k == "bool":
            return n[1]
        if k == "tag":
            name = n[1]
            if name.endswith(".Q") and name[:-2] in self.timers:
                return self.timers[name[:-2]]["q"]
            return self.vars.get(name, 0)
        if k == "not":
            return not self.ev(n[1])
        if k == "and":
            return bool(self.ev(n[1])) and bool(self.ev(n[2]))
        if k == "or":
            return bool(self.ev(n[1])) or bool(self.ev(n[2]))
        if k == "cmp":
            a, b = self.ev(n[2]), self.ev(n[3])
            return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b, "=": a == b, "<>": a != b}[n[1]]
        if k == "math":
            a, b = self.ev(n[2]), self.ev(n[3])
            if n[1] == "/":
                return a // b if isinstance(a, int) and isinstance(b, int) else a / b
            return {"+": a + b, "-": a - b, "*": a * b, "MOD": a % b}[n[1]]
        raise ValueError(f"can't evaluate {n}")


class SoftPLC:
    def __init__(self, map_path, host, port, time_scale):
        with open(map_path, encoding="utf-8") as f:
            doc = json.load(f)
        self.map = {t: e for t, e in doc["map"].items() if not t.startswith("__")}
        import random

        import simulator

        tags = simulator.make_tags(random.Random(doc["seed"]), doc["seed"])
        self.prog = Program(plc_programs.PROGRAMS, {k: v["tag"] for k, v in tags.items()}, time_scale)
        self.regs = [0] * 2048
        self.lock = threading.Lock()
        plc = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock = self.request
                while True:
                    head = _recv(sock, 7)
                    if not head:
                        return
                    tid, _, length, uid = struct.unpack(">HHHB", head)
                    body = _recv(sock, length - 1)
                    if not body:
                        return
                    fc = body[0]
                    with plc.lock:
                        if fc in (3, 4):
                            a, n = struct.unpack(">HH", body[1:5])
                            pdu = struct.pack(">BB", fc, 2 * n) + struct.pack(f">{n}H", *plc.regs[a : a + n])
                        elif fc == 6:
                            a, v = struct.unpack(">HH", body[1:5])
                            plc.regs[a] = v
                            pdu = body[:5]
                        elif fc == 16:
                            a, n = struct.unpack(">HH", body[1:5])
                            plc.regs[a : a + n] = list(struct.unpack(f">{n}H", body[6 : 6 + 2 * n]))
                            pdu = body[:5]
                        else:
                            pdu = struct.pack(">BB", fc | 0x80, 1)
                    sock.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer((host, port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def loop(self, cycle=0.1):
        last = time.time()
        while True:
            now = time.time()
            dt, last = now - last, now
            with self.lock:
                for t, e in self.map.items():  # inputs: registers -> values
                    if e["dir"] == "in":
                        raw = struct.unpack(">h", struct.pack(">H", self.regs[e["addr"]]))[0]
                        self.prog.vars[t] = (
                            (raw != 0) if e["type"] == "bool" else (raw if e["kind"] == "int" else raw / e["scale"])
                        )
                self.prog.scan(dt)
                for t, e in self.map.items():  # outputs: values -> registers
                    if e["dir"] == "out":
                        v = self.prog.vars.get(t, 0)
                        r = (
                            (1 if v else 0)
                            if e["type"] == "bool"
                            else int(round(v * (1 if e["kind"] == "int" else e["scale"])))
                        )
                        self.regs[e["addr"]] = max(-32768, min(32767, r)) & 0xFFFF
            time.sleep(max(0.0, cycle - (time.time() - now)))


def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return b""
        if not chunk:
            return b""
        buf += chunk
    return buf


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5502)
    ap.add_argument("--map", default="data/openplc/map.json")
    ap.add_argument("--time-scale", type=float, default=1.0, help="timers run this many times faster (tests)")
    a = ap.parse_args()
    p = SoftPLC(a.map, a.host, a.port, a.time_scale)
    print(f"stand-in PLC on {a.host}:{a.port} (OpenPLC layout: %QW = 0.., %MW = 1024..)", flush=True)
    try:
        p.loop()
    except KeyboardInterrupt:
        pass
