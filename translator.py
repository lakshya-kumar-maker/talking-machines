"""Talking Machines — PLC program translator.

Reads a machine's PLC program (IEC 61131-3 Structured Text, full of cryptic tags) and explains every
rule in plain English, using the names the decoder worked out from the live data.

  python translator.py                      translate every program in data/plc_programs/
  python translator.py data/plc_programs/PLC2_OB1.st

No AI is needed: the code is parsed exactly. An AI summary is optional (see summarise()).
"""
import json
import os
import re
import sys

# ── 1. tokens ──────────────────────────────────────────────────────────────
TOKEN = re.compile(r"""
    (?P<ws>\s+) | (?P<comment>\(\*.*?\*\)) |
    (?P<time>T\#\d+(?:MS|S|M|H)) |
    (?P<num>\d+\.\d+|\d+) |
    (?P<op>:=|<=|>=|<>|[<>=+\-*/();,]) |
    (?P<id>[A-Za-z_][A-Za-z0-9_.]*(?::\d+(?:/\d+)?)?)
""", re.X | re.S | re.I)
KEYWORDS = {"IF", "THEN", "ELSIF", "ELSE", "END_IF", "AND", "OR", "NOT", "TRUE", "FALSE", "MOD"}


def tokenize(src):
    out, pos = [], 0
    while pos < len(src):
        m = TOKEN.match(src, pos)
        if not m:
            raise SyntaxError(f"can't read the code near: {src[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind in ("ws", "comment"):
            continue
        val = m.group(kind)
        if kind == "id" and val.upper() in KEYWORDS:
            kind, val = "kw", val.upper()
        out.append((kind, val))
    return out


# ── 2. parser: Structured Text -> a small tree ─────────────────────────────
class Parser:
    def __init__(self, tokens):
        self.t, self.i = tokens, 0

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else ("eof", "")

    def take(self, val=None):
        tok = self.peek()
        if val is not None and tok[1] != val:
            raise SyntaxError(f"expected {val!r} but found {tok[1]!r}")
        self.i += 1
        return tok

    def program(self):
        body = []
        while self.peek()[0] != "eof":
            body.append(self.statement())
        return body

    def block(self, *stops):
        body = []
        while self.peek()[1] not in stops:
            body.append(self.statement())
        return body

    def statement(self):
        kind, val = self.peek()
        if val == "IF":
            self.take("IF")
            branches = [(self.expr(), self.take("THEN") and self.block("ELSIF", "ELSE", "END_IF"))]
            other = None
            while self.peek()[1] == "ELSIF":
                self.take("ELSIF")
                branches.append((self.expr(), self.take("THEN") and self.block("ELSIF", "ELSE", "END_IF")))
            if self.peek()[1] == "ELSE":
                self.take("ELSE")
                other = self.block("END_IF")
            self.take("END_IF")
            self.take(";")
            return ("if", branches, other)
        if kind == "id" and self.peek(1)[1] == "(":            # a function block call, e.g. a TON timer
            name = self.take()[1]
            self.take("(")
            args = {}
            while self.peek()[1] != ")":
                key = self.take()[1]
                self.take(":=")
                args[key.upper()] = self.expr()
                if self.peek()[1] == ",":
                    self.take(",")
            self.take(")")
            self.take(";")
            return ("call", name, args)
        target = self.take()[1]
        self.take(":=")
        value = self.expr()
        self.take(";")
        return ("set", target, value)

    def expr(self):
        node = self.and_()
        while self.peek()[1] == "OR":
            self.take()
            node = ("or", node, self.and_())
        return node

    def and_(self):
        node = self.not_()
        while self.peek()[1] == "AND":
            self.take()
            node = ("and", node, self.not_())
        return node

    def not_(self):
        if self.peek()[1] == "NOT":
            self.take()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        node = self.sum()
        if self.peek()[1] in ("<", ">", "<=", ">=", "=", "<>"):
            op = self.take()[1]
            node = ("cmp", op, node, self.sum())
        return node

    def sum(self):
        node = self.term()
        while self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            node = ("math", op, node, self.term())
        return node

    def term(self):
        node = self.atom()
        while self.peek()[1] in ("*", "/", "MOD"):
            op = self.take()[1]
            node = ("math", op, node, self.atom())
        return node

    def atom(self):
        kind, val = self.take()
        if val == "(":
            node = self.expr()
            self.take(")")
            return node
        if kind == "num":
            return ("num", float(val))
        if kind == "time":
            return ("time", val)
        if val in ("TRUE", "FALSE"):
            return ("bool", val == "TRUE")
        if kind == "id":
            return ("tag", val)
        raise SyntaxError(f"unexpected {val!r}")


# ── 3. English ─────────────────────────────────────────────────────────────
STATE = {  # how to say "this on/off signal is on" / "… is off"
    "running": ("{m} is running", "{m} is not running"),
    "fault": ("{m} has a fault", "{m} has no fault"),
    "gripper_closed": ("{m} gripper is closed", "{m} gripper is open"),
    "zone_occupied": ("someone is in the {m}'s safety zone", "the {m}'s safety zone is clear"),
}
ACTION = {  # what writing TRUE / FALSE to it means
    "running": ("start the {m}", "stop the {m}"),
    "fault": ("raise the {m} fault", "clear the {m} fault"),
    "gripper_closed": ("close the {m} gripper", "open the {m} gripper"),
}


def duration(t):
    m = re.match(r"T#(\d+)(MS|S|M|H)", t, re.I)
    n, unit = int(m.group(1)), m.group(2).upper()
    word = {"MS": "millisecond", "S": "second", "M": "minute", "H": "hour"}[unit]
    return f"{n} {word}{'s' if n != 1 else ''}"


def fmt(x):
    return f"{x:g}"


class English:
    def __init__(self, names):
        self.names = names            # {tag: {"machine", "signal", "name", "unit"}} from the decoder
        self.timers = {}              # timer name -> (condition text, duration text)
        self.unknown = set()

    def who(self, tag):
        r = self.names.get(tag)
        if not r:
            return None
        return "the " + {"case_packer": "case packer"}.get(r["machine"], r["machine"])

    def thing(self, tag):
        r = self.names.get(tag)
        if not r:
            self.unknown.add(tag)
            return f"{tag} (an internal flag with no live data)"
        return "the " + r["name"][0].lower() + r["name"][1:]

    def unit(self, node):
        if node[0] == "tag" and node[1] in self.names and self.names[node[1]].get("unit") not in ("", None):
            return " " + self.names[node[1]]["unit"]
        return ""

    def value(self, node, unit=""):
        k = node[0]
        if k == "num":
            return fmt(node[1]) + unit
        if k == "bool":
            return "on" if node[1] else "off"
        if k == "time":
            return duration(node[1])
        if k == "tag":
            if node[1].endswith(".Q") and node[1][:-2] in self.timers:
                cond, dur = self.timers[node[1][:-2]]
                return f"{cond} for {dur}"
            return self.thing(node[1])
        if k == "math":
            words = {"+": "plus", "-": "minus", "*": "times", "/": "divided by", "MOD": "modulo"}[node[1]]
            return f"{self.value(node[2])} {words} {self.value(node[3])}"
        return self.cond(node)

    def cond(self, node, negate=False):
        k = node[0]
        if k == "not":
            return self.cond(node[1], not negate)
        if k in ("and", "or"):
            if negate:     # not (a and b) = not a or not b
                k = "or" if k == "and" else "and"
            return f"{self.cond(node[1], negate)} {k} {self.cond(node[2], negate)}"
        if k == "tag":
            tag = node[1]
            if tag.endswith(".Q") and tag[:-2] in self.timers:
                cond, dur = self.timers[tag[:-2]]
                return (f"{cond} for {dur}" if not negate else f"it is not true that {cond} for {dur}")
            r = self.names.get(tag)
            if r and r["signal"] in STATE:
                on, off = STATE[r["signal"]]
                m = {"case_packer": "case packer"}.get(r["machine"], r["machine"])
                text = (off if negate else on).format(m=m)
                return text if text.startswith("someone") or text.startswith("the") else "the " + text
            return f"{self.thing(tag)} is {'off' if negate else 'on'}"
        if k == "cmp":
            op, left, right = node[1], node[2], node[3]
            if right[0] == "bool":                       # X = FALSE  ->  "X is off"
                want = right[1] if op == "=" else not right[1]
                return self.cond(left, negate=(not want) ^ negate)
            u = self.unit(left)
            words = {">": "is above", "<": "is below", ">=": "is at least", "<=": "is at most",
                     "=": "equals", "<>": "is not"}
            if negate:
                op = {">": "<=", "<": ">=", ">=": "<", "<=": ">", "=": "<>", "<>": "="}[op]
            return f"{self.value(left)} {words[op]} {self.value(right, u)}"
        return self.value(node)

    def statement(self, s, depth=0):
        k = s[0]
        if k == "call":
            name, args = s[1], s[2]
            if "IN" in args and "PT" in args:            # an on-delay timer
                cond, dur = self.cond(args["IN"]), duration(args["PT"][1])
                if " and " in cond or " or " in cond:             # make clear the timer covers the whole condition
                    cond = f"({cond}) holds"
                self.timers[name] = (cond, dur)
                return [f"Start a timer: it trips once {cond} for {dur}."]
            return [f"Run {name}."]
        if k == "set":
            return [self.action(s[1], s[2]) + "."]
        if k == "if":
            lines = []
            for i, (c, body) in enumerate(s[1]):
                head = "If" if i == 0 else "Otherwise, if"
                lines.append(f"{head} {self.cond(c)}: " + "; ".join(self.flat(body)) + ".")
            if s[2]:
                lines.append("Otherwise: " + "; ".join(self.flat(s[2])) + ".")
            return lines
        return []

    def flat(self, body):
        parts = []
        for s in body:
            parts += [p.rstrip(".") for p in self.statement(s)]
        return [p[0].lower() + p[1:] if p else p for p in parts]

    def action(self, target, value):
        r = self.names.get(target)
        if r and r["signal"] in ACTION and value[0] == "bool":
            on, off = ACTION[r["signal"]]
            m = {"case_packer": "case packer"}.get(r["machine"], r["machine"])
            return (on if value[1] else off).format(m=m).capitalize()
        u = " " + r["unit"] if r and r.get("unit") else ""
        if value[0] == "num":
            return f"Set {self.thing(target)} to {fmt(value[1])}{u}"
        return f"Set {self.thing(target)} to {self.value(value)}"


# ── 4. public API ──────────────────────────────────────────────────────────
def load_names(path="data/decoded.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


def split_rules(src):
    """Top-level statements with their original code, so code and English can be shown side by side."""
    tokens = tokenize(src)
    p = Parser(tokens)
    rules = []
    lines = src.splitlines()
    starts = [i for i, l in enumerate(lines) if l.strip() and not l.startswith((" ", "\t"))
              and not l.strip().upper().startswith(("ELSIF", "ELSE", "END_IF"))]
    while p.peek()[0] != "eof":
        rules.append(p.statement())
    blocks = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        blocks.append("\n".join(lines[i:end]).rstrip())
    if len(blocks) != len(rules):                       # unusual layout: fall back to one block
        blocks = [src.strip()] + [""] * (len(rules) - 1)
    return list(zip(blocks, rules))


def which_machine(rules, names):
    """The machine whose signals this program writes to most."""
    count = {}
    def walk(s):
        if s[0] == "set" and s[1] in names:
            count[names[s[1]]["machine"]] = count.get(names[s[1]]["machine"], 0) + 1
        if s[0] == "if":
            for _, body in s[1]:
                for x in body:
                    walk(x)
            for x in s[2] or []:
                walk(x)
    for _, r in rules:
        walk(r)
    return max(count, key=count.get) if count else None


def translate(src, names):
    rules = split_rules(src)
    en = English(names)
    out = [{"code": code, "english": " ".join(en.statement(rule))} for code, rule in rules]
    return {"machine": which_machine(rules, names), "rules": out, "unknown": sorted(en.unknown)}


def translate_all(folder="data/plc_programs", names=None):
    names = names if names is not None else load_names()
    result = {}
    for f in sorted(os.listdir(folder)):
        if f.endswith(".st"):
            with open(os.path.join(folder, f), encoding="utf-8") as fh:
                result[f] = translate(fh.read(), names)
    return result


def fault_rules(names, folder="data/plc_programs"):
    """{machine: (file, "the condition that makes its own program raise its fault")} — used by Ask the Line."""
    out = {}
    if not os.path.isdir(folder):
        return out
    for f, t in translate_all(folder, names).items():
        m = t["machine"]
        label = "the " + {"case_packer": "case packer"}.get(m, m or "") + " fault"
        for r in t["rules"]:
            hit = re.match(r"If (.+?): raise " + re.escape(label), r["english"])
            if hit and m:
                out.setdefault(m, (f, hit.group(1)))
    return out


def summarise(translation):
    """Optional: a short AI summary of a whole program, built only from the exact translation above."""
    text = "\n".join(r["english"] for r in translation["rules"])
    try:
        from dotenv import load_dotenv
        from groq import Groq
        load_dotenv()
        r = Groq(api_key=os.environ["GROQ_API_KEY"]).chat.completions.create(
            model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"), timeout=20,
            messages=[{"role": "user", "content": "Summarise what this machine's control program does for a "
                       "maintenance engineer in 2 short sentences. Use only these rules, add nothing:\n" + text}])
        return r.choices[0].message.content.strip()
    except Exception:
        return None


if __name__ == "__main__":
    names = load_names()
    files = sys.argv[1:] or [os.path.join("data/plc_programs", f) for f in sorted(os.listdir("data/plc_programs"))]
    for path in files:
        with open(path, encoding="utf-8") as fh:
            t = translate(fh.read(), names)
        machine = (t["machine"] or "unknown").replace("_", " ").title()
        print(f"\n═══ {os.path.basename(path)}  →  {machine}")
        for r in t["rules"]:
            print("\n" + "\n".join("   │ " + l for l in r["code"].splitlines()))
            print("   → " + r["english"])
        if t["unknown"]:
            print(f"\n   not decoded (no live data): {', '.join(t['unknown'])}")