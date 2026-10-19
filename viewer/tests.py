import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from viewer import views
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
