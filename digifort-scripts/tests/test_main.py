import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

import main


def camera(folder, **changes):
    config = dict(url="https://camera.test/image.jpg?password=secret", folder=folder.name,
                  start=0, path=folder, interval_seconds=0.03)
    return config | changes


class FilesAndConfigTests(unittest.TestCase):
    def test_resume_uses_latest_filename_and_ignores_partial_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "2026-10-29_08-02-00.jpg").touch()
            (folder / "2026-10-29_08-01-00.jpg").touch()
            (folder / "2026-10-29_08-03-00_123456.png").touch()
            (folder / "2099-01-01_00-00-00.part").touch()
            (folder / "not-a-timestamp.jpg").touch()
            config = camera(folder, interval_seconds=60)
            expected = datetime(2026, 10, 29, 8, 4, 0, 123456).timestamp()
            self.assertEqual(main.next_capture(config), expected)

    def test_empty_folder_uses_start_date(self):
        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=time.time() + 3600)
            self.assertEqual(main.next_capture(config), config["start"])

    def test_existing_image_has_priority_over_later_start_date(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            latest = datetime(2026, 10, 29, 8, 0, 0)
            (folder / (latest.strftime(main.STAMP) + ".jpg")).touch()
            config = camera(folder, start=latest.timestamp() + 3600, interval_seconds=60)
            self.assertEqual(main.next_capture(config), latest.timestamp() + 60)

    def test_optional_start_date_in_live_mode_keeps_existing_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None, interval_seconds=60)
            with patch.object(main.time, "time", return_value=12345):
                self.assertEqual(main.next_capture(config), 12345)
            latest = datetime(2026, 10, 29, 8, 0, 0)
            (Path(directory) / (latest.strftime(main.STAMP) + ".jpg")).touch()
            self.assertEqual(main.next_capture(config), latest.timestamp() + 60)

    def test_start_date_can_be_omitted_or_null(self):
        valid = dict(url="http://camera.test/Interface/Cameras/GetSnapshot?Camera=cam",
                     folder="cam", interval_seconds=60, history=True)
        with tempfile.TemporaryDirectory() as directory:
            for extra in ({}, {"start_date": None}, {"start_date": ""}):
                with patch.object(main, "BASE_DIR", Path(directory)):
                    with patch.dict(os.environ, {"CAMERAS": json.dumps([valid | extra])}, clear=True):
                        configs, _ = main.load_config()
                self.assertIsNone(configs[0]["start"])

    def test_save_preserves_bytes_and_avoids_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            captured = datetime(2026, 10, 29, 8, 0, 0, 123456)
            paths = [main.save_image(folder, bytes([i]), captured, ".jpg") for i in range(3)]
            self.assertEqual(len(set(paths)), 3)
            self.assertEqual([p.read_bytes() for p in paths], [b"\0", b"\1", b"\2"])
            self.assertFalse(list(folder.glob("*.part")))

    def test_failed_rename_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(Path, "rename", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    main.save_image(Path(directory), b"image", datetime.now(), ".jpg")
            self.assertFalse(list(Path(directory).iterdir()))

    def test_resize_preserves_aspect_ratio(self):
        content = BytesIO()
        Image.new("RGB", (1600, 1200), "red").save(content, format="JPEG")
        with tempfile.TemporaryDirectory() as directory:
            path = main.save_image(Path(directory), content.getvalue(), datetime.now(),
                                   ".jpg", resize=(1280, 720))
            with Image.open(path) as image:
                self.assertEqual(image.size, (960, 720))
                self.assertEqual(image.format, "JPEG")

    def test_resize_does_not_enlarge_small_images(self):
        content = BytesIO()
        Image.new("RGBA", (80, 60), (255, 0, 0, 128)).save(content, format="PNG")
        with tempfile.TemporaryDirectory() as directory:
            path = main.save_image(Path(directory), content.getvalue(), datetime.now(),
                                   ".png", resize=(1280, 720))
            with Image.open(path) as image:
                self.assertEqual(image.size, (80, 60))
                self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 128))

    def test_digifort_capture_defaults_and_overrides(self):
        valid = dict(url="http://camera.test/Interface/Cameras/GetSnapshot?Camera=cam&Width=352",
                     start_date="2026-10-29T08:00:00", folder="cam", interval_seconds=60)
        with tempfile.TemporaryDirectory() as directory:
            for changes, size, quality in (({}, (1920, 1080), 100),
                                          ({"capture_resolution": [3840, 2160], "quality": 95,
                                            "resize": [1280, 720]}, (3840, 2160), 95)):
                with self.subTest(changes=changes), patch.object(main, "BASE_DIR", Path(directory)):
                    with patch.dict(os.environ, {"CAMERAS": json.dumps([valid | changes])}, clear=True):
                        cameras, _ = main.load_config()
                    params = httpx.URL(cameras[0]["url"]).params
                    self.assertEqual(params["Width"], str(size[0]))
                    self.assertEqual(params["Height"], str(size[1]))
                    self.assertEqual(params["Quality"], str(quality))
                    self.assertEqual(params["Profile"], "Recording")
                    self.assertEqual(params["Camera"], "cam")
                    self.assertEqual(params["KeepAspectRatio"], "TRUE")

    def test_loads_multiline_env_relative_to_project(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            configs = [dict(url="https://camera.test/snapshot", start_date="2026-10-29T08:00:00",
                            folder="camera_01", interval_seconds=60)]
            (base / ".env").write_text("CAMERAS='" + json.dumps(configs, indent=2)
                                       + "'\nOUTPUT_DIR=volume/image\nREQUEST_TIMEOUT=10\n")
            with patch.object(main, "BASE_DIR", base), patch.dict(os.environ, {}, clear=True):
                cameras, timeout = main.load_config()
            self.assertEqual(timeout, 10)
            self.assertEqual(cameras[0]["path"], base / "volume/image/camera_01")
            self.assertTrue(cameras[0]["path"].is_dir())
            self.assertEqual(cameras[0]["url"], configs[0]["url"])
            self.assertIsNone(cameras[0]["resize"])

    def test_invalid_configs_are_rejected(self):
        valid = dict(url="https://camera.test/image.jpg", start_date="2026-10-29T08:00:00",
                     folder="camera_01", interval_seconds=60)
        cases = [[], {}, [valid, valid], *[[valid | change] for change in (
            {"folder": "../escape"}, {"folder": "CON"}, {"folder": "a/b"},
            {"url": "rtsp://camera.test/stream"}, {"url": "http:///"},
            {"start_date": "invalid"}, {"interval_seconds": 0},
            {"interval_seconds": True}, {"interval_seconds": "nan"},
            {"resize": [0, 720]}, {"resize": "1280x720"}, {"resize": [1280]},
            {"resize": [True, 720]}, {"capture_resolution": None},
            {"quality": 101}, {"quality": 0}, {"quality": True},
            {"history": "true"}, {"history": True}, {"history_search_hours": 0},
            {"history_retry_seconds": -1},
        )]]
        with tempfile.TemporaryDirectory() as directory:
            for configs in cases:
                with self.subTest(configs=configs), patch.object(main, "BASE_DIR", Path(directory)):
                    with patch.dict(os.environ, {"CAMERAS": json.dumps(configs)}, clear=True):
                        with self.assertRaises((ValueError, TypeError, httpx.InvalidURL)):
                            main.load_config()


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_overdue_camera_captures_immediately_then_waits_its_interval(self):
        calls = []
        finished = asyncio.Event()

        async def handler(request):
            calls.append(time.time())
            if len(calls) == 2:
                finished.set()
            return httpx.Response(200, content=b"unchanged", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "2020-01-01_00-00-00.jpg").touch()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                task = asyncio.create_task(main.collect(camera(folder), client, 1))
                try:
                    await asyncio.wait_for(finished.wait(), 2)
                    self.assertEqual(len(calls), 2)
                    self.assertGreaterEqual(calls[1] - calls[0], 0.025)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_failed_camera_does_not_block_healthy_camera_and_retries(self):
        calls = {"bad": 0, "good": 0}
        finished = asyncio.Event()

        async def handler(request):
            name = request.url.host.split(".")[0]
            calls[name] += 1
            if calls["good"] >= 2 and calls["bad"] >= 2:
                finished.set()
            if name == "bad":
                await asyncio.sleep(0.04)  # Exceeds the total request timeout.
            return httpx.Response(200, content=b"unchanged", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            folders = [Path(directory) / name for name in calls]
            for folder in folders:
                folder.mkdir()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tasks = [asyncio.create_task(main.collect(
                    camera(folder, url=f"https://{folder.name}.test/image?password=secret"), client, 0.02
                )) for folder in folders]
                try:
                    with self.assertLogs(level="ERROR") as logs:
                        await asyncio.wait_for(finished.wait(), 2)
                    self.assertNotIn("secret", " ".join(logs.output))
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            self.assertFalse(list(folders[0].iterdir()))
            self.assertTrue(list(folders[1].glob("*.jpg")))

    async def test_http_errors_and_html_are_not_saved(self):
        finished = asyncio.Event()
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            if calls >= 3:
                finished.set()
            return httpx.Response(503 if calls == 1 else 200, text="error page")

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                task = asyncio.create_task(main.collect(camera(Path(directory)), client, 1))
                try:
                    with self.assertLogs(level="ERROR"):
                        await asyncio.wait_for(finished.wait(), 2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(list(Path(directory).iterdir()))


if __name__ == "__main__":
    unittest.main()
