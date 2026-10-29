import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx

import main
from test_history import BASE, camera


class ProgressTests(unittest.TestCase):
    def test_readable_last_image_resumes_after_files_are_deleted_and_settings_change(self):
        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None)
            captured = datetime.fromtimestamp(BASE + 60)
            state = main.read_progress(config) | dict(next=BASE + 120, found=True,
                last_image=captured.strftime(main.STAMP) + ".jpg", last_image_at=captured.isoformat())
            main.write_progress(config, state)
            saved = json.loads((config["path"] / ".history.json").read_text())
            self.assertEqual(saved["folder"], config["folder"])
            self.assertEqual(saved["last_image_at"], captured.isoformat())
            self.assertEqual(saved["next_capture_at"], datetime.fromtimestamp(BASE + 120).isoformat(timespec="microseconds"))
            changed = config | dict(start=BASE + 1000, interval_seconds=120)
            self.assertEqual(main.next_capture(changed), BASE + 180)

    def test_last_image_date_recovers_when_numeric_cursor_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None)
            saved = main.read_progress(config) | dict(last_image_at=datetime.fromtimestamp(BASE).isoformat(),
                                                      last_image="2026-10-01_00-00-00.jpg")
            saved.pop("next")
            (config["path"] / ".history.json").write_text(json.dumps(saved))
            self.assertEqual(main.next_capture(config), BASE + 60)

    def test_progress_from_another_folder_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None)
            state = main.read_progress(config) | dict(next=BASE + 60, found=True)
            main.write_progress(config, state)
            changed = config | dict(folder="another_camera")
            with self.assertLogs(level="WARNING"):
                self.assertFalse(main.read_progress(changed)["found"])

    def test_legacy_checkpoint_without_last_image_fields_is_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None)
            identity = main.read_progress(config)["identity"]
            saved = dict(identity=identity, next=BASE + 60, found=True, done=True)
            (config["path"] / ".history.json").write_text(json.dumps(saved))
            state = main.read_progress(config)
            self.assertEqual(state["next"], BASE + 60)
            self.assertIsNone(state["last_image_at"])
            self.assertEqual(state["folder"], config["folder"])

    def test_failed_atomic_replace_keeps_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None)
            state = main.read_progress(config) | dict(next=BASE + 60, found=True)
            main.write_progress(config, state)
            with patch.object(Path, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    main.write_progress(config, state | dict(next=BASE + 120))
            self.assertEqual(main.read_progress(config)["next"], BASE + 60)


class CaptureProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_live_camera_records_last_image_even_without_history(self):
        async def handler(request):
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None, history=False,
                            url="http://camera.test/image.jpg", interval_seconds=3600)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                collector = asyncio.create_task(main.collect(config, client, 1))
                try:
                    async with asyncio.timeout(2):
                        while main.read_progress(config)["last_image_at"] is None:
                            await asyncio.sleep(0.01)
                finally:
                    collector.cancel()
                    await asyncio.gather(collector, return_exceptions=True)
            state = main.read_progress(config)
            self.assertEqual(state["folder"], config["folder"])
            saved_path = config["path"] / state["last_image"]
            self.assertTrue(saved_path.exists())
            captured = datetime.fromisoformat(state["last_image_at"])
            self.assertEqual(state["next"], captured.timestamp() + 3600)
            saved_path.unlink()
            self.assertEqual(main.next_capture(config.copy()), state["next"])

    async def test_missing_historical_image_does_not_change_last_saved_date(self):
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 2:
                return httpx.Response(200, text="RESPONSE_CODE=10200\r\n")
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None)
            main.write_progress(config, main.read_progress(config) | dict(next=BASE + 60, found=True))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 180), self.assertLogs(level="WARNING"):
                    await main.collect_history(config, client, 1)
            state = main.read_progress(config)
            self.assertEqual(datetime.fromisoformat(state["last_image_at"]).timestamp(), BASE + 60)
            self.assertEqual(state["next"], BASE + 180)
            self.assertTrue((config["path"] / state["last_image"]).exists())


if __name__ == "__main__":
    unittest.main()
