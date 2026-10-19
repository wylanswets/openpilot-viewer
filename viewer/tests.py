import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from viewer import summary, tracks, views
from viewer.templatetags import custom_filters


class DriveListCameraTests(TestCase):
    """The drive list only shows camera rows that have footage."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.stitched = root / "stitched"
        self.raw = root / "raw"
        self.stitched.mkdir()
        self.raw.mkdir()
        for target, name in (
            (views, "STITCHED_DIR"),
            (views, "RAW_DIR"),
            (custom_filters, "STITCHED_DIR"),
        ):
            patcher = mock.patch.object(
                target, name, self.stitched if name == "STITCHED_DIR" else self.raw
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(views, "PRESERVED_FILE", root / "preserved.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_drive(self, route_id, cameras):
        d = self.stitched / route_id
        d.mkdir()
        for cam in cameras:
            (d / f"{cam}.mp4").write_bytes(b"")
            thumbs = d / "thumbs" / cam
            thumbs.mkdir(parents=True)
            for i in (1, 2, 3):
                (thumbs / f"thumb_{i}.jpg").write_bytes(b"")

    def get_rows(self):
        response = self.client.get(reverse("drive_list"))
        self.assertEqual(response.status_code, 200)
        return response, list(response.context["page_obj"])

    def test_missing_driver_camera_is_hidden(self):
        self.make_drive("00000001--aaaaaaaaaa", ["fcamera", "ecamera"])
        response, drives = self.get_rows()
        labels = [row["label"] for row in drives[0]["thumbnails"]]
        self.assertEqual(labels, ["Front Camera", "Wide Camera"])
        self.assertNotContains(response, "Driver Camera")
        self.assertNotContains(response, "N/A")

    def test_cameras_are_decided_per_drive(self):
        self.make_drive("00000001--aaaaaaaaaa", ["fcamera"])
        self.make_drive("00000002--bbbbbbbbbb", ["fcamera", "ecamera", "dcamera"])
        _, drives = self.get_rows()
        by_id = {d["route_id"]: d for d in drives}
        self.assertEqual(len(by_id["00000001--aaaaaaaaaa"]["thumbnails"]), 1)
        self.assertEqual(len(by_id["00000002--bbbbbbbbbb"]["thumbnails"]), 3)

    def test_logs_only_drive_has_no_thumbnail_rows(self):
        (self.raw / "00000003--cccccccccc--0").mkdir()
        _, drives = self.get_rows()
        self.assertEqual(drives[0]["thumbnails"], [])

    def test_map_tiles_are_requested_with_a_referrer(self):
        # OpenStreetMap blocks tile requests that carry no Referer, and Django's
        # default Referrer-Policy removes it for cross-origin requests.
        self.make_drive("00000006--ffffffffff", ["fcamera"])
        response = self.client.get(reverse("drive_detail", args=["00000006--ffffffffff"]))
        self.assertContains(response, 'referrerPolicy: "strict-origin-when-cross-origin"')

    def test_route_map_comes_before_the_cameras(self):
        self.make_drive("00000007--1111111111", ["fcamera", "ecamera"])
        response = self.client.get(reverse("drive_detail", args=["00000007--1111111111"]))
        html = response.content.decode()
        map_at = html.index('id="route-map-section"')
        self.assertLess(map_at, html.index("Front Camera"))
        self.assertLess(map_at, html.index("Wide Camera"))

    def test_route_map_has_an_enlarge_button_and_starts_at_normal_height(self):
        self.make_drive("00000008--2222222222", ["fcamera"])
        response = self.client.get(reverse("drive_detail", args=["00000008--2222222222"]))
        self.assertContains(response, 'id="route-map-resize"')
        self.assertContains(response, 'id="route-map" class="w-full h-96')
        self.assertContains(response, 'mapEl.style.height = enlarged ? "85vh" : ""')

    def test_summary_panel_is_below_all_the_cameras(self):
        self.make_drive("00000010--3333333333", ["fcamera", "ecamera", "dcamera"])
        response = self.client.get(reverse("drive_detail", args=["00000010--3333333333"]))
        html = response.content.decode()
        panel = html.index('id="summary-section"')
        for label in ("Front Camera", "Wide Camera", "Driver Camera"):
            self.assertLess(html.index(f">{label}<"), panel)
        self.assertContains(response, reverse("drive_summary", args=["00000010--3333333333"]))

    def test_summary_graph_has_zoom_controls_and_pinned_libraries(self):
        self.make_drive("00000011--4444444444", ["fcamera"])
        response = self.client.get(reverse("drive_detail", args=["00000011--4444444444"]))
        for control in ("summary-zoom-in", "summary-zoom-out", "summary-pan-left",
                        "summary-pan-right", "summary-zoom-reset", "summary-range"):
            self.assertContains(response, f'id="{control}"')
        # every script loaded from a CDN carries an integrity hash
        html = response.content.decode()
        libraries = ("leaflet", "chart.js", "hammerjs", "chartjs-plugin-zoom")
        tags = [tag.split(">")[0] for tag in html.split("<script")[1:]]
        for library in libraries:
            (tag,) = [tag for tag in tags if f"unpkg.com/{library}" in tag]
            self.assertIn("integrity=", tag)

    def test_no_stitched_path_line(self):
        self.make_drive("00000004--dddddddddd", ["fcamera"])
        response, _ = self.get_rows()
        self.assertNotContains(response, "Stitched path")


def write_qlog(path, fixes):
    """Write a qlog.zst holding one gpsLocationExternal per (flags, lat, lon, speed)."""
    import zstandard as zstd

    schema = tracks._schema()
    chunks = []
    for i, (flags, lat, lon, speed) in enumerate(fixes):
        event = schema.Event.new_message(logMonoTime=i + 1)
        gps = event.init("gpsLocationExternal")
        gps.flags, gps.latitude, gps.longitude, gps.speed = flags, lat, lon, speed
        chunks.append(event.to_bytes())
        # an unrelated message type that must be ignored
        other = schema.Event.new_message(logMonoTime=i + 1)
        other.init("carState").vEgo = 5.0
        chunks.append(other.to_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstd.ZstdCompressor().compress(b"".join(chunks)))


class RouteTrackTests(TestCase):
    ROUTE = "00000005--eeeeeeeeee"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.raw = root / "raw"
        self.cache = root / "metadata" / "tracks"
        self.raw.mkdir()
        patcher = mock.patch.multiple(views, RAW_DIR=self.raw, METADATA_DIR=root / "metadata")
        patcher.start()
        self.addCleanup(patcher.stop)

    def segment(self, number, fixes):
        write_qlog(self.raw / f"{self.ROUTE}--{number}" / "qlog.zst", fixes)

    def track(self):
        return tracks.route_track(self.ROUTE, self.raw, self.cache)

    def test_points_from_all_segments_are_joined_in_order(self):
        self.segment(1, [(1, 35.002, -82.002, 11.0)])
        self.segment(0, [(1, 35.000, -82.000, 10.0), (1, 35.001, -82.001, 10.5)])
        result = self.track()
        self.assertEqual(
            result["lines"],
            [[[35.0, -82.0], [35.001, -82.001], [35.002, -82.002]]],
        )
        self.assertEqual(result["points"], 3)

    def test_fixes_without_a_valid_gps_flag_or_at_0_0_are_dropped(self):
        self.segment(0, [(0, 35.0, -82.0, 1.0), (1, 0.0, 0.0, 1.0), (1, 35.5, -82.5, 1.0)])
        self.assertEqual(self.track()["lines"], [[[35.5, -82.5]]])

    def test_missing_segment_breaks_the_line(self):
        self.segment(0, [(1, 35.0, -82.0, 1.0)])
        self.segment(2, [(1, 36.0, -83.0, 1.0)])
        self.assertEqual(self.track()["lines"], [[[35.0, -82.0]], [[36.0, -83.0]]])

    def test_route_without_any_fix_has_no_lines(self):
        self.segment(0, [(0, 35.0, -82.0, 1.0)])
        self.assertEqual(self.track(), {"lines": [], "points": 0})

    def test_segments_are_cached_and_only_changed_ones_reparsed(self):
        self.segment(0, [(1, 35.0, -82.0, 1.0)])
        self.track()
        self.assertTrue((self.cache / f"{self.ROUTE}.json").exists())
        with mock.patch.object(tracks, "parse_segment", side_effect=AssertionError("reparsed")):
            self.assertEqual(self.track()["points"], 1)
        # a new segment appears: only it is parsed
        self.segment(1, [(1, 35.1, -82.1, 1.0)])
        real = tracks.parse_segment
        with mock.patch.object(tracks, "parse_segment", side_effect=real) as parse:
            self.assertEqual(self.track()["points"], 2)
        self.assertEqual(parse.call_count, 1)

    def test_corrupt_qlog_is_skipped(self):
        self.segment(0, [(1, 35.0, -82.0, 1.0)])
        bad = self.raw / f"{self.ROUTE}--1" / "qlog.zst"
        bad.parent.mkdir()
        bad.write_bytes(b"not zstd")
        self.assertEqual(self.track()["points"], 1)

    def test_unwritable_cache_does_not_break_the_track(self):
        self.segment(0, [(1, 35.0, -82.0, 1.0)])
        blocker = self.cache.parent
        blocker.write_text("a file where the metadata dir should be")
        self.assertEqual(self.track()["points"], 1)

    def test_track_endpoint_returns_json(self):
        self.segment(0, [(1, 35.0, -82.0, 1.0)])
        response = self.client.get(reverse("drive_track", args=[self.ROUTE]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"lines": [[[35.0, -82.0]]], "points": 1})

    def test_track_endpoint_rejects_odd_route_ids(self):
        response = self.client.get(reverse("drive_track", args=["bad.id"]))
        self.assertEqual(response.status_code, 404)


def write_drive_qlog(path, samples, onroad=(), stale_header=False):
    """Write a qlog.zst from 10 Hz drive samples and onroadEvents.

    samples: (t_seconds, v_m_s, wheel, gas, brake, lat, long) per carState/carControl.
    onroad:  (t_seconds, [event names]) per onroadEvents message.
    """
    import zstandard as zstd

    schema = tracks._schema()
    chunks = []

    def message(t):
        return schema.Event.new_message(logMonoTime=int(t * 1e9) + 1)

    for t, v, wheel, gas, brake, lat, long_ in samples:
        control = message(t)
        cc = control.init("carControl")
        cc.latActive, cc.longActive = lat, long_
        chunks.append(control.to_bytes())
        state = message(t)
        cs = state.init("carState")
        cs.vEgo, cs.steeringPressed, cs.gasPressed, cs.brakePressed = v, wheel, gas, brake
        chunks.append(state.to_bytes())
    if stale_header:
        # qlog headers (initData, ...) keep the timestamp from when the route began
        header = schema.Event.new_message(logMonoTime=1)
        header.init("initData")
        chunks.insert(0, header.to_bytes())
    for t, names in onroad:
        event = message(t)
        listing = event.init("onroadEvents", len(names))
        for i, name in enumerate(names):
            listing[i].name = name
        chunks.append(event.to_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstd.ZstdCompressor().compress(b"".join(chunks)))


def run(start, seconds, v=20.0, wheel=False, gas=False, brake=False, lat=False, long_=False):
    """10 Hz samples for `seconds` seconds from time `start`."""
    return [
        (start + i / 10, v, wheel, gas, brake, lat, long_) for i in range(int(seconds * 10))
    ]


class DriveSummaryTests(TestCase):
    ROUTE = "00000009--9999999999"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.raw = root / "raw"
        self.cache = root / "metadata" / "summaries"
        self.raw.mkdir()
        patcher = mock.patch.multiple(views, RAW_DIR=self.raw, METADATA_DIR=root / "metadata")
        patcher.start()
        self.addCleanup(patcher.stop)

    def segment(self, number, samples, onroad=(), stale_header=False):
        write_drive_qlog(
            self.raw / f"{self.ROUTE}--{number}" / "qlog.zst", samples, onroad, stale_header
        )

    def summary(self):
        return summary.route_summary(self.ROUTE, self.raw, self.cache)

    def test_pilot_and_driver_share_of_engaged_time(self):
        # 5 s pilot steering + speed, 2 s driver pushing the wheel, 3 s nothing engaged
        self.segment(0, (
            run(0, 5, lat=True, long_=True)
            + run(5, 2, lat=True, long_=True, wheel=True)
            + run(7, 3)
        ))
        s = self.summary()
        self.assertAlmostEqual(s["duration_s"], 10, delta=1)
        self.assertAlmostEqual(s["engaged_s"], 7, delta=1)
        self.assertAlmostEqual(s["steer_pilot_pct"], 100 * 5 / 7, delta=3)
        self.assertAlmostEqual(s["steer_driver_pct"], 100 * 2 / 7, delta=3)
        # the wheel push does not change who controls the speed
        self.assertEqual(s["speed_pilot_pct"], 100.0)
        self.assertEqual(s["speed_driver_pct"], 0.0)

    def test_pedal_press_makes_speed_the_drivers(self):
        self.segment(0, run(0, 4, lat=True, long_=True) + run(4, 3, lat=True, long_=True, gas=True))
        s = self.summary()
        self.assertAlmostEqual(s["speed_driver_pct"], 100 * 3 / 7, delta=3)
        self.assertEqual(s["steer_pilot_pct"], 100.0)
        self.assertEqual(s["counts"]["pedal_override"], 1)

    def test_steering_only_time_counts_speed_as_the_drivers(self):
        # lateral control on, longitudinal off: the driver is controlling speed
        self.segment(0, run(0, 5, lat=True))
        s = self.summary()
        self.assertEqual(s["steer_pilot_pct"], 100.0)
        self.assertEqual(s["speed_pilot_pct"], 0.0)
        self.assertEqual(s["speed_driver_pct"], 100.0)

    def test_nothing_engaged_has_no_percentages(self):
        self.segment(0, run(0, 5))
        s = self.summary()
        self.assertEqual(s["engaged_s"], 0)
        self.assertIsNone(s["steer_pilot_pct"])
        self.assertIsNone(s["speed_pilot_pct"])
        self.assertEqual(s["interruptions"], 0)

    def test_speed_and_distance(self):
        self.segment(0, run(0, 10, v=20.0) + run(10, 1, v=25.0))
        s = self.summary()
        self.assertAlmostEqual(s["distance_mi"], (200 + 25) / 1609.344, delta=0.006)
        self.assertEqual(s["max_mph"], round(25 * 2.2369363, 1))
        self.assertAlmostEqual(s["avg_mph"], 20.5 * 2.2369363, delta=2)

    def test_interruptions_are_counted_once_per_episode(self):
        self.segment(0, (
            run(0, 3, lat=True, long_=True)
            + run(3, 2, lat=True, long_=True, wheel=True)    # wheel override
            + run(5, 3, lat=True, long_=True)
            + run(8, 2, lat=True, long_=True, brake=True)    # pedal override
            + run(10, 2, lat=True)                           # speed control turned off
            + run(12, 2)                                     # steering turned off
        ))
        counts = self.summary()["counts"]
        self.assertEqual(counts["wheel_override"], 1)
        self.assertEqual(counts["pedal_override"], 1)
        self.assertEqual(counts["speed_off"], 1)
        self.assertEqual(counts["steer_off"], 1)
        self.assertEqual(self.summary()["interruptions"], 4)

    def test_flickering_wheel_or_pedal_presses_count_as_one_episode(self):
        self.segment(0, (
            run(0, 2, lat=True, long_=True)
            + run(2, 0.5, lat=True, long_=True, wheel=True, gas=True)
            + run(2.5, 0.5, lat=True, long_=True)
            + run(3, 0.5, lat=True, long_=True, wheel=True, gas=True)
            + run(3.5, 0.5, lat=True, long_=True)
            + run(4, 0.5, lat=True, long_=True, wheel=True, gas=True)
            + run(4.5, 10, lat=True, long_=True)
            + run(14.5, 0.5, lat=True, long_=True, wheel=True)   # well after: a new episode
        ))
        counts = self.summary()["counts"]
        self.assertEqual(counts["wheel_override"], 2)
        self.assertEqual(counts["pedal_override"], 1)

    def test_old_header_timestamps_do_not_shift_the_time_axis(self):
        # carState samples start at mono 1000 s; a header event is stamped long before
        self.segment(0, run(1000, 58, lat=True), stale_header=True)
        self.segment(1, run(2000, 5, lat=True) + run(2005, 2, lat=True, wheel=True), stale_header=True)
        s = self.summary()
        self.assertEqual(s["series"]["t"][0], 0)
        self.assertNotIn(None, s["series"]["mph"])
        self.assertAlmostEqual(s["events"][0][0], 65, delta=1)  # 5 s into segment 1

    def test_a_short_gap_between_segments_does_not_break_the_line(self):
        self.segment(0, run(1, 58, lat=True))      # a segment starts ~1 s late
        self.segment(1, run(1, 58, lat=True))
        self.assertNotIn(None, self.summary()["series"]["mph"])

    def test_onroad_events_are_reported_when_they_start(self):
        self.segment(0, run(0, 10, lat=True), onroad=[
            (0.5, []),
            (1.0, ["steerSaturated"]),
            (1.5, ["steerSaturated"]),            # still the same event
            (2.0, []),
            (3.0, ["steerSaturated", "laneChange"]),
            (4.0, ["driverDistracted1"]),
            (4.5, ["driverDistracted2"]),         # escalation: same incident
            (6.0, ["wrongGear"]),                 # not reported
        ])
        counts = self.summary()["counts"]
        self.assertEqual(counts["steer_saturated"], 2)
        self.assertEqual(counts["lane_change"], 1)
        self.assertEqual(counts["driver_distracted"], 1)
        self.assertNotIn("wrongGear", counts)

    def test_event_already_active_at_segment_start_is_not_new(self):
        self.segment(0, run(0, 5, lat=True), onroad=[(0.1, ["steerSaturated"]), (0.6, ["steerSaturated"])])
        self.assertNotIn("steer_saturated", self.summary()["counts"])

    def test_segments_are_merged_with_offsets_and_gaps(self):
        self.segment(0, run(0, 5, lat=True, long_=True))
        self.segment(2, run(0, 2, lat=True, long_=True) + run(2, 3, lat=True, long_=True, wheel=True))
        s = self.summary()
        self.assertEqual(s["segments"], 2)
        self.assertAlmostEqual(s["engaged_s"], 10, delta=1)
        self.assertEqual(s["events"][0][1], "wheel_override")
        self.assertAlmostEqual(s["events"][0][0], 122, delta=1)  # segment 2 starts at 120 s
        series = s["series"]
        self.assertEqual(len(series["t"]), len(series["mph"]))
        self.assertIn(None, series["mph"])  # the missing segment 1 breaks the line
        self.assertEqual(series["t"], sorted(series["t"]))

    def test_summary_is_cached_per_segment(self):
        self.segment(0, run(0, 5, lat=True))
        self.summary()
        self.assertTrue((self.cache / f"{self.ROUTE}.json").exists())
        with mock.patch.object(summary, "parse_segment", side_effect=AssertionError("reparsed")):
            self.assertEqual(self.summary()["segments"], 1)

    def test_summary_endpoint(self):
        self.segment(0, run(0, 5, lat=True, long_=True))
        response = self.client.get(reverse("drive_summary", args=[self.ROUTE]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["steer_pilot_pct"], 100.0)
        self.assertEqual(set(data["series"]), {"t", "mph", "steer", "speed"})

    def test_summary_endpoint_rejects_odd_route_ids(self):
        self.assertEqual(self.client.get(reverse("drive_summary", args=["bad.id"])).status_code, 404)
