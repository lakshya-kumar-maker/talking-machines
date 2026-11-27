"""Talking Machines — a tiny Modbus TCP link, standard library only.

The simulator uses PLCServer to act like a real PLC; the collector uses read_registers to read it.
Only READ functions are answered (03 read holding registers, 04 read input registers).
Every WRITE request (05, 06, 15, 16, …) is refused with Modbus exception 01 "illegal function":
Talking Machines can never change anything on a machine.
"""

import json
import os
import socket
import socketserver
import struct
import threading

READ_FUNCTIONS = (3, 4)
MAX_REGS = 125  # Modbus limit per read request


# ── register map: where each tag lives in the PLC ─────────────────────────
def build_map(tags, kinds, start=10):
    """tags: list of tag names; kinds: {tag: 'bool'|'int'|'real'}. Register 0-1 hold the PLC clock."""
    m, addr = {}, start
    for t in tags:
        typ = {"bool": "bool", "int": "int32", "real": "float32"}[kinds[t]]
        m[t] = {"addr": addr, "type": typ}
        addr += 1 if typ == "bool" else 2
    return m


def encode(values, regmap, clock, size):
    regs = [0] * size
    regs[0], regs[1] = (clock >> 16) & 0xFFFF, clock & 0xFFFF
    for t, spec in regmap.items():
        v, a = values[t], spec["addr"]
        if spec["type"] == "bool":
            regs[a] = 1 if v else 0
        elif spec["type"] == "int32":
            hi, lo = struct.unpack(">HH", struct.pack(">i", int(v)))
            regs[a], regs[a + 1] = hi, lo
        else:
            hi, lo = struct.unpack(">HH", struct.pack(">f", float(v)))
            regs[a], regs[a + 1] = hi, lo
    return regs


def decode(regs, regmap):
    """regs: register values by address (a list, or a dict address -> value).
    Map types: bool, int32, float32 (two registers), int16 with a scale (value = register / scale; the usual
    way PLCs send real numbers in one register), uint32 (two registers). The clock is in registers 0-1, or at
    the address of the "__clock__" entry."""
    out = {}
    for t, spec in regmap.items():
        if t.startswith("__") or spec.get("internal"):
            continue
        a = spec["addr"]
        if spec["type"] == "bool":
            out[t] = int(regs[a] != 0)
        elif spec["type"] == "int32":
            out[t] = struct.unpack(">i", struct.pack(">HH", regs[a], regs[a + 1]))[0]
        elif spec["type"] == "int16":
            raw = struct.unpack(">h", struct.pack(">H", regs[a] & 0xFFFF))[0]
            sc = spec.get("scale", 1)
            out[t] = raw if sc == 1 else round(raw / sc, 4)
        else:
            out[t] = round(struct.unpack(">f", struct.pack(">HH", regs[a], regs[a + 1]))[0], 3)
    c = regmap.get("__clock__", {"addr": 0})["addr"]
    clock = (regs[c] << 16) | regs[c + 1]
    return clock, out


def blocks(regmap, gap=8):
    """The register ranges to read: used addresses grouped into blocks (OpenPLC keeps outputs at 0.. and
    memory at 1024.., so reading everything in between would be wasteful)."""
    used = set()
    for t, spec in regmap.items():
        n = 2 if spec["type"] in ("int32", "float32", "uint32") else 1
        used.update(range(spec["addr"], spec["addr"] + n))
    if "__clock__" not in regmap:
        used.update((0, 1))
    out, start, prev = [], None, None
    for a in sorted(used):
        if start is None:
            start = prev = a
        elif a - prev > gap:
            out.append((start, prev - start + 1))
            start = a
        prev = a
    if start is not None:
        out.append((start, prev - start + 1))
    return out


def read_map(client, regmap):
    """Read every register the map uses (only read requests). -> {address: value}"""
    regs = {}
    for start, n in blocks(regmap):
        for i, v in enumerate(client.read_registers(start, n)):
            regs[start + i] = v
    return regs


def map_size(regmap):
    return max(s["addr"] + (1 if s["type"] in ("bool", "int16") else 2) for s in regmap.values())


# ── server: the simulated PLC ──────────────────────────────────────────────
class PLCServer:
    def __init__(self, size, host="127.0.0.1", port=5020):
        self.regs = [0] * size
        self.lock = threading.Lock()
        self.refused_writes = 0
        plc = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock = self.request
                while True:
                    head = _recv(sock, 7)
                    if not head:
                        return
                    tid, pid, length, uid = struct.unpack(">HHHB", head)
                    body = _recv(sock, length - 1)
                    if not body:
                        return
                    fc = body[0]
                    if fc in READ_FUNCTIONS and len(body) >= 5:
                        addr, qty = struct.unpack(">HH", body[1:5])
                        with plc.lock:
                            ok = 1 <= qty <= MAX_REGS and addr + qty <= len(plc.regs)
                            data = plc.regs[addr : addr + qty] if ok else []
                        if ok:
                            pdu = struct.pack(">BB", fc, 2 * qty) + struct.pack(f">{qty}H", *data)
                        else:
                            pdu = struct.pack(">BB", fc | 0x80, 2)  # 02 illegal data address
                    else:
                        plc.refused_writes += 1
                        pdu = struct.pack(">BB", fc | 0x80, 1)  # 01 illegal function: read-only
                    sock.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu)

        self.server = _Server((host, port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def update(self, regs):
        with self.lock:
            self.regs[: len(regs)] = regs

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second PLC bind a port that is still in use, so an old simulator left
    # running would silently keep answering. Only reuse the address where it means "after TIME_WAIT" (Linux/macOS).
    allow_reuse_address = os.name != "nt"


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


# ── client: how Talking Machines reads the PLC ─────────────────────────────
class PLCClient:
    def __init__(self, host="127.0.0.1", port=5020, timeout=2.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.tid = 0

    def _ask(self, pdu):
        self.tid = (self.tid + 1) & 0xFFFF
        self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, 1) + pdu)
        head = _recv(self.sock, 7)
        if not head:
            raise ConnectionError("PLC closed the connection")
        _, _, length, _ = struct.unpack(">HHHB", head)
        body = _recv(self.sock, length - 1)
        if not body:
            raise ConnectionError("PLC closed the connection mid-reply")
        return body

    def read_registers(self, addr, qty):
        out = []
        while qty > 0:
            n = min(qty, MAX_REGS)
            resp = self._ask(struct.pack(">BHH", 3, addr, n))
            if resp[0] & 0x80:
                raise IOError(f"PLC refused the read (exception {resp[1]})")
            out += list(struct.unpack(f">{n}H", resp[2 : 2 + 2 * n]))
            addr, qty = addr + n, qty - n
        return out

    def try_write(self, addr, value):
        """Only used to prove the PLC link is read-only. Returns the PLC's answer."""
        resp = self._ask(struct.pack(">BHH", 6, addr, value))
        return "refused (read-only)" if resp[0] & 0x80 else "accepted"

    def close(self):
        self.sock.close()


def save_map(regmap, path="data/plc_map.json"):
    """The tag list a plant would export from its PLC: names and addresses, no meanings."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(regmap, f, indent=1)
    os.replace(tmp, path)  # atomic: the collector never sees a half-written map


def load_map(path="data/plc_map.json"):
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    return m.get("map", m)  # data/openplc/map.json keeps the map under "map"
