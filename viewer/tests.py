import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from viewer import tracks, views
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
