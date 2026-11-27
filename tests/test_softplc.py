"""The soft PLC runs the capper's real Structured Text, TON timer included."""

import random

import plc_programs
import simulator
import softplc


def _capper(torque):
    tags = simulator.make_tags(random.Random(42), 42)
    tag = {k: v["tag"] for k, v in tags.items()}
    prog = softplc.Program({"capper": plc_programs.PROGRAMS["capper"]}, tag)
    prog.vars[tag[("capper", "cap_torque")]] = torque
    prog.vars[tag[("capper", "running")]] = True
    prog.vars[tag[("filler", "fault")]] = False
    return prog, tag[("capper", "fault")]


def test_low_torque_for_more_than_2_s_raises_the_fault():
    prog, fault = _capper(torque=0.5)
    for _ in range(3):  # 1.5 s: the 2 s timer hasn't run out
        prog.scan(0.5)
    assert not prog.vars.get(fault, False)
    for _ in range(2):  # 2.5 s
        prog.scan(0.5)
    assert prog.vars[fault] is True


def test_normal_torque_never_raises_the_fault():
    prog, fault = _capper(torque=1.2)
    for _ in range(20):
        prog.scan(0.5)
    assert not prog.vars.get(fault, False)
