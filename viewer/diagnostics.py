"""Vehicle and device diagnostics from the qlogs: braking, steering, CAN bus health, the 12 V supply,
device temperatures, the radar, driver monitoring, learned calibration, alerts and the car's modules.

Each segment is read once (direct field access, no to_dict) into a small dict of totals, extremes and
counts; the segments of a drive are merged into one dict; flag_findings() turns that into a short list of
things worth a look, from the rules in FLAG_RULES. The segment results are cached by qlogs.cached_segments
like the drive summary, so a page never parses a log. present() chooses the units for display.

Times are seconds estimated from each message's usual rate (NOMINAL_DT), so they are approximate.
"""
from pathlib import Path

from .qlogs import _found_segments, cached_only, cached_segments, read_events

CACHE_VERSION = 1  # bump whenever parse_segment's output or logic changes

G = 9.80665                 # m/s2 per g
MOVING_MPS = 1.0            # "moving" means faster than this
LEAD_MIN_SPEED_MPS = 5.0    # radar readings below this speed (11 mph) are ignored: parking lots, glitches
HARD_BRAKE_MPS2 = 3.0       # braking harder than this (0.31 g) is a hard-braking event
LOW_VOLTAGE_MV = 13000      # the alternator keeps the battery above this while the car is running

# how often each message arrives in a qlog, in seconds (observed on real recordings)
NOMINAL_DT = {"carState": 0.1, "pandaStates": 0.1, "peripheralState": 0.5, "deviceState": 0.5,
              "radarState": 0.25, "driverMonitoringState": 0.5}

# The rules behind the flags, in one place. "warning" findings need a look, "info" ones are just notable.
FLAG_RULES = {
    "can_rec": 96,              # peak CAN receive error counter (96 is the controller's own warning level)
    "eps_fault_s": 3.0,         # longest stretch of a temporary steering fault, seconds
    "low_voltage_s": 60.0,      # time below LOW_VOLTAGE_MV while driving, seconds
    "temp_c": 80.0,             # device temperature, degrees C
    "seatbelt_s": 5.0,          # seatbelt unlatched while driving, seconds
    "hard_brakes": 1,           # info: at least this many hard-braking events
}

# onroad alerts worth naming in the list of alerts, with a readable label
ALERT_LABELS = {
    "steerSaturated": "Turn too tight for the system", "steerTempUnavailable": "Steering temporarily unavailable",
    "steerUnavailable": "Steering unavailable", "canError": "CAN bus error", "canBusMissing": "CAN bus missing",
    "controlsFailed": "Controls failed", "overheat": "Device overheating", "outOfSpace": "Device out of storage",
    "lowBattery": "Low battery", "commIssue": "Process communication issue", "posenetInvalid": "Camera pose invalid",
    "vehicleModelInvalid": "Vehicle model invalid", "driverDistracted1": "Driver distraction warning",
    "driverDistracted2": "Driver distraction warning (stronger)", "driverDistracted3": "Driver distraction (critical)",
    "tooDistracted": "Too distracted", "laneChange": "Lane change", "preLaneChangeLeft": "Lane change left (ready)",
    "preLaneChangeRight": "Lane change right (ready)", "fcw": "Forward collision warning",
    "cruiseMismatch": "Cruise speed mismatch", "wrongGear": "Wrong gear", "doorOpen": "Door open",
    "seatbeltNotLatched": "Seatbelt not latched", "steerOverride": "Steering override",
    "gasPressedOverride": "Gas pedal override", "preEnableStandstill": "Standstill before engaging",
    "buttonEnable": "Engaged with the button", "pcmEnable": "Engaged by the cruise control",
    "silentLkasDisable": "Steering assist turned off", "silentLkasEnable": "Steering assist turned on",
    "lkasEnable": "Steering assist turned on", "lkasDisable": "Steering assist turned off",
    "brakeHold": "Brake hold", "noGps": "No GPS", "selfdriveInitializing": "System starting",
}


# ----------------------
# Small helpers: extremes and "stretches"
# ----------------------

def _lo(d, key, value):
    if value is None:
        return
    d[key] = value if d.get(key) is None else min(d[key], value)


def _hi(d, key, value):
    if value is None:
        return
    d[key] = value if d.get(key) is None else max(d[key], value)


class _Run:
    """Time spent in a condition, and its longest unbroken stretch, kept so that stretches can be
    joined across segment boundaries (the leading and trailing parts are remembered)."""

    def __init__(self):
        self.total = self.longest = self.current = self.length = 0.0
        self.lead = None  # length of the stretch at the start, once the condition has ended once

    def add(self, active, dt):
        self.length += dt
        if active:
            self.total += dt
            self.current += dt
            self.longest = max(self.longest, self.current)
        else:
            if self.lead is None:
                self.lead = self.current
            self.current = 0.0

    def result(self):
        return {"total": round(self.total, 2), "longest": round(self.longest, 2),
                "lead": round(self.length if self.lead is None else self.lead, 2),
                "trail": round(self.current, 2), "length": round(self.length, 2)}


def merge_runs(a, b, join=True):
    """Combine the runs of two consecutive segments; `join` is False when segments are missing between them."""
    joined = a["trail"] + b["lead"] if join else 0.0
    lead = a["lead"] if (a["lead"] < a["length"] or not join) else a["length"] + b["lead"]
    trail = b["trail"] if (b["trail"] < b["length"] or not join) else b["length"] + a["trail"]
    return {"total": round(a["total"] + b["total"], 2), "longest": round(max(a["longest"], b["longest"], joined), 2),
            "lead": round(lead, 2), "trail": round(trail, 2), "length": round(a["length"] + b["length"], 2)}


# ----------------------
# One segment
# ----------------------

def parse_segment(qlog_path):
    """Read one qlog.zst into a dict of totals, extremes and counts (None where nothing was seen)."""
    s = {
        "dur": 0.0, "moving": 0.0, "standstill": 0.0, "brake_pedal": 0.0, "gas_pedal": 0.0,
        "hard": {"count": 0, "first": False, "last": False},
        "sat_s": 0.0, "can_invalid_s": 0.0, "seatbelt_s": 0.0, "door_s": 0.0, "blinker_s": 0.0,
        "rec_max": [None, None, None], "err_passive_s": 0.0, "err_warning_s": 0.0,
        "resets": {}, "tx_blocked": {}, "can_errors": {}, "volt": {"sum": 0.0, "s": 0.0},
        "lead": {"present_s": 0.0}, "dm": {"s": 0.0, "face_s": 0.0, "distracted_s": 0.0, "phone_s": 0.0, "pose_s": 0.0},
        "calib": {}, "events": {}, "fw": None, "thermal": None,
    }
    eps, low_volt = _Run(), _Run()
    moving, previous_names, in_hard, first_car, v_ego = False, None, False, True, 0.0
    thermal_rank = {}

    for event in read_events(qlog_path):
        kind = event.which()
        if kind == "carState":
            c = event.carState
            dt = NOMINAL_DT["carState"]
            v_ego = c.vEgo
            moving = c.vEgo > MOVING_MPS
            s["dur"] += dt
            _hi(s, "v_max", c.vEgo)
            if moving:
                s["moving"] += dt
                _lo(s, "a_min", c.aEgo)
                _hi(s, "a_max", c.aEgo)
                _hi(s, "brake_max", c.brakeDEPRECATED)
                s["brake_pedal"] += dt if c.brakePressed else 0.0
                s["gas_pedal"] += dt if c.gasPressed else 0.0
                s["seatbelt_s"] += dt if c.seatbeltUnlatched else 0.0
                s["door_s"] += dt if c.doorOpen else 0.0
                s["blinker_s"] += dt if (c.leftBlinker or c.rightBlinker) else 0.0
                hard = c.aEgo < -HARD_BRAKE_MPS2
                if first_car:
                    s["hard"]["first"] = hard
                if hard and not in_hard:
                    s["hard"]["count"] += 1
                in_hard = hard
                first_car = False
            else:
                in_hard = False
            s["standstill"] += dt if c.standstill else 0.0
            _lo(s, "angle_min", c.steeringAngleDeg)
            _hi(s, "angle_max", c.steeringAngleDeg)
            _hi(s, "rate_max", abs(c.steeringRateDeg))
            _hi(s, "torque_max", abs(c.steeringTorque))
            eps.add(bool(c.steerFaultTemporary), dt)
            s["can_invalid_s"] += 0.0 if c.canValid else dt
            _hi(s, "can_err_counter_max", c.canErrorCounter)
            if c.cruiseState.enabled and c.vCruise < 250:
                _hi(s, "set_speed_kph", c.vCruise)
        elif kind == "carOutput":
            _hi(s, "eps_cmd_max", abs(event.carOutput.actuatorsOutput.torqueOutputCan))
        elif kind == "controlsState":
            lateral = event.controlsState.lateralControlState
            if lateral.which() == "torqueState":
                torque = lateral.torqueState
                _hi(s, "lat_acc_max", abs(torque.actualLateralAccel))
                s["sat_s"] += NOMINAL_DT["carState"] if torque.saturated else 0.0
        elif kind == "pandaStates":
            dt = NOMINAL_DT["pandaStates"]
            for panda in event.pandaStates:
                for bus, state in enumerate((panda.canState0, panda.canState1, panda.canState2)):
                    cur = s["rec_max"][bus]
                    s["rec_max"][bus] = state.receiveErrorCnt if cur is None else max(cur, state.receiveErrorCnt)
                    s["err_passive_s"] += dt if state.errorPassive else 0.0
                    s["err_warning_s"] += dt if state.errorWarning else 0.0
                    _lo(s["resets"], str(bus), state.canCoreResetCnt)
                    _hi(s["resets"], f"{bus}max", state.canCoreResetCnt)
                    _lo(s["can_errors"], str(bus), state.totalErrorCnt)
                    _hi(s["can_errors"], f"{bus}max", state.totalErrorCnt)
                _lo(s["tx_blocked"], "min", panda.safetyTxBlocked)
                _hi(s["tx_blocked"], "max", panda.safetyTxBlocked)
        elif kind == "peripheralState":
            p, dt, mv = event.peripheralState, NOMINAL_DT["peripheralState"], event.peripheralState.voltage
            _hi(s, "current_max", p.current)
            _hi(s, "fan_max", p.fanSpeedRpm)
            if mv > 6000:  # below this the reading is not the car's supply
                _lo(s["volt"], "min", mv)
                _hi(s["volt"], "max", mv)
                if moving:
                    _lo(s["volt"], "moving_min", mv)
                    s["volt"]["sum"] += mv * dt
                    s["volt"]["s"] += dt
                low_volt.add(moving and mv < LOW_VOLTAGE_MV, dt)
        elif kind == "deviceState":
            d = event.deviceState
            _hi(s, "temp_max", d.maxTempC)
            _hi(s, "mem_max", d.memoryUsagePercent)
            _lo(s, "free_min", d.freeSpacePercent)
            _hi(s, "power_max", d.somPowerDrawW)
            name = str(d.thermalStatus)
            thermal_rank.setdefault("seen", set()).add(name)
        elif kind == "radarState":
            lead = event.radarState.leadOne
            if lead.status and moving:
                s["lead"]["present_s"] += NOMINAL_DT["radarState"]
                if v_ego > LEAD_MIN_SPEED_MPS and lead.dRel > 0.5:
                    _lo(s["lead"], "d_min", lead.dRel)
                    _lo(s["lead"], "gap_min_s", lead.dRel / v_ego)
                    _hi(s["lead"], "closing_max", -lead.vRel)
        elif kind == "driverMonitoringState":
            m, dt = event.driverMonitoringState, NOMINAL_DT["driverMonitoringState"]
            v = m.visionPolicyState
            s["dm"]["s"] += dt
            s["dm"]["face_s"] += dt if v.faceDetected else 0.0
            s["dm"]["distracted_s"] += dt if v.isDistracted else 0.0
            s["dm"]["phone_s"] += dt if v.distractedTypes.phone else 0.0
            s["dm"]["pose_s"] += dt if v.distractedTypes.pose else 0.0
            _lo(s["dm"], "vision_awareness_min", v.awarenessPercent)
            _lo(s["dm"], "wheel_awareness_min", m.wheeltouchPolicyState.awarenessPercent)
            _hi(s["dm"], "fallback_max", v.wheeltouchFallbackPercent)
        elif kind == "liveParameters":
            p = event.liveParameters
            s["calib"].update(steer_ratio=p.steerRatio, angle_offset_deg=p.angleOffsetAverageDeg, stiffness=p.stiffnessFactor)
        elif kind == "liveTorqueParameters":
            p = event.liveTorqueParameters
            s["calib"].update(lat_acc_factor=p.latAccelFactorFiltered, friction=p.frictionCoefficientFiltered)
        elif kind == "liveDelay":
            s["calib"]["lateral_delay_s"] = event.liveDelay.lateralDelay
        elif kind == "onroadEvents":
            names = {str(e.name) for e in event.onroadEvents}
            if previous_names is not None:  # events already active when the segment starts are not new
                for name in names - previous_names:
                    s["events"][name] = s["events"].get(name, 0) + 1
            previous_names = names
        elif kind == "carParams" and s["fw"] is None:
            s["fw"] = [[str(x.ecu), x.brand, bytes(x.fwVersion).hex()] for x in event.carParams.carFw]
    s["hard"]["last"] = in_hard
    s["eps_fault"] = eps.result()
    s["low_voltage"] = low_volt.result()
    s["thermal"] = sorted(thermal_rank.get("seen", []))
    for key in ("dur", "moving", "standstill", "brake_pedal", "gas_pedal", "sat_s", "can_invalid_s", "seatbelt_s",
                "door_s", "blinker_s", "err_passive_s", "err_warning_s"):
        s[key] = round(s[key], 2)
    return s


# ----------------------
# A whole drive
# ----------------------

_SUM_KEYS = ("dur", "moving", "standstill", "brake_pedal", "gas_pedal", "sat_s", "can_invalid_s", "seatbelt_s",
             "door_s", "blinker_s", "err_passive_s", "err_warning_s")
_MAX_KEYS = ("v_max", "a_max", "brake_max", "angle_max", "rate_max", "torque_max", "can_err_counter_max",
             "set_speed_kph", "eps_cmd_max", "lat_acc_max", "current_max", "fan_max", "temp_max", "mem_max",
             "power_max")
_MIN_KEYS = ("a_min", "angle_min", "free_min")


def merge(segments):
    """Combine {segment number: parse_segment result} into one dict for the whole drive."""
    m = {k: 0.0 for k in _SUM_KEYS}
    m.update(segments=len(segments), hard=0, eps_fault=None, low_voltage=None, rec_max=[None, None, None], events={}, calib={}, fw=None, thermal=set(),
             volt={"sum": 0.0, "s": 0.0}, lead={"present_s": 0.0}, dm={"s": 0.0, "face_s": 0.0, "distracted_s": 0.0,
                                                                    "phone_s": 0.0, "pose_s": 0.0})
    resets, errors, txb = {}, {}, {}
    previous = None
    for number in sorted(segments):
        s = segments[number]
        for k in _SUM_KEYS:
            m[k] += s[k]
        for k in _MAX_KEYS:
            _hi(m, k, s.get(k))
        for k in _MIN_KEYS:
            _lo(m, k, s.get(k))
        joined = previous is not None and number == previous[0] + 1
        m["hard"] += s["hard"]["count"] - (1 if joined and previous[1]["hard"]["last"] and s["hard"]["first"] else 0)
        for bus, value in enumerate(s["rec_max"]):
            if value is not None:
                m["rec_max"][bus] = value if m["rec_max"][bus] is None else max(m["rec_max"][bus], value)
        for src, dst in ((s["resets"], resets), (s["can_errors"], errors)):
            for key, value in src.items():
                (_hi if key.endswith("max") else _lo)(dst, key, value)
        for key, value in s["tx_blocked"].items():
            (_hi if key == "max" else _lo)(txb, key, value)
        for k in ("min", "moving_min"):
            _lo(m["volt"], k, s["volt"].get(k))
        _hi(m["volt"], "max", s["volt"].get("max"))
        m["volt"]["sum"] += s["volt"]["sum"]
        m["volt"]["s"] += s["volt"]["s"]
        for k in ("d_min", "gap_min_s"):
            _lo(m["lead"], k, s["lead"].get(k))
        _hi(m["lead"], "closing_max", s["lead"].get("closing_max"))
        m["lead"]["present_s"] += s["lead"]["present_s"]
        for k in ("s", "face_s", "distracted_s", "phone_s", "pose_s"):
            m["dm"][k] += s["dm"][k]
        for k in ("vision_awareness_min", "wheel_awareness_min"):
            _lo(m["dm"], k, s["dm"].get(k))
        _hi(m["dm"], "fallback_max", s["dm"].get("fallback_max"))
        m["calib"].update(s["calib"])
        for name, n in s["events"].items():
            m["events"][name] = m["events"].get(name, 0) + n
        m["fw"] = m["fw"] or s["fw"]
        m["thermal"].update(s["thermal"])
        for k in ("eps_fault", "low_voltage"):
            m[k] = s[k] if m[k] is None else merge_runs(m[k], s[k], joined)
        previous = (number, s)
    m["resets"] = {b: (resets.get(f"{b}max", 0) - resets.get(str(b), 0)) for b in range(3)}
    m["can_total_errors"] = {b: (errors.get(f"{b}max", 0) - errors.get(str(b), 0)) for b in range(3)}
    m["tx_blocked_delta"] = (txb.get("max", 0) - txb.get("min", 0)) if txb else 0
    m["thermal"] = sorted(m["thermal"])
    for k in ("eps_fault", "low_voltage"):
        if m[k] is None:
            m[k] = _Run().result()
    return m


def route_diagnostics(route_id, raw_dir, cache_dir):
    """Parse (or read from the cache) a drive's segments and present them; see present()."""
    segments = cached_segments(route_id, raw_dir, Path(cache_dir) / f"{route_id}.json", parse_segment, CACHE_VERSION)
    return present(merge(segments), _expected_segments(route_id, raw_dir), len(segments))


def cached_diagnostics(route_id, raw_dir, cache_dir):
    """The presented diagnostics if every segment is cached already, else None. Never parses a log."""
    data, complete = cached_only(route_id, raw_dir, Path(cache_dir) / f"{route_id}.json", CACHE_VERSION)
    if not complete:
        return None
    return present(merge(data), _expected_segments(route_id, raw_dir), len(data))


def _expected_segments(route_id, raw_dir):
    found = _found_segments(route_id, raw_dir)
    return (max(found) + 1) if found else 0


# ----------------------
# Findings and display
# ----------------------

def flag_findings(m, expected=0):
    """The things worth a look, each {level, text, rule}; the rules are in FLAG_RULES above."""
    r, out = FLAG_RULES, []
    peak = max([x for x in m["rec_max"] if x is not None], default=0)
    if peak >= r["can_rec"] or m["events"].get("canError"):
        out.append(("warning", f"CAN bus errors: receive error counter reached {peak}",
                    f"warns when the counter reaches {r['can_rec']} or the car logs a CAN error alert"))
    if m["eps_fault"]["longest"] >= r["eps_fault_s"]:
        out.append(("warning", f"Steering was temporarily unavailable for {_dur(m['eps_fault']['longest'])} in a row",
                    f"warns at {r['eps_fault_s']:g} s or more in a row"))
    if m["low_voltage"]["total"] >= r["low_voltage_s"]:
        out.append(("warning", f"12 V supply below {LOW_VOLTAGE_MV / 1000:g} V for {_dur(m['low_voltage']['total'])} while driving",
                    f"warns at {r['low_voltage_s']:g} s or more"))
    if (m.get("temp_max") or 0) > r["temp_c"]:
        out.append(("warning", f"Device reached {m['temp_max']:.0f} °C", f"warns above {r['temp_c']:g} °C"))
    if m["seatbelt_s"] >= r["seatbelt_s"]:
        out.append(("warning", f"Seatbelt unlatched for {_dur(m['seatbelt_s'])} while driving", f"warns at {r['seatbelt_s']:g} s or more"))
    if expected and m["segments"] < expected:
        out.append(("warning", f"{expected - m['segments']} of {expected} log segments could not be read",
                    "the numbers below cover only the readable parts"))
    if m["hard"] >= r["hard_brakes"]:
        out.append(("info", f"{m['hard']} hard-braking event(s)", f"notes braking harder than {HARD_BRAKE_MPS2 / G:.2f} g"))
    return [{"level": l, "text": t, "rule": rule} for l, t, rule in out]


def _dur(seconds):
    from .labels import format_duration
    return format_duration(int(round(seconds))) or "0 sec"


def _g(mps2):
    return f"{abs(mps2) / G:.2f} g ({abs(mps2):.1f} m/s²)"


def _ft(m):
    return f"{m * 3.28084:.0f} ft"


def _pct(part, whole):
    return f"{100 * part / whole:.0f}%" if whole else "–"


def _temp(c):
    return f"{c:.0f} °C ({c * 9 / 5 + 32:.0f} °F)"


def _printable(hexstr):
    try:
        raw = bytes.fromhex(hexstr)
    except ValueError:
        return hexstr
    text = raw.decode("ascii", "replace").strip("\x00 ")
    return text if text and all(32 <= ord(ch) < 127 for ch in text) else hexstr


def present(m, expected=0, found=None):
    """Display-ready {flags, sections:[{title, rows:[[label, value]]}], segments}: units are chosen here."""
    mph = 2.2369363
    moving = m["moving"]
    volt, lead, dm = m["volt"], m["lead"], m["dm"]
    rows = lambda *pairs: [list(p) for p in pairs if p[1] is not None]  # noqa: E731
    sections = [
        {"title": "Braking and driving", "rows": rows(
            ("Hardest braking", _g(m["a_min"]) if m.get("a_min") is not None else None),
            ("Hard-braking events", f"{m['hard']} (braking harder than {HARD_BRAKE_MPS2 / G:.2f} g)"),
            ("Strongest acceleration", _g(m["a_max"]) if m.get("a_max") is not None else None),
            ("Peak brake level (car's own unit)", f"{m['brake_max']:.2f}" if m.get("brake_max") is not None else None),
            ("Brake pedal pressed", f"{_dur(m['brake_pedal'])} ({_pct(m['brake_pedal'], moving)} of moving time)"),
            ("Gas pedal pressed", f"{_dur(m['gas_pedal'])} ({_pct(m['gas_pedal'], moving)} of moving time)"),
            ("Time at a standstill", _dur(m["standstill"])),
            ("Top speed", f"{m['v_max'] * mph:.0f} mph" if m.get("v_max") is not None else None),
            ("Cruise set speed (highest)", f"{m['set_speed_kph'] * 0.621371:.0f} mph" if m.get("set_speed_kph") else None))},
        {"title": "Steering", "rows": rows(
            ("Wheel angle range", f"{m['angle_min']:.0f}° to {m['angle_max']:.0f}°" if m.get("angle_min") is not None else None),
            ("Fastest wheel movement", f"{m['rate_max']:.0f}°/s" if m.get("rate_max") is not None else None),
            ("Strongest driver torque", f"{m['torque_max']:.1f}" if m.get("torque_max") is not None else None),
            ("Strongest steering command", f"{m['eps_cmd_max']:.0f}" if m.get("eps_cmd_max") is not None else None),
            ("Highest sideways acceleration", _g(m["lat_acc_max"]) if m.get("lat_acc_max") is not None else None),
            ("Steering at its limit", _dur(m["sat_s"])),
            ("Steering temporarily unavailable", f"{_dur(m['eps_fault']['total'])} (longest {_dur(m['eps_fault']['longest'])})"))},
        {"title": "CAN bus and panda", "rows": rows(
            ("Peak receive errors per bus", ", ".join(f"bus {i}: {v}" for i, v in enumerate(m["rec_max"]) if v is not None) or None),
            ("Time in error-passive", _dur(m["err_passive_s"])),
            ("Time in error-warning", _dur(m["err_warning_s"])),
            ("CAN controller resets", ", ".join(f"bus {i}: {v}" for i, v in m["resets"].items())),
            ("Errors counted", ", ".join(f"bus {i}: {v}" for i, v in m["can_total_errors"].items())),
            ("Messages blocked by safety", str(m["tx_blocked_delta"])),
            ("CAN data not valid", _dur(m["can_invalid_s"])),
            ("Highest error counter in car state", str(m["can_err_counter_max"]) if m.get("can_err_counter_max") is not None else None))},
        {"title": "12 V supply", "rows": rows(
            ("Average while moving", f"{volt['sum'] / volt['s'] / 1000:.1f} V" if volt["s"] else None),
            ("Lowest while moving", f"{volt['moving_min'] / 1000:.1f} V" if volt.get("moving_min") else None),
            ("Lowest overall", f"{volt['min'] / 1000:.1f} V" if volt.get("min") else None),
            ("Highest", f"{volt['max'] / 1000:.1f} V" if volt.get("max") else None),
            ("Below 13 V while moving", _dur(m["low_voltage"]["total"])),
            ("Peak current drawn", f"{m['current_max']} mA" if m.get("current_max") else None))},
        {"title": "Device", "rows": rows(
            ("Highest temperature", _temp(m["temp_max"]) if m.get("temp_max") is not None else None),
            ("Thermal states seen", ", ".join(m["thermal"]) or None),
            ("Fan (highest)", f"{m['fan_max']} rpm" if m.get("fan_max") is not None else None),
            ("Memory use (highest)", f"{m['mem_max']}%" if m.get("mem_max") is not None else None),
            ("Free storage (lowest)", f"{m['free_min']}%" if m.get("free_min") is not None else None),
            ("Power draw (highest)", f"{m['power_max']:.1f} W" if m.get("power_max") else None))},
        {"title": "Cars ahead (radar)", "rows": rows(
            ("A car was ahead", f"{_dur(lead['present_s'])} ({_pct(lead['present_s'], moving)} of moving time)"),
            ("Closest while above 11 mph", _ft(lead["d_min"]) if lead.get("d_min") is not None else None),
            ("Shortest gap", f"{lead['gap_min_s']:.1f} s" if lead.get("gap_min_s") is not None else None),
            ("Fastest closing speed", f"{lead['closing_max'] * mph:.0f} mph" if lead.get("closing_max") else None))},
        {"title": "Driver monitoring", "rows": rows(
            ("Face seen", _pct(dm["face_s"], dm["s"]) if dm["s"] else None),
            ("Distracted", _dur(dm["distracted_s"]) if dm["s"] else None),
            ("Phone use", _dur(dm["phone_s"]) if dm["s"] else None),
            ("Lowest attention level", f"{dm['vision_awareness_min'] * 100:.0f}%" if dm.get("vision_awareness_min") is not None else None))},
        {"title": "Learned car values", "rows": rows(
            ("Steering ratio", f"{m['calib']['steer_ratio']:.1f}" if "steer_ratio" in m["calib"] else None),
            ("Wheel angle offset", f"{m['calib']['angle_offset_deg']:.1f}°" if "angle_offset_deg" in m["calib"] else None),
            ("Torque factor", f"{m['calib']['lat_acc_factor']:.2f}" if "lat_acc_factor" in m["calib"] else None),
            ("Friction", f"{m['calib']['friction']:.2f}" if "friction" in m["calib"] else None),
            ("Steering delay", f"{m['calib']['lateral_delay_s']:.2f} s" if "lateral_delay_s" in m["calib"] else None))},
        {"title": "Alerts and events", "rows": [[ALERT_LABELS.get(n, n), f"{c}x"] for n, c in sorted(m["events"].items(), key=lambda x: -x[1])]},
        {"title": "Car modules", "rows": [[f"{ecu} ({brand})", _printable(fw)] for ecu, brand, fw in (m["fw"] or [])]},
        {"title": "Other", "rows": rows(
            ("Seatbelt unlatched while moving", _dur(m["seatbelt_s"])), ("Door open while moving", _dur(m["door_s"])),
            ("Blinker on", _dur(m["blinker_s"])))},
    ]
    return {"flags": flag_findings(m, expected), "sections": [s for s in sections if s["rows"]],
            "segments": m["segments"], "expected": expected}
