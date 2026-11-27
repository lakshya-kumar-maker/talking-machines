"""Talking Machines — maintenance recommendations: what to inspect, what to bring, and what to do first.

1. PATTERNS: each alert's evidence (which signals moved and in what order, the PLC rule, a fault) is matched
   to a failure pattern with an inspection checklist, likely parts, tools, and two repair times:
   planned (fix it before it breaks) and unplanned (it breaks first; usually longer: more damage, no parts
   ready, the line has to restart from cold).
2. Priority = confidence × money at risk ÷ time left. Money at risk = the extra line-down minutes if it
   breaks instead of being fixed in a planned stop × what one line-minute costs.
3. Work orders and a shift report as HTML files (open in a browser, print or "save as PDF").

Repair times and parts are typical figures for a small bottling line; a plant replaces them with its own.
"""

import html
from datetime import timedelta

LOTO = "Lock out and tag out the machine before opening guards or touching moving parts."

PATTERNS = {
    "bearing": {
        "name": "Bearing wear / misalignment",
        "checklist": [
            "Listen at the motor and gearbox bearings (screwdriver or stethoscope): grinding or rumble?",
            "Measure the bearing housing temperature (IR thermometer): more than 15 °C above the other end?",
            "Check shaft and coupling alignment; look for fretting or a worn coupling insert",
            "Check grease: discoloured, dry or over-filled? Re-grease to the plan's amount only",
            "Feel for play in the shaft; replace the bearing if there is any",
        ],
        "parts": ["Drive-end motor bearing", "Coupling insert", "Bearing grease"],
        "tools": ["IR thermometer", "Vibration pen", "Bearing puller", "Alignment tool", "Torque wrench"],
        "planned": 45,
        "unplanned": 120,
    },
    "loose": {
        "name": "Looseness or imbalance",
        "checklist": [
            "Check mounting bolts, guards and the motor base for loose fasteners",
            "Look for a damaged or dirty fan, pulley or star wheel (imbalance)",
            "Check for something touching or rubbing a moving part",
        ],
        "parts": ["Fasteners", "Anti-vibration mounts"],
        "tools": ["Spanner set", "Torque wrench", "Vibration pen"],
        "planned": 20,
        "unplanned": 60,
    },
    "overload": {
        "name": "Overload or extra friction",
        "checklist": [
            "Check the product: a heavier or thicker batch than usual?",
            "Check chain and belt tension; look for a partial jam or build-up",
            "Check lubrication of guides, chains and gearbox oil level",
            "Compare motor current with the nameplate",
        ],
        "parts": ["Chain/belt (if worn)", "Lubricant"],
        "tools": ["Clamp meter", "Tension gauge", "Grease gun"],
        "planned": 25,
        "unplanned": 60,
    },
    "cooling": {
        "name": "Motor cooling problem",
        "checklist": [
            "Check the motor fan and fan cover for damage or blockage",
            "Clean dust off the motor fins and the cabinet air filters",
            "If several motors are hot at once: check hall ventilation and ambient temperature first",
        ],
        "parts": ["Cabinet filter mats", "Motor fan (if broken)"],
        "tools": ["IR thermometer", "Compressed air / brush"],
        "planned": 15,
        "unplanned": 45,
    },
    "friction": {
        "name": "More current at the same speed",
        "checklist": [
            "Look for a partial jam or product build-up",
            "Check lubrication and for a dragging brake or bearing",
            "Check whether a heavier product batch started at the same time (then it may be harmless)",
        ],
        "parts": ["Lubricant"],
        "tools": ["Clamp meter", "Grease gun"],
        "planned": 15,
        "unplanned": 45,
    },
    "glue_heater": {
        "name": "Glue heater failing",
        "checklist": [
            "Check the glue temperature controller: set point, alarms, output on?",
            "Measure the heater element resistance (open circuit = broken element)",
            "Check the heater fuse and contactor",
            "Check the thermocouple/PT100 and its connector (a bad reading looks like a cold heater)",
            "Check the glue level in the tank",
        ],
        "parts": ["Heater cartridge", "Heater fuse", "Thermocouple / PT100"],
        "tools": ["Multimeter", "IR thermometer", "Heat-resistant gloves"],
        "planned": 30,
        "unplanned": 50,
    },
    "cap_jam": {
        "name": "Cap jam",
        "checklist": [
            "Clear the jammed caps from the chute and capping head",
            "Check the cap feed chute and sorter for deformed caps",
            "Check the capping head clutch / torque setting",
            "Run a few bottles slowly and watch the cap pick-up",
        ],
        "parts": ["Spare caps (check batch)", "Capping chuck insert"],
        "tools": ["Hand tools", "Torque tester"],
        "planned": 10,
        "unplanned": 15,
    },
    "fault": {
        "name": "Machine fault",
        "checklist": [
            "Read the fault message on the machine's panel (HMI)",
            "Check the last signals before the stop on the Live line tab",
            "Follow the machine manual for that fault code",
        ],
        "parts": [],
        "tools": ["Machine manual"],
        "planned": 20,
        "unplanned": 40,
    },
}


def pattern_of(card):
    """Which failure pattern this alert's evidence matches."""
    keys = [c["key"] for c in card.get("changed", [])]
    why = (card.get("forest") or {}).get("why", "")
    if "glue_temp" in keys or "glue" in why:
        return "glue_heater"
    if card["machine"] == "Capper" and card["status"] == "FAULT" and not keys:
        return "cap_jam"
    if keys[:1] == ["vibration"] and len(keys) > 1:
        return "bearing"
    if "vibration" in keys or "vibration" in why:
        return "bearing" if card.get("forecast") else "loose"
    if "motor_temp" in keys and "motor_current" in keys:
        return "overload"
    if "motor_temp" in keys:
        return "cooling"
    if "motor_current" in keys or "current" in why:
        return "friction"
    return "fault"


def plan(cards, line_cost_per_min, now):
    """Work orders, most urgent first.
    cards: line.alert_cards(); line_cost_per_min: ₹ one minute of the whole line standing still."""
    orders = []
    for c in cards:
        key = pattern_of(c)
        p = PATTERNS[key]
        conf = (c.get("confidence_pct") or 50) / 100
        fault = c["status"] == "FAULT"
        fc = c.get("forecast")
        left = 0.0 if fault else (fc["minutes"] if fc else None)
        extra = p["unplanned"] - p["planned"]  # minutes saved by fixing it before it breaks
        if fault:
            at_risk = p["unplanned"] * line_cost_per_min  # the line is already down: every minute counts
            when, slot = "Now", "Now: the line is down until this is fixed."
        elif left is not None:
            at_risk = conf * extra * line_cost_per_min
            deadline = now + timedelta(minutes=left)
            if left <= p["planned"] + 5:
                when, slot = (
                    f"Now (trips ~{deadline:%H:%M})",
                    (f"Now: it is predicted to trip at about {deadline:%H:%M}, too soon to wait for a better moment."),
                )
            else:
                latest = deadline - timedelta(minutes=p["planned"] + 5)
                when, slot = (
                    f"Before {latest:%H:%M}",
                    (
                        f"Plan a {p['planned']}-minute stop before {latest:%H:%M} (it is predicted to trip at about "
                        f"{deadline:%H:%M}); a changeover or break before then is ideal."
                    ),
                )
        elif conf >= 0.5:
            at_risk = conf * extra * line_cost_per_min
            when, slot = "Next planned stop", "At the next planned stop or changeover; no trip is predicted yet."
        else:
            at_risk = conf * extra * line_cost_per_min
            when, slot = (
                "Walk-round check",
                ("Low confidence it is real: look at it on the next walk-round, no stop needed."),
            )
        urgency = 1 / max(left, 1) if left is not None else 1 / 60
        orders.append(
            {
                "machine": c["machine"],
                "pattern": key,
                "problem": p["name"],
                "title": c["title"],
                "confidence": c.get("confidence_pct"),
                "fault": fault,
                "when": when,
                "slot": slot,
                "planned_min": p["planned"],
                "unplanned_min": p["unplanned"],
                "at_risk": at_risk,
                "save_if_planned": extra * line_cost_per_min,
                "priority": at_risk * urgency * (10 if fault else 1),
                "checklist": [LOTO] + p["checklist"],
                "parts": p["parts"],
                "tools": p["tools"],
                "evidence": [
                    f"{x['signal']}: normal {x['normal']} → {x['now']} {x['unit']} ({x['change_pct']:+}%) "
                    f"since {x['since']:%H:%M}"
                    for x in c.get("changed", [])
                ]
                + (
                    [f"Isolation Forest: unusual since {c['forest']['since']:%H:%M}, mainly {c['forest']['why']}"]
                    if c.get("forest")
                    else []
                )
                + (
                    [
                        f"Forecast: trips at about {fc['at']:%H:%M} (PLC limit {fc['limit']:g} {fc['unit']}, "
                        f"{fc['file']})"
                    ]
                    if fc
                    else []
                ),
                "why": c["cause"],
            }
        )
    return sorted(orders, key=lambda o: -o["priority"])


def summary(o: dict, rupees) -> str:
    """Return a one-paragraph summary of a work order (what, when, why it pays to do it now)."""
    head = {"N": "Do now", "B": "Do first", "W": "Check when passing"}.get(o["when"][0], "Plan")
    conf = f", {o['confidence']}% confident" if o["confidence"] is not None and not o["fault"] else ""
    if o["fault"]:
        tail = (
            f"Repair takes about {o['unplanned_min']} min now that it has broken; every minute the line is "
            f"down costs {rupees(o['at_risk'] / o['unplanned_min'])}."
        )
    else:
        tail = (
            f"About {o['planned_min']} min if planned, {o['unplanned_min']} min if it breaks first: fixing it "
            f"early saves ≈{rupees(o['save_if_planned'])} of line stop."
        )
    return f"{head}: {o['machine'].lower()}, {o['problem'].lower()}{conf}. {o['slot']} {tail}"


# ── downloadable documents ────────────────────────────────────────────────
CSS = """body{font-family:Segoe UI,Helvetica,Arial,sans-serif;max-width:820px;margin:24px auto;padding:0 16px;color:#1f2328}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:22px 0 6px;border-bottom:1px solid #ddd;padding-bottom:4px}
.meta{color:#666;font-size:13px}.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;
background:#eee;margin-left:6px}.now{background:#fde2e1;color:#a40e26}.soon{background:#fff1cc;color:#7a4d00}
.ok{background:#e6f4ea;color:#1e6b34}table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}
td,th{border:1px solid #ddd;padding:5px 7px;text-align:left;vertical-align:top}th{background:#f6f8fa}
ul{margin:4px 0 4px 18px;padding:0}li{margin:2px 0}.box{border:1px solid #ddd;border-radius:8px;padding:10px 14px;margin:10px 0}
.sign{margin-top:28px;font-size:13px}.sign span{display:inline-block;width:45%;border-top:1px solid #999;padding-top:4px;margin-right:4%}
@media print{body{margin:0}.box{break-inside:avoid}}"""


def _e(x):
    return html.escape(str(x))


def work_order_html(orders, now, rupees, plant="Bottling line 1"):
    """Return printable HTML for the work orders."""
    parts = [
        f"<!doctype html><meta charset='utf-8'><title>Work orders {now:%H:%M}</title><style>{CSS}</style>",
        f"<h1>Maintenance work orders</h1><div class='meta'>{_e(plant)} · created by Talking Machines at "
        f"line time {now:%d %b %Y %H:%M} · read-only monitoring: nothing was changed on the machines</div>",
    ]
    if not orders:
        parts.append("<p>No work orders: every machine looks normal.</p>")
    for i, o in enumerate(orders, 1):
        cls = "now" if o["when"].startswith("Now") else ("soon" if o["when"].startswith("Before") else "ok")
        conf = "fault is on" if o["fault"] else (f"{o['confidence']}% confident" if o["confidence"] is not None else "")
        parts.append(
            f"<div class='box'><h2>{i}. {_e(o['machine'])}: {_e(o['problem'])}"
            f"<span class='tag {cls}'>{_e(o['when'])}</span><span class='tag'>{_e(conf)}</span></h2>"
            f"<p><b>When:</b> {_e(o['slot'])}<br><b>Time:</b> "
            + (
                f"about {o['unplanned_min']} min (it has already broken)"
                if o["fault"]
                else f"about {o['planned_min']} min planned · {o['unplanned_min']} min if it breaks first"
            )
            + f"<br><b>Why we think so:</b> {_e(o['why'])}</p>"
            + (
                "<b>Evidence</b><ul>" + "".join(f"<li>{_e(x)}</li>" for x in o["evidence"]) + "</ul>"
                if o["evidence"]
                else ""
            )
            + "<b>Checklist</b><ul>"
            + "".join(f"<li>☐ {_e(x)}</li>" for x in o["checklist"])
            + "</ul>"
            + "<table><tr><th>Parts to bring</th><th>Tools</th></tr><tr><td>"
            + (", ".join(map(_e, o["parts"])) or "–")
            + "</td><td>"
            + ", ".join(map(_e, o["tools"]))
            + "</td></tr></table>"
            + "<p class='meta'>Found / done: ________________________________________________</p></div>"
        )
    parts.append("<div class='sign'><span>Technician</span><span>Shift lead</span></div>")
    return "\n".join(parts)


def shift_report_html(
    df, cards, orders, cost, events, warns, rupees, names, preds=(), forest_found=(), plant="Bottling line 1"
):
    """Return printable HTML for the shift report."""
    now, start = df["timestamp"].iloc[-1], df["timestamp"].iloc[0]
    out = [
        f"<!doctype html><meta charset='utf-8'><title>Shift report {now:%H:%M}</title><style>{CSS}</style>",
        f"<h1>Shift report</h1><div class='meta'>{_e(plant)} · {start:%d %b %Y %H:%M}–{now:%H:%M} · "
        f"Talking Machines (read-only)</div>",
    ]
    lost = cost["rupees"] if cost else 0
    out.append(
        f"<h2>Summary</h2><table><tr><th>Output lost</th><td>{cost['bottles']:,.0f} bottles ≈ {rupees(lost)}</td></tr>"
        if cost
        else "<h2>Summary</h2><table>"
    )
    down = [c for c in cards if c["status"] == "FAULT"]
    out.append(
        f"<tr><th>Machines in fault now</th><td>{_e(', '.join(c['machine'] for c in down) or 'none')}</td></tr>"
        f"<tr><th>Open alerts</th><td>{len([c for c in cards if c['status'] != 'FAULT'])}</td></tr>"
        f"<tr><th>Work orders</th><td>{len(orders)}</td></tr></table>"
    )
    if cost and cost["events"]:
        out.append(
            "<h2>Stoppages and their cost</h2><table><tr><th>From</th><th>Cause</th><th>Minutes</th>"
            "<th>Bottles lost</th><th>Cost</th></tr>"
        )
        for e in cost["events"]:
            out.append(
                f"<tr><td>{e['start']:%H:%M}</td><td>{_e(names.get(e['cause'], 'Unexplained'))}</td>"
                f"<td>{e['minutes']:.1f}</td><td>{e['bottles']:,.0f}</td><td>{rupees(e['rupees'])}"
                f"{' (still down)' if e['ongoing'] else ''}</td></tr>"
            )
        out.append("</table>")
    out.append("<h2>Alerts</h2>")
    if cards:
        out.append("<table><tr><th>Alert</th><th>Severity</th><th>Confidence</th><th>Since</th></tr>")
        for c in cards:
            conf = (
                "fault"
                if c["status"] == "FAULT"
                else (f"{c['confidence_pct']}%" if "confidence_pct" in c else c["confidence"])
            )
            out.append(
                f"<tr><td>{_e(c['title'])}</td><td>{_e(c['severity'])}</td><td>{_e(conf)}</td>"
                f"<td>{c['first_seen']:%H:%M}</td></tr>"
                if c.get("first_seen") is not None
                else f"<tr><td>{_e(c['title'])}</td><td>{_e(c['severity'])}</td><td>{_e(conf)}</td><td>–</td></tr>"
            )
        out.append("</table>")
    else:
        out.append("<p>No alerts.</p>")
    if preds:
        out.append(
            "<h2>Forecasts</h2><ul>"
            + "".join(
                f"<li>{_e(names.get(p['machine'], p['machine']))}: trips at about {p['at']:%H:%M} "
                f"({p['low_at']:%H:%M}–{p['high_at']:%H:%M}), PLC limit {p['limit']:g} {_e(p['unit'])} ({_e(p['file'])})</li>"
                for p in preds
            )
            + "</ul>"
        )
    if events:
        out.append("<h2>What happened</h2><ul>" + "".join(f"<li>{_e(e)}</li>" for e in events) + "</ul>")
    if warns:
        out.append("<h2>Early warnings</h2><ul>" + "".join(f"<li>{_e(w)}</li>" for w in warns) + "</ul>")
    out.append(
        "<h2>Work orders (most urgent first)</h2><ol>"
        + "".join(f"<li><b>{_e(o['machine'])}: {_e(o['problem'])}</b>: {_e(o['slot'])}</li>" for o in orders)
        + "</ol>"
        if orders
        else "<h2>Work orders</h2><p>None.</p>"
    )
    out.append("<div class='sign'><span>Shift lead</span><span>Maintenance</span></div>")
    return "\n".join(out)
