"""Drive summary from the qlogs: how long the pilot (openpilot) was in control of
steering and of speed versus the driver, plus speed, distance and events.

"Engaged" means openpilot is active for steering (carControl.latActive) or for speed
(carControl.longActive); the two are independent on this setup. Within engaged time:
  - steering is the pilot's while lateral control is active and the driver is not
    pushing the wheel, otherwise the driver's;
  - speed is the pilot's while longitudinal control is active and neither pedal is
    pressed, otherwise the driver's.
"""
from pathlib import Path

from .qlogs import cached_segments, read_events

CACHE_VERSION = 1
SEGMENT_SECONDS = 60
SAMPLE_CAP_S = 0.5  # a logging gap is not counted as driving time
GAP_S = 5.0  # series points further apart than this are drawn as a break (segments start ~1 s late)
M_S_TO_MPH = 2.2369363
METERS_PER_MILE = 1609.344

# series codes for the pilot/driver strips
NOT_ENGAGED, PILOT, DRIVER = 0, 1, 2

# onroad event names we report, and the type they are reported as
ONROAD_EVENTS = {
    "steerSaturated": "steer_saturated",
    "driverDistracted1": "driver_distracted",
    "driverDistracted2": "driver_distracted",
    "driverDistracted3": "driver_distracted",
    "laneChange": "lane_change",
}
# Minimum seconds between two events of the same type: light touches of the wheel or a
# pedal flicker, and escalating warnings are one incident, so they count as one episode.
MIN_GAP_S = {"wheel_override": 5.0, "pedal_override": 3.0, "driver_distracted": 10.0}
DEFAULT_MIN_GAP_S = 1.0

INTERRUPTION_TYPES = ("wheel_override", "pedal_override", "steer_off", "speed_off")


def parse_segment(qlog_path):
    """Summarise one qlog.zst (times are seconds from the start of the segment)."""
    samples = []  # (mono, vEgo, wheel, gas, brake, lat, long)
    onroad = []  # (mono, set of event names)
    lat = long_ = False
    first_mono = None

    for event in read_events(qlog_path):
        mono = event.logMonoTime
        if first_mono is None or mono < first_mono:
            first_mono = mono
        kind = event.which()
        if kind == "carControl":
            lat, long_ = event.carControl.latActive, event.carControl.longActive
        elif kind == "carState":
            c = event.carState
            samples.append((mono, c.vEgo, c.steeringPressed, c.gasPressed, c.brakePressed, lat, long_))
        elif kind == "onroadEvents":
            onroad.append((mono, {str(e.name) for e in event.onroadEvents}))

    summary = {
        "dur": 0.0, "dist": 0.0, "vmax": 0.0, "engaged": 0.0,
        "steer_pilot": 0.0, "speed_pilot": 0.0, "series": [], "events": [],
    }
    if not samples:
        return summary

    def seconds(mono):
        return (mono - first_mono) / 1e9

    last_event = {}
    events = []

    def add_event(t, kind):
        gap = MIN_GAP_S.get(kind, DEFAULT_MIN_GAP_S)
        if t - last_event.get(kind, -1e9) >= gap:
            last_event[kind] = t
            events.append([round(t, 1), kind])

    buckets = {}  # second -> [sum v*dt, sum dt, steer code, speed code]
    previous = None
    for i, (mono, v, wheel, gas, brake, lat, long_) in enumerate(samples):
        t = seconds(mono)
        following = seconds(samples[i + 1][0]) if i + 1 < len(samples) else t + 0.1
        dt = min(max(following - t, 0.0), SAMPLE_CAP_S)
        pedal = gas or brake

        summary["dur"] += dt
        summary["dist"] += v * dt
        summary["vmax"] = max(summary["vmax"], v)

        steer_code = speed_code = NOT_ENGAGED
        if lat or long_:
            summary["engaged"] += dt
            pilot_steer = lat and not wheel
            pilot_speed = long_ and not pedal
            summary["steer_pilot"] += dt if pilot_steer else 0.0
            summary["speed_pilot"] += dt if pilot_speed else 0.0
            steer_code = PILOT if pilot_steer else DRIVER
            speed_code = PILOT if pilot_speed else DRIVER

        bucket = buckets.setdefault(int(t), [0.0, 0.0, 0, 0])
        bucket[0] += v * dt
        bucket[1] += dt
        bucket[2], bucket[3] = steer_code, speed_code

        if previous is not None:
            p_wheel, p_pedal, p_lat, p_long = previous
            if wheel and lat and not (p_wheel and p_lat):
                add_event(t, "wheel_override")
            if pedal and long_ and not (p_pedal and p_long):
                add_event(t, "pedal_override")
            if p_lat and not lat:
                add_event(t, "steer_off")
            if p_long and not long_:
                add_event(t, "speed_off")
        previous = (wheel, pedal, lat, long_)

    # Events that are already active when the segment starts are not new.
    seen = None
    for mono, names in onroad:
        if seen is not None:
            for name in sorted(names - seen):
                if name in ONROAD_EVENTS:
                    add_event(seconds(mono), ONROAD_EVENTS[name])
        seen = names

    summary["series"] = [
        [second, round(b[0] / b[1], 2) if b[1] else 0.0, b[2], b[3]]
        for second, b in sorted(buckets.items())
    ]
    summary["events"] = sorted(events)
    for key in ("dur", "dist", "vmax", "engaged", "steer_pilot", "speed_pilot"):
        summary[key] = round(summary[key], 2)
    return summary


def _share(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def route_summary(route_id, raw_dir, cache_dir):
    """Merge a route's segment summaries into the JSON served to the drive page."""
    segments = cached_segments(
        route_id, raw_dir, Path(cache_dir) / f"{route_id}.json", parse_segment, CACHE_VERSION
    )
    total = dict.fromkeys(("dur", "dist", "engaged", "steer_pilot", "speed_pilot"), 0.0)
    vmax = 0.0
    series = {"t": [], "mph": [], "steer": [], "speed": []}
    events = []
    last_t = None

    for number in sorted(segments):
        seg = segments[number]
        for key in total:
            total[key] += seg[key]
        vmax = max(vmax, seg["vmax"])
        offset = number * SEGMENT_SECONDS
        for second, v, steer, speed in seg["series"]:
            t = offset + second
            if last_t is not None and t - last_t > GAP_S:
                # break the line at missing time
                series["t"].append(last_t + 1)
                series["mph"].append(None)
                series["steer"].append(NOT_ENGAGED)
                series["speed"].append(NOT_ENGAGED)
            series["t"].append(t)
            series["mph"].append(round(v * M_S_TO_MPH, 1))
            series["steer"].append(steer)
            series["speed"].append(speed)
            last_t = t
        events.extend([offset + t, kind] for t, kind in seg["events"])

    events.sort()
    counts = {}
    for _, kind in events:
        counts[kind] = counts.get(kind, 0) + 1
    engaged = total["engaged"]
    return {
        "segments": len(segments),
        "duration_s": round(total["dur"]),
        "engaged_s": round(engaged),
        "distance_mi": round(total["dist"] / METERS_PER_MILE, 2),
        "avg_mph": round(total["dist"] / total["dur"] * M_S_TO_MPH, 1) if total["dur"] else 0.0,
        "max_mph": round(vmax * M_S_TO_MPH, 1),
        "steer_pilot_pct": _share(total["steer_pilot"], engaged),
        "steer_driver_pct": _share(engaged - total["steer_pilot"], engaged),
        "speed_pilot_pct": _share(total["speed_pilot"], engaged),
        "speed_driver_pct": _share(engaged - total["speed_pilot"], engaged),
        "interruptions": sum(counts.get(k, 0) for k in INTERRUPTION_TYPES),
        "counts": counts,
        "events": events,
        "series": series,
    }
