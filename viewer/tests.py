import os
import tempfile
import threading
import time
from io import BytesIO
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from viewer import deletion, labels, prepare, qlogs, summary, thumbnails, tracks, views
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
        self.metadata = root / "metadata"
        patcher = mock.patch.object(views, "METADATA_DIR", self.metadata)
        patcher.start()
        self.addCleanup(patcher.stop)
        # never start the real thumbnail thread (it would fetch map tiles) in tests
        patcher = mock.patch.object(thumbnails, "start_background")
        self.start_background = patcher.start()
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

    def test_list_shows_an_existing_thumbnail(self):
        self.make_drive("00000012--5555555555", ["fcamera"])
        image = thumbnails.image_path(self.metadata / "thumbs", "00000012--5555555555")
        image.parent.mkdir(parents=True)
        image.write_bytes(b"png")
        thumbnails._write_state(self.metadata / "thumbs", "00000012--5555555555", {"status": "ok", "segments": 0})
        response = self.client.get(reverse("drive_list"))
        self.assertContains(response, "/drive/00000012--5555555555/thumbnail.png?v=")
        self.assertContains(response, 'alt="Map of the route"')
        self.assertNotContains(response, "thumbnail-pending\" data-src")

    def test_list_asks_for_missing_thumbnails_and_leaves_a_placeholder(self):
        self.make_drive("00000013--6666666666", ["fcamera"])
        response = self.client.get(reverse("drive_list"))
        self.assertContains(response, 'class="thumbnail-pending" data-src="/drive/00000013--6666666666/thumbnail.png"')
        self.start_background.assert_called_once()
        self.assertEqual(self.start_background.call_args.args[0], ["00000013--6666666666"])

    def test_route_known_to_have_no_gps_gets_no_placeholder(self):
        self.make_drive("00000014--7777777777", ["fcamera"])
        thumbnails._write_state(self.metadata / "thumbs", "00000014--7777777777", {"status": "no_gps", "segments": 0})
        with mock.patch.object(summary, "cached_duration", return_value=60.0):
            response = self.client.get(reverse("drive_list"))
        self.assertNotContains(response, 'class="thumbnail-pending" data-src')
        self.assertEqual(self.start_background.call_args.args[0], [])  # nothing left to do for it

    def test_a_drive_without_a_known_duration_is_queued_for_the_background_thread(self):
        thumbnails._write_state(self.metadata / "thumbs", "00000014--7777777777", {"status": "no_gps", "segments": 0})
        self.make_drive("00000014--7777777777", ["fcamera"])
        response = self.client.get(reverse("drive_list"))  # no cached duration
        self.assertNotContains(response, 'class="thumbnail-pending" data-src')
        self.assertEqual(self.start_background.call_args.args[0], ["00000014--7777777777"])
        self.assertIs(self.start_background.call_args.kwargs["make"], prepare.prepare_route)

    def test_list_label_is_in_local_time_with_the_duration(self):
        self.make_drive("00000015--8888888888", ["fcamera"])
        (self.stitched / "00000015--8888888888" / "start_time.txt").write_text("2026-10-19 11:34:55")
        with override_settings(TIME_ZONE="America/New_York"), \
                mock.patch.object(summary, "cached_duration", return_value=626.4):
            response = self.client.get(reverse("drive_list"))
        self.assertContains(response, "Oct 19, 2026 - 7:34 AM (10 min 26 sec)")
        self.assertNotContains(response, "2026-10-19 11:34")

    def test_list_label_without_a_duration_has_no_brackets(self):
        self.make_drive("00000015--8888888888", ["fcamera"])
        (self.stitched / "00000015--8888888888" / "start_time.txt").write_text("2026-10-19 11:34:55")
        with override_settings(TIME_ZONE="America/New_York"):
            response = self.client.get(reverse("drive_list"))
        self.assertContains(response, "Oct 19, 2026 - 7:34 AM</span>")

    def test_a_drive_with_no_known_start_time_says_so(self):
        self.make_drive("00000015--8888888888", ["fcamera"])
        self.assertContains(self.client.get(reverse("drive_list")), "No timestamp")

    def test_the_drive_page_uses_the_same_label(self):
        self.make_drive("00000015--8888888888", ["fcamera"])
        (self.stitched / "00000015--8888888888" / "start_time.txt").write_text("2026-10-19 11:34:55")
        with override_settings(TIME_ZONE="America/New_York"), \
                mock.patch.object(summary, "cached_duration", return_value=3302):
            response = self.client.get(reverse("drive_detail", args=["00000015--8888888888"]))
        self.assertContains(response, "Oct 19, 2026 - 7:34 AM (55 min 2 sec)")

    def test_building_the_list_never_parses_a_log(self):
        self.make_drive("00000015--8888888888", ["fcamera"])
        (self.raw / "00000015--8888888888--0").mkdir()
        (self.raw / "00000015--8888888888--0" / "qlog.zst").write_bytes(b"not a real log")
        with mock.patch.object(qlogs, "read_events", side_effect=AssertionError("parsed a log")):
            self.assertEqual(self.client.get(reverse("drive_list")).status_code, 200)

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

    def test_cached_duration_is_none_until_the_route_has_been_summarised(self):
        self.segment(0, run(0, 5, lat=True))
        self.assertIsNone(summary.cached_duration(self.ROUTE, self.raw, self.cache))
        total = self.summary()["duration_s"]
        self.assertAlmostEqual(summary.cached_duration(self.ROUTE, self.raw, self.cache), total, delta=0.5)

    def test_cached_duration_never_parses_and_is_the_same_number_as_the_summary(self):
        self.segment(0, run(0, 5, lat=True))
        self.segment(1, run(0, 7, lat=True))
        expected = self.summary()["duration_s"]
        with mock.patch.object(qlogs, "read_events", side_effect=AssertionError("parsed a log")):
            self.assertAlmostEqual(summary.cached_duration(self.ROUTE, self.raw, self.cache), expected, delta=0.6)

    def test_cached_duration_is_none_when_a_segment_is_not_cached_yet(self):
        self.segment(0, run(0, 5, lat=True))
        self.summary()
        self.segment(1, run(0, 5, lat=True))  # a new segment arrived
        self.assertIsNone(summary.cached_duration(self.ROUTE, self.raw, self.cache))

    def test_cached_duration_is_none_for_a_route_without_logs(self):
        self.assertIsNone(summary.cached_duration("0000000f--ffffffffff", self.raw, self.cache))

    def test_summary_endpoint(self):
        self.segment(0, run(0, 5, lat=True, long_=True))
        response = self.client.get(reverse("drive_summary", args=[self.ROUTE]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["steer_pilot_pct"], 100.0)
        self.assertEqual(set(data["series"]), {"t", "mph", "steer", "speed"})

    def test_summary_endpoint_rejects_odd_route_ids(self):
        self.assertEqual(self.client.get(reverse("drive_summary", args=["bad.id"])).status_code, 404)


def tile_png(color=(200, 220, 200)):
    from PIL import Image

    out = BytesIO()
    Image.new("RGB", (256, 256), color).save(out, format="PNG")
    return out.getvalue()


class ThumbnailGeometryTests(TestCase):
    def test_world_pixels(self):
        self.assertEqual(thumbnails.world_xy(0, 0, 0), (128, 128))
        self.assertAlmostEqual(thumbnails.world_xy(0, -180, 3)[0], 0)
        self.assertAlmostEqual(thumbnails.world_xy(0, 180, 3)[0], 256 * 8)
        self.assertAlmostEqual(thumbnails.world_xy(85.0511, 0, 2)[1], 0, delta=0.5)

    def fits(self, points, zoom):
        x0, y1 = thumbnails.world_xy(min(p[0] for p in points), min(p[1] for p in points), zoom)
        x1, y0 = thumbnails.world_xy(max(p[0] for p in points), max(p[1] for p in points), zoom)
        return (
            x1 - x0 <= thumbnails.WIDTH - 2 * thumbnails.PADDING
            and y1 - y0 <= thumbnails.HEIGHT - 2 * thumbnails.PADDING
        )

    def test_zoom_is_the_closest_that_fits_the_route(self):
        for points in (
            [[35.0, -82.0], [35.0001, -82.0001]],      # a few metres
            [[35.0, -82.0], [35.02, -82.03]],           # a few km
            [[34.9, -82.3], [35.1, -82.0]],             # about 30 km
        ):
            zoom, left, top = thumbnails.layout(points)
            self.assertTrue(self.fits(points, zoom), points)
            if zoom < thumbnails.MAX_ZOOM:
                self.assertFalse(self.fits(points, zoom + 1), points)
        self.assertEqual(thumbnails.layout([[35.0, -82.0]])[0], thumbnails.MAX_ZOOM)

    def test_route_is_centred_in_the_image(self):
        points = [[35.0, -82.0], [35.02, -82.03]]
        zoom, left, top = thumbnails.layout(points)
        xs = [thumbnails.to_pixel(lat, lon, zoom, left, top)[0] for lat, lon in points]
        ys = [thumbnails.to_pixel(lat, lon, zoom, left, top)[1] for lat, lon in points]
        self.assertAlmostEqual((min(xs) + max(xs)) / 2, thumbnails.WIDTH / 2, delta=1)
        self.assertAlmostEqual((min(ys) + max(ys)) / 2, thumbnails.HEIGHT / 2, delta=1)


class ThumbnailRenderTests(TestCase):
    LINES = [[[35.0, -82.0], [35.01, -82.012]]]

    def render(self, source=None):
        self.calls = []

        def default(z, x, y):
            self.calls.append((z, x, y))
            return tile_png()

        return thumbnails.render(self.LINES, source or default)

    def test_route_is_drawn_over_the_tiles(self):
        image = self.render()
        self.assertEqual(image.size, (thumbnails.WIDTH, thumbnails.HEIGHT))
        zoom, left, top = thumbnails.layout([p for line in self.LINES for p in line])
        start = [round(v) for v in thumbnails.to_pixel(*self.LINES[0][0], zoom, left, top)]
        end = [round(v) for v in thumbnails.to_pixel(*self.LINES[0][1], zoom, left, top)]
        mid = ((start[0] + end[0]) // 2, (start[1] + end[1]) // 2)
        r, g, b = image.getpixel(tuple(start))
        self.assertTrue(g > 150 and r < 100, "start dot should be green")
        r, g, b = image.getpixel(tuple(end))
        self.assertTrue(r > 200 and g < 120, "end dot should be red")
        r, g, b = image.getpixel(mid)
        self.assertTrue(b > 200 and r < 100, "the line should be blue")
        self.assertEqual(image.getpixel((5, 5)), (200, 220, 200))  # untouched map background

    def test_map_credit_is_on_the_image(self):
        image = self.render()
        corner = image.getpixel((thumbnails.WIDTH - 3, thumbnails.HEIGHT - 3))
        self.assertNotEqual(corner, (200, 220, 200))
        # the credit box is lighter than the tile and has some dark text pixels in it
        region = image.crop((thumbnails.WIDTH - 150, thumbnails.HEIGHT - 16, thumbnails.WIDTH, thumbnails.HEIGHT))
        self.assertTrue(any(sum(px) < 450 for px in region.getdata()))

    def test_tiles_asked_for_cover_the_image_with_valid_coordinates(self):
        self.render()
        zoom = self.calls[0][0]
        self.assertTrue(self.calls)
        for z, x, y in self.calls:
            self.assertEqual(z, zoom)
            self.assertTrue(0 <= x < 2 ** z and 0 <= y < 2 ** z)
        self.assertEqual(len(self.calls), len(set(self.calls)))

    def test_a_missing_tile_means_no_image(self):
        self.assertIsNone(self.render(lambda z, x, y: None))

    def test_empty_route_has_no_image(self):
        self.assertIsNone(thumbnails.render([], lambda z, x, y: tile_png()))


class TileCacheTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        self.fetched = []

    def fetch(self, z, x, y):
        self.fetched.append((z, x, y))
        return tile_png()

    def test_a_tile_is_fetched_once_and_then_served_from_disk(self):
        first = thumbnails.get_tile(5, 3, 4, self.cache, self.fetch)
        second = thumbnails.get_tile(5, 3, 4, self.cache, self.fetch)
        self.assertEqual(first, second)
        self.assertEqual(self.fetched, [(5, 3, 4)])

    def test_a_tile_older_than_a_week_is_fetched_again(self):
        thumbnails.get_tile(5, 3, 4, self.cache, self.fetch)
        thumbnails.get_tile(5, 3, 4, self.cache, self.fetch, now=time.time() + thumbnails.TILE_MAX_AGE_S + 60)
        self.assertEqual(len(self.fetched), 2)

    def test_a_stale_copy_is_used_when_the_refetch_fails(self):
        stale = thumbnails.get_tile(5, 3, 4, self.cache, self.fetch)

        def failing(z, x, y):
            raise OSError("no internet")

        later = time.time() + thumbnails.TILE_MAX_AGE_S + 60
        self.assertEqual(thumbnails.get_tile(5, 3, 4, self.cache, failing, now=later), stale)

    def test_failure_with_nothing_cached_gives_none(self):
        def failing(z, x, y):
            raise OSError("no internet")

        self.assertIsNone(thumbnails.get_tile(5, 3, 4, self.cache, failing))

    def test_replies_that_are_not_tiles_are_not_cached(self):
        for body in (b"<html>rate limited</html>", tile_png() + b"0" * thumbnails.MAX_TILE_BYTES):
            self.assertIsNone(thumbnails.get_tile(5, 3, 4, self.cache, lambda z, x, y: body))
            self.assertFalse((self.cache / "5" / "3" / "4.png").exists())


class MakeThumbnailTests(TestCase):
    ROUTE = "0000000a--aaaaaaaaaa"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.raw = root / "raw"
        self.metadata = root / "metadata"
        self.raw.mkdir()
        self.stitched = root / "stitched"
        self.stitched.mkdir()
        self.later = time.time() + 3600  # pretend the logs are an hour old
        self.fetched = []

    def fetch(self, z, x, y):
        self.fetched.append((z, x, y))
        return tile_png()

    def segment(self, number, fixes, route=None):
        write_qlog(self.raw / f"{route or self.ROUTE}--{number}" / "qlog.zst", fixes)

    def make(self, route=None, **kwargs):
        kwargs.setdefault("now", self.later)
        kwargs.setdefault("fetch", self.fetch)
        kwargs.setdefault("stitched_dir", self.stitched)
        return thumbnails.make_thumbnail(route or self.ROUTE, self.raw, self.metadata, **kwargs)

    def test_makes_a_png_and_then_leaves_it_alone(self):
        from PIL import Image

        self.segment(0, [(1, 35.0, -82.0, 5.0), (1, 35.005, -82.004, 5.0)])
        self.assertEqual(self.make(), "made")
        path = thumbnails.image_path(self.metadata / "thumbs", self.ROUTE)
        with Image.open(path) as image:
            self.assertEqual(image.size, (640, 260))
            self.assertEqual(image.format, "PNG")
        fetched = len(self.fetched)
        self.assertEqual(self.make(), "fresh")
        self.assertEqual(len(self.fetched), fetched)

    def test_is_redone_when_the_route_grows(self):
        self.segment(0, [(1, 35.0, -82.0, 5.0), (1, 35.001, -82.001, 5.0)])
        self.assertEqual(self.make(), "made")
        self.segment(1, [(1, 35.03, -82.05, 5.0), (1, 35.04, -82.06, 5.0)])
        self.assertTrue(thumbnails.needs_work(self.metadata / "thumbs", self.ROUTE, 2))
        self.assertEqual(self.make(), "made")
        self.assertFalse(thumbnails.needs_work(self.metadata / "thumbs", self.ROUTE, 2))

    def test_a_route_still_being_recorded_is_skipped(self):
        self.segment(0, [(1, 35.0, -82.0, 5.0)])
        self.assertEqual(self.make(now=time.time()), "recording")
        self.assertEqual(self.fetched, [])
        self.assertTrue(thumbnails.needs_work(self.metadata / "thumbs", self.ROUTE, 1))

    def test_a_route_the_sync_has_finished_is_made_even_if_its_logs_keep_being_rewritten(self):
        # the sync rewrites some routes' logs on every run, so their times are always recent
        self.segment(0, [(1, 35.0, -82.0, 5.0), (1, 35.001, -82.001, 5.0)])
        (self.stitched / self.ROUTE).mkdir()
        (self.stitched / self.ROUTE / ".done").touch()
        self.assertEqual(self.make(now=time.time()), "made")

    def test_a_stitched_folder_without_done_does_not_count_as_finished(self):
        self.segment(0, [(1, 35.0, -82.0, 5.0)])
        (self.stitched / self.ROUTE).mkdir()  # stitching under way: no .done yet
        self.assertEqual(self.make(now=time.time()), "recording")

    def test_old_logs_are_enough_when_there_is_no_stitched_folder(self):
        self.segment(0, [(1, 35.0, -82.0, 5.0), (1, 35.001, -82.001, 5.0)])
        self.assertEqual(self.make(), "made")

    def test_without_a_stitched_folder_given_the_log_times_decide(self):
        self.segment(0, [(1, 35.0, -82.0, 5.0)])
        self.assertEqual(self.make(now=time.time(), stitched_dir=None), "recording")

    def test_route_without_gps_is_remembered(self):
        self.segment(0, [(0, 35.0, -82.0, 5.0)])
        self.assertEqual(self.make(), "no_gps")
        self.assertEqual(self.make(), "fresh")
        self.assertFalse(thumbnails.needs_work(self.metadata / "thumbs", self.ROUTE, 1))
        self.assertFalse(thumbnails.image_path(self.metadata / "thumbs", self.ROUTE).exists())
        self.assertEqual(self.fetched, [])

    def test_a_failed_tile_fetch_leaves_nothing_behind_and_retries_later(self):
        self.segment(0, [(1, 35.0, -82.0, 5.0), (1, 35.001, -82.001, 5.0)])

        def failing(z, x, y):
            raise OSError("no internet")

        self.assertEqual(self.make(fetch=failing), "failed")
        self.assertFalse(thumbnails.image_path(self.metadata / "thumbs", self.ROUTE).exists())
        self.assertTrue(thumbnails.needs_work(self.metadata / "thumbs", self.ROUTE, 1))
        self.assertEqual(self.make(), "made")

    def test_force_redoes_an_up_to_date_thumbnail(self):
        self.segment(0, [(1, 35.0, -82.0, 5.0), (1, 35.001, -82.001, 5.0)])
        self.make()
        self.assertEqual(self.make(force=True), "made")


class BackgroundThumbnailTests(TestCase):
    def setUp(self):
        patcher = mock.patch.object(thumbnails, "_backoff_until", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_makes_each_route_in_a_background_thread(self):
        made = []
        thread = thumbnails.start_background(
            ["a", "b"], "raw", "meta", make=lambda r, raw, meta: made.append((r, raw, meta)) or "made"
        )
        thread.join(5)
        self.assertEqual(made, [("a", "raw", "meta"), ("b", "raw", "meta")])

    def test_nothing_to_do_starts_nothing(self):
        self.assertIsNone(thumbnails.start_background([], "raw", "meta", make=lambda *a: "made"))

    def test_only_one_run_at_a_time(self):
        release = threading.Event()
        first = thumbnails.start_background(["a"], "raw", "meta", make=lambda *a: release.wait(5) and "made")
        self.assertIsNotNone(first)
        self.assertIsNone(thumbnails.start_background(["b"], "raw", "meta", make=lambda *a: "made"))
        release.set()
        first.join(5)
        again = thumbnails.start_background(["b"], "raw", "meta", make=lambda *a: "made")
        again.join(5)

    def test_a_failure_stops_the_run_and_waits_before_trying_again(self):
        made = []

        def make(route, raw, meta):
            made.append(route)
            return "failed"

        thumbnails.start_background(["a", "b"], "raw", "meta", make=make).join(5)
        self.assertEqual(made, ["a"])
        self.assertIsNone(thumbnails.start_background(["c"], "raw", "meta", make=make))

    def test_an_exception_counts_as_a_failure_and_frees_the_lock(self):
        def boom(*args):
            raise RuntimeError("boom")

        thumbnails.start_background(["a"], "raw", "meta", make=boom).join(5)
        thumbnails._backoff_until = 0.0
        thread = thumbnails.start_background(["b"], "raw", "meta", make=lambda *a: "made")
        self.assertIsNotNone(thread)
        thread.join(5)


class ThumbnailEndpointTests(TestCase):
    ROUTE = "0000000b--bbbbbbbbbb"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.metadata = Path(tmp.name) / "metadata"
        patcher = mock.patch.object(views, "METADATA_DIR", self.metadata)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_thumbnail_is_a_404(self):
        self.assertEqual(self.client.get(reverse("drive_thumbnail", args=[self.ROUTE])).status_code, 404)

    def test_serves_the_png(self):
        path = thumbnails.image_path(self.metadata / "thumbs", self.ROUTE)
        path.parent.mkdir(parents=True)
        path.write_bytes(tile_png())
        response = self.client.get(reverse("drive_thumbnail", args=[self.ROUTE]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(b"".join(response.streaming_content), tile_png())

    def test_odd_route_ids_are_rejected(self):
        self.assertEqual(self.client.get(reverse("drive_thumbnail", args=["bad.id"])).status_code, 404)


class FormNesting(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    """Records how deeply <form> elements nest (browsers ignore a form inside a form)."""

    def __init__(self):
        super().__init__()
        self.depth = self.max_depth = 0
        self.actions = []

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
            self.actions.append(dict(attrs).get("action"))

    def handle_endtag(self, tag):
        if tag == "form":
            self.depth -= 1


class DeleteDriveTests(TestCase):
    ROUTE = "0000000c--cccccccccc"
    OTHER = "0000000d--dddddddddd"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.raw, self.stitched, self.metadata = root / "raw", root / "stitched", root / "metadata"
        self.raw.mkdir()
        self.stitched.mkdir()
        for target, name, value in (
            (views, "RAW_DIR", self.raw),
            (views, "STITCHED_DIR", self.stitched),
            (views, "METADATA_DIR", self.metadata),
            (views, "PRESERVED_FILE", self.metadata / "preserved_routes.json"),
            (custom_filters, "STITCHED_DIR", self.stitched),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(thumbnails, "start_background")  # no real map tile fetching
        patcher.start()
        self.addCleanup(patcher.stop)
        self.metadata.mkdir()
        for route in (self.ROUTE, self.OTHER):
            (self.raw / f"{route}--0").mkdir()
            (self.stitched / route).mkdir()
        self.marker = self.metadata / "delete_requests" / self.ROUTE

    def star(self, route):
        (self.metadata / "preserved_routes.json").write_text(f'["{route}"]')

    def card(self, route):
        return f'href="/drive/{route}/"'

    def test_marking_a_drive_writes_the_request_the_sync_reads(self):
        response = self.client.post(reverse("delete_drive", args=[self.ROUTE]), follow=True)
        # the contract with sync/delete_lib.sh: an empty file named after the route
        self.assertTrue(self.marker.is_file())
        self.assertEqual(self.marker.read_bytes(), b"")
        self.assertContains(response, "marked for deletion")
        self.assertContains(response, "within about 5 minutes")

    def test_the_viewer_itself_deletes_nothing(self):
        self.client.post(reverse("delete_drive", args=[self.ROUTE]))
        self.assertTrue((self.raw / f"{self.ROUTE}--0").exists())
        self.assertTrue((self.stitched / self.ROUTE).exists())

    def test_a_marked_drive_is_hidden_with_an_undo_and_others_stay(self):
        self.client.post(reverse("delete_drive", args=[self.ROUTE]))
        response = self.client.get(reverse("drive_list"))
        self.assertNotContains(response, self.card(self.ROUTE))
        self.assertContains(response, self.card(self.OTHER))
        self.assertContains(response, "Being deleted (1)")
        self.assertContains(response, reverse("undo_delete", args=[self.ROUTE]))

    def test_undo_brings_the_drive_back(self):
        self.client.post(reverse("delete_drive", args=[self.ROUTE]))
        response = self.client.post(reverse("undo_delete", args=[self.ROUTE]), follow=True)
        self.assertFalse(self.marker.exists())
        self.assertContains(response, "will not be deleted")
        self.assertContains(self.client.get(reverse("drive_list")), self.card(self.ROUTE))

    def test_undo_when_nothing_is_waiting_says_so(self):
        response = self.client.post(reverse("undo_delete", args=[self.ROUTE]), follow=True)
        self.assertContains(response, "was not waiting to be deleted")

    def test_a_starred_drive_cannot_be_marked(self):
        self.star(self.ROUTE)
        response = self.client.post(reverse("delete_drive", args=[self.ROUTE]), follow=True)
        self.assertFalse(self.marker.exists())
        self.assertContains(response, "is starred")

    def test_unknown_or_odd_route_ids_are_rejected(self):
        for route in ("0000000e--eeeeeeeeee", "bad.id", "..x--y", "a--b--c"):
            response = self.client.post(reverse("delete_drive", args=[route]))
            self.assertEqual(response.status_code, 404, route)
        self.assertFalse((self.metadata / "delete_requests").exists())
        self.assertEqual(self.client.post(reverse("undo_delete", args=["bad.id"])).status_code, 404)

    def test_only_post_is_allowed(self):
        self.assertEqual(self.client.get(reverse("delete_drive", args=[self.ROUTE])).status_code, 405)
        self.assertEqual(self.client.get(reverse("undo_delete", args=[self.ROUTE])).status_code, 405)
        self.assertFalse(self.marker.exists())

    def test_the_page_of_a_marked_drive_goes_back_to_the_list(self):
        self.client.post(reverse("delete_drive", args=[self.ROUTE]))
        response = self.client.get(reverse("drive_detail", args=[self.ROUTE]))
        self.assertRedirects(response, reverse("drive_list"), fetch_redirect_response=False)

    def test_marker_for_a_drive_that_is_already_gone_is_not_listed(self):
        gone = "0000000f--ffffffffff"
        deletion.request_delete(gone, self.metadata)
        self.assertNotContains(self.client.get(reverse("drive_list")), "Being deleted")

    def test_drive_page_forms_are_not_nested_and_have_a_confirmation(self):
        response = self.client.get(reverse("drive_detail", args=[self.ROUTE]))
        parser = FormNesting()
        parser.feed(response.content.decode())
        self.assertEqual(parser.max_depth, 1, "a form inside a form is ignored by browsers")
        for name in ("toggle_preserve", "recreate_stitched", "delete_drive"):
            self.assertIn(reverse(name, args=[self.ROUTE]), parser.actions)
        self.assertContains(response, "return confirm(")
        self.assertContains(response, "The copy on the device is not touched")

    def test_a_starred_drive_shows_a_disabled_delete_button(self):
        self.star(self.ROUTE)
        response = self.client.get(reverse("drive_detail", args=[self.ROUTE]))
        self.assertContains(response, "Unstar this drive first")
        self.assertNotContains(response, reverse("delete_drive", args=[self.ROUTE]))

    def test_pending_ignores_other_files(self):
        directory = self.metadata / "delete_requests"
        directory.mkdir()
        (directory / self.ROUTE).touch()
        (directory / "not a route").touch()
        (directory / ".hidden").touch()
        (directory / "sub--dir").mkdir()
        self.assertEqual(deletion.pending(self.metadata), {self.ROUTE})
        self.assertEqual(deletion.pending(self.metadata / "missing"), set())

    def test_route_id_rule_matches_the_sync_script(self):
        # sync/delete_lib.sh: ROUTE_ID_RE='^[A-Za-z0-9]+--[A-Za-z0-9]+$'
        source = (Path(__file__).resolve().parent.parent / "sync" / "delete_lib.sh").read_text()
        self.assertIn("ROUTE_ID_RE='^[A-Za-z0-9]+--[A-Za-z0-9]+$'", source)
        self.assertEqual(deletion.ROUTE_ID_RE.pattern, "^[A-Za-z0-9]+--[A-Za-z0-9]+$")

    def test_messages_are_shown_on_any_page(self):
        response = self.client.post(reverse("undo_delete", args=[self.ROUTE]), follow=True)
        self.assertContains(response, "bg-yellow-100")  # the warning styling from the base template


class DriveLabelTests(TestCase):
    def test_durations(self):
        for seconds, expected in (
            (None, None), (0, None), (0.4, None),
            (1, "1 sec"), (45, "45 sec"), (59.4, "59 sec"),
            (59.6, "1 min 0 sec"), (60, "1 min 0 sec"), (61, "1 min 1 sec"),
            (626, "10 min 26 sec"), (626.4, "10 min 26 sec"), (3599, "59 min 59 sec"),
            (3600, "1 hr 0 min 0 sec"), (3605, "1 hr 0 min 5 sec"), (3725, "1 hr 2 min 5 sec"),
            (3302, "55 min 2 sec"), (90000, "25 hr 0 min 0 sec"),
        ):
            self.assertEqual(labels.format_duration(seconds), expected, seconds)

    def test_no_zero_is_shown_for_a_larger_unit(self):
        for seconds in (1, 45, 59, 61, 626, 3599):
            self.assertNotIn("0 hr", labels.format_duration(seconds))
        for seconds in (1, 45, 59):
            self.assertNotIn("0 min", labels.format_duration(seconds))

    @override_settings(TIME_ZONE="America/New_York")
    def test_start_time_is_shown_in_local_time(self):
        import datetime as dt
        label = labels.drive_label
        self.assertEqual(label(dt.datetime(2026, 10, 19, 11, 34, 55)), "Oct 19, 2026 - 7:34 AM")       # summer: UTC-4
        self.assertEqual(label(dt.datetime(2026, 1, 15, 17, 5, 0)), "Jan 15, 2026 - 12:05 PM")          # winter: UTC-5
        self.assertEqual(label(dt.datetime(2026, 10, 20, 2, 30, 0)), "Oct 19, 2026 - 10:30 PM")        # the day before, locally
        self.assertEqual(label(dt.datetime(2026, 10, 19, 4, 5, 0)), "Oct 19, 2026 - 12:05 AM")         # 12 AM, not 0
        self.assertEqual(label(dt.datetime(2026, 10, 19, 16, 5, 0)), "Oct 19, 2026 - 12:05 PM")        # 12 PM
        self.assertEqual(label(dt.datetime(2026, 10, 19, 21, 5, 0)), "Oct 19, 2026 - 5:05 PM")         # no leading zero, minutes padded
        self.assertEqual(label(dt.datetime(2026, 3, 8, 6, 59, 0)), "Mar 8, 2026 - 1:59 AM")            # day without leading zero, before the clocks change
        self.assertEqual(label(dt.datetime(2026, 3, 8, 7, 1, 0)), "Mar 8, 2026 - 3:01 AM")             # after the spring change

    @override_settings(TIME_ZONE="America/New_York")
    def test_label_with_a_duration(self):
        import datetime as dt
        start = dt.datetime(2026, 10, 19, 21, 5, 0)
        self.assertEqual(labels.drive_label(start, 626.4), "Oct 19, 2026 - 5:05 PM (10 min 26 sec)")
        self.assertEqual(labels.drive_label(start, 3725), "Oct 19, 2026 - 5:05 PM (1 hr 2 min 5 sec)")
        self.assertEqual(labels.drive_label(start, None), "Oct 19, 2026 - 5:05 PM")
        self.assertEqual(labels.drive_label(start, 0), "Oct 19, 2026 - 5:05 PM")

    @override_settings(TIME_ZONE="UTC")
    def test_utc_when_no_time_zone_is_set(self):
        import datetime as dt
        self.assertEqual(labels.drive_label(dt.datetime(2026, 10, 19, 11, 34, 55)), "Oct 19, 2026 - 11:34 AM")

    @override_settings(TIME_ZONE="America/New_York")
    def test_an_aware_time_is_converted_not_relabelled(self):
        import datetime as dt
        aware = dt.datetime(2026, 10, 19, 11, 34, 55, tzinfo=dt.timezone.utc)
        self.assertEqual(labels.drive_label(aware), "Oct 19, 2026 - 7:34 AM")

    @override_settings(TIME_ZONE="America/New_York")
    def test_the_template_tag(self):
        import datetime as dt
        from django.template import Context, Template
        out = Template("{% load custom_filters %}{% drive_label start 626 %}").render(
            Context({"start": dt.datetime(2026, 10, 19, 21, 5, 0)}))
        self.assertEqual(out, "Oct 19, 2026 - 5:05 PM (10 min 26 sec)")

    def test_the_time_zone_setting_falls_back_to_utc(self):
        from openpilot_viewer import settings as project_settings
        self.assertEqual(project_settings._time_zone("America/New_York"), "America/New_York")
        self.assertEqual(project_settings._time_zone("Europe/Paris"), "Europe/Paris")
        self.assertEqual(project_settings._time_zone("Not/AZone"), "UTC")
        self.assertEqual(project_settings._time_zone(""), "UTC")


class PrepareRouteTests(TestCase):
    ROUTE = "00000020--2020202020"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.raw, self.stitched, self.metadata = root / "raw", root / "stitched", root / "metadata"
        self.raw.mkdir()
        self.stitched.mkdir()
        self.later = time.time() + 3600
        write_qlog(self.raw / f"{self.ROUTE}--0" / "qlog.zst", [(1, 35.0, -82.0, 5.0), (1, 35.001, -82.001, 5.0)])

    def finish(self):
        (self.stitched / self.ROUTE).mkdir()
        (self.stitched / self.ROUTE / ".done").touch()

    def prepare(self, fetch=None, **kwargs):
        kwargs.setdefault("now", self.later)
        return prepare.prepare_route(
            self.ROUTE, self.raw, self.metadata, stitched_dir=self.stitched,
            fetch=fetch or (lambda z, x, y: tile_png()), **kwargs)

    def cache_file(self):
        return self.metadata / "summaries" / f"{self.ROUTE}.json"

    def test_a_finished_drive_gets_its_summary_and_its_map_image(self):
        self.finish()
        self.assertEqual(self.prepare(), "made")
        self.assertTrue(self.cache_file().exists())
        self.assertIsNotNone(summary.cached_duration(self.ROUTE, self.raw, self.metadata / "summaries"))
        self.assertTrue(thumbnails.image_path(self.metadata / "thumbs", self.ROUTE).exists())

    def test_the_duration_does_not_wait_for_the_internet(self):
        self.finish()

        def failing(z, x, y):
            raise OSError("no internet")

        self.assertEqual(self.prepare(fetch=failing), "failed")
        self.assertTrue(self.cache_file().exists())

    def test_a_drive_that_is_still_arriving_is_left_alone(self):
        (self.stitched / self.ROUTE).mkdir()  # stitching under way: no .done, logs are fresh
        self.assertEqual(self.prepare(now=time.time()), "recording")
        self.assertFalse(self.cache_file().exists())

    def test_a_summary_that_fails_does_not_stop_the_map_image(self):
        self.finish()
        with mock.patch.object(summary, "route_summary", side_effect=RuntimeError("boom")):
            self.assertEqual(self.prepare(), "made")

    def test_the_background_thread_passes_the_stitched_folder_on(self):
        self.finish()
        seen = []
        thread = thumbnails.start_background(
            [self.ROUTE], self.raw, self.metadata,
            make=lambda r, raw, meta, stitched_dir=None: seen.append(stitched_dir) or "made",
            stitched_dir=self.stitched)
        thread.join(5)
        self.assertEqual(seen, [self.stitched])
