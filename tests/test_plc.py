"""The Modbus register encoding round-trips, and the PLC link refuses writes."""

import struct

import pytest

import plant
import plc


def test_encode_decode_round_trip_bool_int32_float32():
    regmap = {
        "RUN": {"addr": 10, "type": "bool"},
        "COUNT": {"addr": 11, "type": "int32"},
        "TEMP": {"addr": 13, "type": "float32"},
    }
    values = {"RUN": 1, "COUNT": 123456, "TEMP": 38.25}
    regs = plc.encode(values, regmap, clock=70000, size=plc.map_size(regmap))
    clock, out = plc.decode(regs, regmap)
    assert clock == 70000
    assert out == {"RUN": 1, "COUNT": 123456, "TEMP": 38.25}


def test_int32_negative_round_trip():
    regmap = {"N": {"addr": 2, "type": "int32"}}
    regs = plc.encode({"N": -42}, regmap, clock=0, size=plc.map_size(regmap))
    assert plc.decode(regs, regmap)[1] == {"N": -42}


@pytest.mark.parametrize("value", [7.25, -3.5, 0.0, 300.0])
def test_int16_with_scale_round_trip(value):
    spec = {"addr": 4, "type": "int16", "scale": 100}
    regs = [0] * 8
    regs[spec["addr"]] = plant.to_reg(value, spec)
    _, out = plc.decode(regs, {"V": spec})
    assert out["V"] == pytest.approx(value)


def test_plc_server_refuses_writes():
    server = plc.PLCServer(size=32, host="127.0.0.1", port=0)  # port 0: any free port
    port = server.server.server_address[1]
    server.update([0, 5] + [7] * 30)
    client = plc.PLCClient("127.0.0.1", port)
    try:
        # reads work
        assert client.read_registers(0, 4) == [0, 5, 7, 7]
        # a write (function 06) comes back as exception 01 "illegal function"
        resp = client._ask(struct.pack(">BHH", 6, 3, 99))
        assert resp[0] == 0x86 and resp[1] == 1
        assert client.try_write(3, 99) == "refused (read-only)"
        # and nothing changed
        assert client.read_registers(3, 1) == [7]
        assert server.refused_writes == 2
    finally:
        client.close()
        server.close()
