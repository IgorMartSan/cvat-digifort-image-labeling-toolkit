import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

import main

BASE = datetime(2026, 10, 1).timestamp()


def camera(folder, **changes):
    return dict(url="http://camera.test/Interface/Cameras/GetSnapshot?Camera=cam",
                folder=folder.name, path=folder, start=BASE, interval_seconds=60,
                history=True, history_search_hours=6, history_retry_seconds=0.001) | changes


def timeline(start=None):
    text = "" if start is None else (
        "--DigifortBoundary\r\nContent-Type: text/plain\r\n\r\n"
        "RESPONSE_CODE=0\r\nTIMELINEDATA_MEDIAKIND=VIDEO\r\n"
        f"TIMELINEDATA_STARTTIME={datetime.fromtimestamp(BASE + start).isoformat(sep=' ')}\r\n"
    )
    return httpx.Response(200, text=text, headers={"Content-Type": "multipart/x-mixed-replace"})


def requested_time(request):
    params = request.url.params
    return datetime.strptime(params["Date"] + " " + params["Time"], "%Y.%m.%d %H.%M.%S.%f").timestamp() - BASE


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_images_resume_from_persisted_live_progress(self):
        stamps = []

        async def handler(request):
            self.assertEqual(request.url.path, "/Interface/Cameras/Playback/GetSnapshot")
            stamps.append(requested_time(request))
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None)
            main.write_progress(config, main.read_progress(config) | dict(next=BASE + 60, found=True, done=True))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 180):
                    with patch.object(main, "oldest_recording", new=AsyncMock()) as discover:
                        await main.collect_history(config, client, 1)
                        discover.assert_not_awaited()
            self.assertEqual(stamps, [60, 120])

    async def test_live_capture_persists_next_time_before_images_are_removed(self):
        async def handler(request):
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None, interval_seconds=3600)
            main.write_progress(config, main.read_progress(config) | dict(next=BASE, done=True))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                collector = asyncio.create_task(main.collect(config, client, 1))
                try:
                    async with asyncio.timeout(2):
                        while not main.read_progress(config)["found"]:
                            await asyncio.sleep(0.01)
                finally:
                    collector.cancel()
                    await asyncio.gather(collector, return_exceptions=True)
            state = main.read_progress(config)
            self.assertGreater(state["next"], time.time())
            for path in config["path"].glob("*.jpg"):
                path.unlink()
            self.assertEqual(main.next_capture(config), state["next"])

    async def test_existing_images_resume_without_rediscovering_oldest(self):
        for progress in ("missing", "behind", "done", "corrupt", "changed_start"):
            with self.subTest(progress=progress), tempfile.TemporaryDirectory() as directory:
                config = camera(Path(directory), start=None)
                main.save_image(config["path"], b"saved", datetime.fromtimestamp(BASE + 120),
                                ".jpg", historical=True)
                if progress in ("behind", "done"):
                    main.write_progress(config, main.read_progress(config) |
                                        dict(next=BASE + 60, found=True, done=progress == "done"))
                elif progress == "corrupt":
                    (config["path"] / ".history.json").write_text("invalid json")
                elif progress == "changed_start":
                    config["start"] = BASE + 1000
                stamps = []

                async def handler(request):
                    self.assertEqual(request.url.path, "/Interface/Cameras/Playback/GetSnapshot")
                    stamps.append(requested_time(request))
                    return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with patch.object(main, "oldest_recording", new=AsyncMock()) as discover:
                        with patch.object(main.time, "time", return_value=BASE + 300), self.assertLogs(level="INFO"):
                            await main.collect_history(config, client, 1)
                        discover.assert_not_awaited()
                self.assertEqual(stamps, [180, 240])
                self.assertEqual(len(list(config["path"].glob("*.jpg"))), 3)

    async def test_progress_after_already_checked_gaps_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None)
            main.save_image(config["path"], b"saved", datetime.fromtimestamp(BASE), ".jpg", historical=True)
            main.write_progress(config, main.read_progress(config) | dict(next=BASE + 180, found=True))
            stamps = []

            async def handler(request):
                stamps.append(requested_time(request))
                return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 300):
                    await main.collect_history(config, client, 1)
            self.assertEqual(stamps, [180, 240])

    async def test_discovers_oldest_without_assuming_stream_order(self):
        queries = []

        async def handler(request):
            queries.append(request.url.params)
            return timeline(1000) if len(queries) == 1 else timeline(400)

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                oldest = await main.oldest_recording(camera(Path(directory), start=None), client, 1)
        self.assertEqual(oldest, BASE + 400)
        self.assertEqual(len(queries), 2)
        end = datetime.strptime(queries[1]["EndDate"] + " " + queries[1]["EndTime"], "%Y.%m.%d %H.%M.%S.%f")
        self.assertEqual(end.timestamp(), BASE + 1000)

    async def test_empty_auto_discovery_returns_none(self):
        async def handler(request):
            return timeline()

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.assertIsNone(await main.oldest_recording(camera(Path(directory), start=None), client, 1))

    async def test_incomplete_discovery_is_not_treated_as_no_recordings(self):
        async def handler(request):
            return httpx.Response(200, text="RESPONSE_CODE=0\nTIMELINEDATA_MEDIAKIND=VIDEO\n",
                                  headers={"Content-Type": "multipart/x-mixed-replace"})

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(ValueError):
                    await main.oldest_recording(camera(Path(directory), start=None), client, 1)

    async def test_auto_start_persists_and_does_not_rediscover_on_restart(self):
        async def handler(request):
            self.assertNotIn("GetTimelineData", request.url.path)
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main, "oldest_recording", new=AsyncMock(return_value=BASE)) as discover:
                    with patch.object(main.time, "time", return_value=BASE + 60):
                        await main.collect_history(config, client, 1)
                        await main.collect_history(config, client, 1)
                discover.assert_awaited_once()
            self.assertTrue(main.read_progress(config)["done"])
            self.assertEqual(len(list(Path(directory).glob("*.jpg"))), 1)

    async def test_auto_start_without_footage_finishes_history(self):
        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory), start=None)
            with patch.object(main, "oldest_recording", new=AsyncMock(return_value=None)):
                await main.collect_history(config, None, 1)
            self.assertTrue(main.read_progress(config)["done"])

    async def test_searches_six_hour_windows_then_keeps_interval_across_gap(self):
        windows, snapshots = [], []

        async def handler(request):
            if request.url.path.endswith("GetTimelineData"):
                windows.append(request.url.params)
                return timeline() if len(windows) == 1 else timeline(21630)
            stamp = requested_time(request)
            snapshots.append(stamp)
            if stamp == 21690:
                return httpx.Response(200, text="RESPONSE_CODE=10200\r\n")
            return httpx.Response(200, content=b"jpeg-bytes", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 21760), self.assertLogs(level="INFO"):
                    await main.collect_history(config, client, 1)
                    # Restart after completion makes no additional requests.
                    await main.collect_history(config, client, 1)
            self.assertEqual(len(windows), 2)
            first_start = datetime.strptime(windows[0]["StartDate"] + " " + windows[0]["StartTime"], "%Y.%m.%d %H.%M.%S.%f")
            first_end = datetime.strptime(windows[0]["EndDate"] + " " + windows[0]["EndTime"], "%Y.%m.%d %H.%M.%S.%f")
            self.assertEqual((first_end - first_start).total_seconds(), 6 * 3600)
            self.assertEqual(snapshots, [21630, 21690, 21750])
            self.assertEqual(len(list(Path(directory).glob("*.jpg"))), 2)
            state = main.read_progress(config)
            self.assertTrue(state["found"])
            self.assertTrue(state["done"])

    async def test_network_failure_retries_same_search_window(self):
        queries = []

        async def handler(request):
            queries.append(str(request.url))
            if len(queries) == 1:
                raise httpx.ReadTimeout("timeout")
            return timeline()

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 100), self.assertLogs(level="ERROR"):
                    await main.collect_history(camera(Path(directory)), client, 1)
            self.assertEqual(queries[0], queries[1])
            self.assertEqual(len(queries), 2)

    async def test_snapshot_timeout_retries_same_historical_time(self):
        stamps = []

        async def handler(request):
            if request.url.path.endswith("GetTimelineData"):
                return timeline(0)
            stamps.append(requested_time(request))
            if len(stamps) == 1:
                raise httpx.ReadTimeout("timeout")
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 60), self.assertLogs(level="ERROR"):
                    await main.collect_history(camera(Path(directory)), client, 1)
            self.assertEqual(stamps, [0, 0])
            self.assertEqual(len(list(Path(directory).glob("*.jpg"))), 1)

    async def test_restart_after_gap_preserves_fine_interval(self):
        stamps = []

        async def handler(request):
            self.assertNotIn("GetTimelineData", request.url.path)
            stamp = requested_time(request)
            stamps.append(stamp)
            return httpx.Response(200, text="RESPONSE_CODE=10200\r\n")

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory))
            state = main.read_progress(config) | dict(next=BASE + 60, found=True)
            main.write_progress(config, state)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 180), self.assertLogs(level="WARNING"):
                    await main.collect_history(config, client, 1)
            self.assertEqual(stamps, [60, 120])
            self.assertTrue(main.read_progress(config)["done"])

    async def test_existing_historical_file_is_not_downloaded_again(self):
        async def handler(request):
            self.fail("Imagem ja salva nao deve ser baixada novamente")

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory))
            main.save_image(config["path"], b"original", datetime.fromtimestamp(BASE), ".jpg", historical=True)
            main.write_progress(config, main.read_progress(config) | dict(found=True))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 60):
                    await main.collect_history(config, client, 1)
            self.assertEqual(len(list(Path(directory).glob("*.jpg"))), 1)
            self.assertEqual(next(Path(directory).glob("*.jpg")).read_bytes(), b"original")

    async def test_live_collection_starts_after_history_finishes(self):
        called = asyncio.Event()

        async def handler(request):
            self.assertEqual(request.url.path, "/Interface/Cameras/GetSnapshot")
            called.set()
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})

        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory))
            main.write_progress(config, main.read_progress(config) | dict(done=True))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                task = asyncio.create_task(main.collect(config, client, 1))
                try:
                    await asyncio.wait_for(called.wait(), 2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(list(Path(directory).glob("*.jpg")))

    async def test_permission_error_does_not_advance_progress(self):
        queries = []

        async def handler(request):
            queries.append(str(request.url))
            if len(queries) == 1:
                return httpx.Response(200, text="RESPONSE_CODE=5\r\n")
            return timeline()

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(main.time, "time", return_value=BASE + 10), self.assertLogs(level="ERROR"):
                    await main.collect_history(camera(Path(directory)), client, 1)
            self.assertEqual(queries[0], queries[1])


class TimelineTests(unittest.TestCase):
    def test_finds_short_recording_between_window_boundaries(self):
        self.assertEqual(main.first_recording(timeline(123.456), BASE, BASE + 21600), BASE + 123.456)

    def test_rejects_invalid_timeline_instead_of_treating_as_empty(self):
        response = httpx.Response(200, text="login page", headers={"Content-Type": "text/html"})
        with self.assertRaises(ValueError):
            main.first_recording(response, 0, 21600)

    def test_rejects_truncated_multipart_confirmation(self):
        response = httpx.Response(200, text="RESPONSE_CODE=0\n",
                                  headers={"Content-Type": "multipart/x-mixed-replace"})
        with self.assertRaises(ValueError):
            main.first_recording(response, BASE, BASE + 60)

    def test_playback_requests_native_resolution_and_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            url = main.playback_url(camera(Path(directory)), "GetSnapshot", BASE + 123.456)
        self.assertEqual(url.params["Width"], "-1")
        self.assertEqual(url.params["Height"], "-1")
        self.assertEqual(url.params["Quality"], "100")
        self.assertTrue(url.params["Time"].endswith(".456"))

    def test_changed_start_date_resets_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            config = camera(Path(directory))
            main.write_progress(config, main.read_progress(config) | dict(next=BASE + 300, found=True, done=True))
            changed = config | dict(start=BASE + 100)
            self.assertEqual(main.read_progress(changed)["next"], BASE + 100)
            self.assertFalse(main.read_progress(changed)["found"])


if __name__ == "__main__":
    unittest.main()
