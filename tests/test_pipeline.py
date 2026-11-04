import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import httpx
import pipeline


class ConfigurationTests(unittest.TestCase):
    def setup_files(self, directory, cvat=None, include_cvat=True):
        root = Path(directory)
        camera = root / "cameras.env"
        camera.write_text("CAMERAS='" + json.dumps([dict(url="http://camera.test/image.jpg", folder="cam", interval_seconds=60)]) + "'\n")
        config = {"cameras": {"env_file": "cameras.env"}}
        if include_cvat:
            config["cvat"] = dict(env_file="cvat.env", enabled=True)
        if cvat is not None:
            (root / "cvat.env").write_text(cvat)
        path = root / "pipeline.env"
        path.write_text("PIPELINE='" + json.dumps(config) + "'\nRESTART_SECONDS=5\n")
        return path

    def valid_cvat(self):
        return ("CVAT_URL=http://cvat.test\nCVAT_USERNAME=user\nCVAT_PASSWORD=secret\n"
                "CVAT_ORGANIZATION=VOD\nDRY_RUN=false\nFOLDERS='"
                + json.dumps([dict(images_dir="images/cam", images_per_task=50)]) + "'\n")

    def test_only_camera_reference_is_enough(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.setup_files(directory, include_cvat=False)
            stages, restart = pipeline.load_pipeline(path)
            self.assertEqual([s["name"] for s in stages], ["coleta"])
            self.assertEqual(restart, 5)

    def test_missing_cvat_file_keeps_camera_stage(self):
        with tempfile.TemporaryDirectory() as directory, self.assertLogs(level="WARNING"):
            stages, _ = pipeline.load_pipeline(self.setup_files(directory))
            self.assertEqual(len(stages), 1)

    def test_incomplete_or_invalid_cvat_keeps_camera_stage(self):
        cases = ("", "CVAT_URL=http://cvat.test\n", self.valid_cvat().replace("FOLDERS='[", "FOLDERS='bad["),
                 self.valid_cvat().replace("CVAT_ORGANIZATION=VOD", "CVAT_ORGANIZATION="))
        for content in cases:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                with self.assertLogs(level="WARNING"):
                    stages, _ = pipeline.load_pipeline(self.setup_files(directory, cvat=content))
                self.assertEqual([s["name"] for s in stages], ["coleta"])

    def test_valid_cvat_enables_second_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            stages, _ = pipeline.load_pipeline(self.setup_files(directory, cvat=self.valid_cvat()))
            self.assertEqual([s["name"] for s in stages], ["coleta", "cvat"])

    def test_corrected_cvat_is_enabled_after_pipeline_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.setup_files(directory)
            with self.assertLogs(level="WARNING"):
                first, _ = pipeline.load_pipeline(path)
            (Path(directory) / "cvat.env").write_text(self.valid_cvat())
            second, _ = pipeline.load_pipeline(path)
            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 2)

    def test_collect_only_does_not_enable_configured_cvat(self):
        with tempfile.TemporaryDirectory() as directory:
            stages, _ = pipeline.load_pipeline(self.setup_files(directory, cvat=self.valid_cvat()), True)
            self.assertEqual(len(stages), 1)

    def test_child_environment_does_not_reuse_unrelated_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, dict(CVAT_PASSWORD="old-password", CAMERAS="[]", VIRTUAL_ENV="old-venv")):
                stages, _ = pipeline.load_pipeline(self.setup_files(directory, include_cvat=False))
            env = stages[0]["env"]
            self.assertNotIn("CVAT_PASSWORD", env)
            self.assertNotIn("CAMERAS", env)
            self.assertNotIn("VIRTUAL_ENV", env)
            self.assertEqual(env["CONFIG_ENV_FILE"], str(Path(directory) / "cameras.env"))

    def test_invalid_camera_config_is_fatal_before_starting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.setup_files(directory, include_cvat=False)
            (Path(directory) / "cameras.env").write_text("CAMERAS='invalid'\n")
            with self.assertRaises(ValueError):
                pipeline.load_pipeline(path)


class SupervisorTests(unittest.TestCase):
    def item(self, name="coleta"):
        return pipeline.stage(name, "digifort-scripts", Path("cameras.env").resolve())

    def test_dependencies_then_python_without_wrapper(self):
        item = self.item()
        sync, worker = MagicMock(), MagicMock()
        sync.poll.return_value = worker.poll.return_value = 0
        with patch.object(pipeline.subprocess, "Popen", side_effect=[sync, worker]) as popen:
            with patch.object(pipeline.time, "sleep"):
                pipeline.supervise([item], 5, "uv")
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args_list[0].args[0][:2], ["uv", "sync"])
        self.assertIn("python", Path(popen.call_args_list[1].args[0][0]).name)
        self.assertTrue(item["done"])

    def test_cvat_failure_does_not_stop_or_restart_healthy_camera(self):
        camera, cvat = self.item(), self.item("cvat")
        healthy, failed, retry = MagicMock(), MagicMock(), MagicMock()
        healthy.poll.return_value = retry.poll.return_value = None
        failed.poll.return_value = 1
        camera.update(process=healthy, phase="run")
        cvat.update(process=failed, phase="run")
        ticks = iter(range(100, 1000, 10))
        with patch.object(pipeline.subprocess, "Popen", return_value=retry) as popen:
            with patch.object(pipeline.time, "monotonic", side_effect=lambda: next(ticks)):
                with patch.object(pipeline.time, "sleep", side_effect=[None, None, KeyboardInterrupt]):
                    with self.assertLogs(level="WARNING"), self.assertRaises(KeyboardInterrupt):
                        pipeline.supervise([camera, cvat], 5, "uv")
            popen.assert_called_once()
            healthy.send_signal.assert_called_once()  # Somente no encerramento geral.
            retry.send_signal.assert_called_once()

    def test_stop_only_signals_active_children(self):
        active, finished = self.item(), self.item("cvat")
        active["process"], finished["process"] = MagicMock(), MagicMock()
        active["process"].poll.return_value = None
        finished["process"].poll.return_value = 0
        pipeline.stop_stages([active, finished])
        active["process"].send_signal.assert_called_once()
        active["process"].wait.assert_called_once_with(timeout=8)
        finished["process"].send_signal.assert_not_called()

    def test_slow_child_is_terminated_as_fallback(self):
        item = self.item()
        process = item["process"] = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("worker", 8), None]
        pipeline.stop_stages([item])
        process.terminate.assert_called_once()


class UnifiedRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_camera_worker_retries_on_connection_error_without_stopping(self):
        camera = {
            "folder": "cam_retry",
            "url": "http://cam.test/snap.jpg",
            "interval_seconds": 60,
            "path": Path(tempfile.gettempdir()) / "cam_retry_test",
            "start": None,
            "end": None,
            "history": False,
        }
        camera["path"].mkdir(parents=True, exist_ok=True)
        stop_event = asyncio.Event()
        attempts = 0

        async def failing_request(client, url, timeout):
            nonlocal attempts
            attempts += 1
            if attempts >= 2:
                stop_event.set()
            raise httpx.ConnectError("Connection refused")

        with patch("pipeline.main.request", side_effect=failing_request):
            with self.assertLogs(level="WARNING") as logs:
                await pipeline.camera_worker(camera, MagicMock(), 5, stop_event, retry_seconds=0.01)
        self.assertGreaterEqual(attempts, 2)
        self.assertTrue(any("Falha na captura" in m for m in logs.output))

    async def test_cvat_worker_retries_on_error_without_stopping(self):
        cvat_config = {
            "CVAT_URL": "http://cvat.test",
            "CVAT_USERNAME": "u",
            "CVAT_PASSWORD": "p",
            "FOLDERS": [],
            "POLL_INTERVAL_SECONDS": 0.01,
        }
        stop_event = asyncio.Event()
        attempts = 0

        def failing_cycle(cfg, dry_run):
            nonlocal attempts
            attempts += 1
            if attempts >= 2:
                stop_event.set()
            raise ConnectionError("CVAT server unreachable")

        with patch("pipeline.main.cvat_cycle", side_effect=failing_cycle):
            with self.assertLogs(level="WARNING") as logs:
                await pipeline.cvat_worker(cvat_config, stop_event, dry_run=True, retry_seconds=0.01)
        self.assertGreaterEqual(attempts, 2)
        self.assertTrue(any("Falha na comunicacao com CVAT" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
