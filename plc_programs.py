"""The PLC programs that run each machine, in IEC 61131-3 Structured Text.

They are written with the plant's real (cryptic) tag names, exactly as an engineer would see them in the
PLC software. {machine.signal} placeholders are filled in with the tag names of the current run.
The logic matches what the simulator does, so the translator's explanations are true for this line.
The comments below are for us; they are stripped from the files the translator reads.
"""
import os
import re

PROGRAMS = {
    "depalletiser": """(* Depalletiser: unloads bottles from pallets onto the line *)
IF {filler.fault} THEN
    {depalletiser.running} := FALSE;    (* filler down: nowhere to send bottles *)
ELSE
    {depalletiser.running} := TRUE;
END_IF;
IF {depalletiser.running} THEN
    {depalletiser.speed} := 300.0;
ELSE
    {depalletiser.speed} := 0.0;
END_IF;
{depalletiser.layer_count} := {depalletiser.count} / 120;
""",
    "filler": """(* Filler: fills the bottles *)
TON_VIB(IN := {filler.vibration} > 7.2, PT := T#10S);
IF TON_VIB.Q THEN
    {filler.fault} := TRUE;             (* bearing protection trip *)
END_IF;
IF {filler.fault} THEN
    {filler.running} := FALSE;
    {filler.speed} := 0.0;
ELSIF NOT {capper.running} THEN
    {filler.speed} := 120.0;            (* capper not taking bottles *)
ELSE
    {filler.running} := TRUE;
    {filler.speed} := 300.0;
END_IF;
IF {filler.running} THEN
    {filler.fill_pressure} := 2.5;
ELSE
    {filler.fill_pressure} := 0.2;
END_IF;
""",
    "capper": """(* Capper: screws the caps on *)
TON_JAM(IN := {capper.cap_torque} < 0.8 AND {capper.running}, PT := T#2S);
IF TON_JAM.Q THEN
    {capper.fault} := TRUE;             (* cap jam *)
END_IF;
IF {capper.fault} AND JAM_CLEARED THEN
    {capper.fault} := FALSE;
END_IF;
IF {capper.fault} OR {filler.fault} THEN
    {capper.running} := FALSE;
    {capper.speed} := 0.0;
ELSE
    {capper.running} := TRUE;
    {capper.speed} := 300.0;
END_IF;
""",
    "labeller": """(* Labeller: glues the labels on *)
IF {labeller.glue_temp} < 150.0 THEN
    {labeller.fault} := TRUE;           (* glue too cold to stick *)
END_IF;
IF {filler.fault} OR {labeller.fault} THEN
    {labeller.running} := FALSE;
    {labeller.speed} := 0.0;
ELSIF NOT {capper.running} THEN
    {labeller.speed} := 60.0;           (* starved: few bottles arriving *)
ELSE
    {labeller.running} := TRUE;
    {labeller.speed} := 300.0;
END_IF;
""",
    "case_packer": """(* Case packer: packs 24 bottles per case *)
{case_packer.case_count} := {case_packer.count} / 24;
IF {filler.fault} THEN
    {case_packer.running} := FALSE;
ELSIF NOT {capper.running} THEN
    {case_packer.speed} := 60.0;        (* starved *)
ELSE
    {case_packer.running} := TRUE;
    {case_packer.speed} := 300.0;
END_IF;
""",
    "cobot": """(* Palletising cobot: stacks the cases on pallets *)
IF {cobot.zone_occupied} THEN
    {cobot.speed_override} := 30.0;     (* person nearby: collaborative speed *)
ELSE
    {cobot.speed_override} := 100.0;
END_IF;
IF {cobot.fault} THEN
    {cobot.running} := FALSE;
END_IF;
""",
}


def render(tags, out_dir="data/plc_programs"):
    """tags: {(machine, signal): {"tag": name, …}} from the simulator. Writes one .st file per machine."""
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        if f.endswith(".st"):
            os.remove(os.path.join(out_dir, f))
    # files are named by controller, not by machine: the engineer only sees cryptic program names
    for i, (machine, code) in enumerate(PROGRAMS.items(), 1):
        text = re.sub(r"\{(\w+)\.(\w+)\}", lambda m: tags[(m.group(1), m.group(2))]["tag"], code)
        name = "ROB1_MAIN" if machine == "cobot" else f"PLC{i}_OB1"
        # old plant code usually has no comments, so strip them: the translator must work without them
        text = re.sub(r"[ \t]*\(\*.*?\*\)", "", text).strip() + "\n"
        with open(os.path.join(out_dir, f"{name}.st"), "w", encoding="utf-8") as fh:
            fh.write(text)
    return out_dir