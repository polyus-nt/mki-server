"""Wire-контракт Mega реактора, узел 17. Скорости: signed milli-units."""
from .protocol import Command

READ = {
    "timer": Command("get_reactor_timer", 0x6b, 17, 3, "reactor_timer"),
    "temperature": Command("get_reactor_temp", 0x60, 17, 1, "temperature"),
    "motor": Command("get_motor", 0x66, 17, 3, "reactor_motor"),
    "config": Command("get_motor_config", 0x67, 17, 3, "reactor_config"),
    "timing": Command("get_motor_timing", 0x68, 17, 3, "reactor_timing"),
    "voltage": Command("get_stm_voltage", 0x72, 17, 3, "reactor_voltage"),
    "current": Command("get_stm_current", 0x73, 17, 3, "reactor_current"),
    "voltage_setpoint": Command("get_stm_voltage_setpoint", 0x74, 17, 3, "reactor_voltage_setpoint"),
    "current_setpoint": Command("get_stm_current_setpoint", 0x75, 17, 3, "reactor_current_setpoint"),
}
WRITE = {
    "timer": Command("set_reactor_timer", 0x6c, 17, 3, "ack"),
    "sps": Command("set_motor_sps", 0x61, 17, 3, "ack"),
    "rpm": Command("set_motor_rpm", 0x62, 17, 3, "ack"),
    "stop": Command("stop_motor", 0x63, 17, 3, "ack"),
    "enable": Command("set_motor_enabled", 0x64, 17, 3, "ack"),
    "micro": Command("set_motor_microstep", 0x65, 17, 3, "ack"),
    "stop_all": Command("stop_all_motors", 0x6a, 17, 1, "ack"),
    "voltage": Command("set_stm_voltage", 0x70, 17, 3, "reactor_voltage_setpoint"),
    "current": Command("set_stm_current", 0x71, 17, 3, "reactor_current_setpoint"),
}

def decode(layout, reply):
    d = reply.data
    if layout == "reactor_timer":
        seconds=int.from_bytes(d[:4],"big")
        minutes=int.from_bytes(d[4:6],"big")
        status=d[6]
        if (d[7] or status>2 or seconds>minutes*60 or
                (status==1 and (not seconds or not minutes)) or
                (status!=1 and seconds) or (status==0 and minutes) or
                (status==2 and not minutes)):
            raise ValueError("Invalid reactor timer state")
        return {"idx":reply.idx,"duration_minutes":minutes,"remaining_seconds":seconds,
                "state":("idle","running","finished")[status]}
    if layout == "reactor_motor":
        if any(abs(int.from_bytes(d[offset:offset+4],"big",signed=True))>7000000 for offset in (0,4)):
            raise ValueError("Motor speed out of range")
        return {"idx": reply.idx, "current_sps": int.from_bytes(d[:4], "big", signed=True)/1000,
                "target_sps": int.from_bytes(d[4:], "big", signed=True)/1000}
    if layout == "reactor_config":
        if any(v > 1 for v in d[:3]) or d[3] not in (4,8,16,32) or any(d[6:]) or not int.from_bytes(d[4:6],"big"):
            raise ValueError("Invalid motor configuration")
        return {"enabled": bool(d[0]), "running": bool(d[1]), "ramping": bool(d[2]),
                "microstep": d[3], "full_steps_per_rev": int.from_bytes(d[4:6],"big")}
    if layout == "reactor_timing":
        if any(d[6:]):
            raise ValueError("Invalid timing padding")
        return {"max_late_us": int.from_bytes(d[:2],"big"), "resyncs": int.from_bytes(d[2:6],"big")}
    if any(d[4:]):
        raise ValueError("Invalid STM scalar padding")
    v = int.from_bytes(d[:4], "big", signed=layout=="reactor_voltage")
    if (layout=="reactor_voltage_setpoint" and v>6000) or (layout=="reactor_current_setpoint" and v>50000):
        raise ValueError("STM setpoint out of range")
    unit = "mV" if "voltage" in layout else "mA"
    if layout in ("reactor_voltage","reactor_current","reactor_current_setpoint"):
        v /= 1000
    return {"idx": reply.idx, "value": v, "unit": unit}
