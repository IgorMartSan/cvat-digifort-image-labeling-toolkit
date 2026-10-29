import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx

import main
from test_history import BASE, camera, requested_time, timeline


class DateConfigTests(unittest.TestCase):
    def test_absolute_and_relative_dates(self):
        self.assertEqual(main.parse_date(30, BASE), BASE - 30 * 86400)
        self.assertEqual(main.parse_date(0.5, BASE), BASE - 12 * 3600)
        self.assertEqual(main.parse_date(0, BASE), BASE)
        self.assertIsNone(main.parse_date(None, BASE))
        self.assertEqual(main.parse_date("2026-10-01T08:30:00", BASE),
                         datetime(2026, 10, 1, 8, 30).timestamp())
        self.assertEqual(main.parse_date("2026-10-01T08:30:00-03:00", BASE),
                         datetime.fromisoformat("2026-10-01T11:30:00+00:00").timestamp())

    def test_rejects_invalid_dates(self):
        for value in (True, False, -1, float("inf"), float("nan"), 10**1000, "30", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    main.parse_date(value, BASE)

    def test_config_uses_one_reference_for_start_and_end(self):
        config = dict(url="http://camera.test/Interface/Cameras/GetSnapshot?Camera=cam",
                      folder="cam", interval_seconds=60, history=True,
                      start_date=30, end_date=0)
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "BASE_DIR", Path(directory)):
            with patch.dict(os.environ, {"CAMERAS": json.dumps([config])}, clear=True):
                with patch.object(main.time, "time", return_value=BASE):
                    configs, _ = main.load_config()
            self.assertEqual(configs[0]["start"], BASE - 30 * 86400)
            self.assertEqual(configs[0]["end"], BASE)
            config["end_date"] = 31
            with patch.dict(os.environ, {"CAMERAS": json.dumps([config])}, clear=True):
                with self.assertRaises(ValueError):
                    main.load_config()

    def test_relative_period_resumes_progress_when_reference_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start_date=30, start=BASE - 30 * 86400,
                            end_date=0, end=BASE)
            main.write_progress(config, main.read_progress(config) | dict(next=BASE, found=True, done=True))
            later = config | dict(start=config["start"] + 86400, end=BASE + 86400)
            state = main.read_progress(later)
            self.assertEqual(state["next"], BASE)
            self.assertTrue(state["found"])
            self.assertFalse(state["done"])


class PeriodCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_includes_end_timestamp_and_never_starts_live(self):
        stamps = []

        async def handler(request):
            self.assertEqual(request.url.path, "/Interface/Cameras/Playback/GetSnapshot")
            stamps.append(requested_time(request))
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), end=BASE + 120)
            main.save_image(config["path"], b"saved", datetime.fromtimestamp(BASE), ".jpg", historical=True)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 300):
                    await main.collect(config, client, 1)
            self.assertEqual(stamps, [60, 120])
            self.assertEqual(len(list(config["path"].glob("*.jpg"))), 3)

    async def test_empty_history_search_is_limited_by_end_date(self):
        queries = []

        async def handler(request):
            self.assertEqual(request.url.path, "/Interface/Cameras/Playback/GetTimelineData")
            queries.append(request.url.params)
            return timeline()

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), end=BASE + 120)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 300):
                    await main.collect(config, client, 1)
            self.assertEqual(len(queries), 1)
            end = datetime.strptime(queries[0]["EndDate"] + " " + queries[0]["EndTime"], "%Y.%m.%d %H.%M.%S.%f")
            self.assertEqual(end.timestamp(), BASE + 120)

    async def test_oldest_discovery_is_limited_by_end_date(self):
        queries = []

        async def handler(request):
            queries.append(request.url.params)
            return timeline(100)

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 1000):
                    first = await main.oldest_recording(camera(Path(directory), start=None, end=BASE + 600), client, 1)
            self.assertEqual(first, BASE + 100)
            end = datetime.strptime(queries[0]["EndDate"] + " " + queries[0]["EndTime"], "%Y.%m.%d %H.%M.%S.%f")
            self.assertEqual(end.timestamp(), BASE + 600)

    async def test_existing_image_past_end_finishes_without_requests(self):
        async def handler(request):
            self.fail("Periodo concluido nao deve acessar a camera")

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), end=BASE + 60)
            main.save_image(config["path"], b"saved", datetime.fromtimestamp(BASE + 120), ".jpg", historical=True)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await main.collect(config, client, 1)

    async def test_live_collection_finishes_when_end_is_reached(self):
        calls = []

        async def handler(request):
            calls.append(time.time())
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            now = time.time()
            config = camera(Path(directory), history=False, start=None, end=now + 0.2, interval_seconds=0.05)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await asyncio.wait_for(main.collect(config, client, 1), 2)
            self.assertTrue(calls)
            self.assertTrue(all(stamp <= config["end"] for stamp in calls))
            self.assertTrue(list(config["path"].glob("*.jpg")))

    async def test_request_cannot_save_an_image_after_live_deadline(self):
        async def handler(request):
            await asyncio.sleep(0.2)
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), history=False, start=None, end=time.time() + 0.05)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertLogs(level="ERROR"):
                    await asyncio.wait_for(main.collect(config, client, 1), 2)
            self.assertFalse(list(config["path"].glob("*.jpg")))


if __name__ == "__main__":
    unittest.main()
