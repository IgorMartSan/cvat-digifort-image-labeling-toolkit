import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.core.acquisition_manager import (
    AcquisitionWorkerConfig,
    acquisition_manager,
    build_digifort_http_url,
    format_position_tag,
)


class TestAcquisitionModule(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_build_digifort_http_url(self):
        url = build_digifort_http_url(
            host="10.247.72.55",
            camera="VO2C01",
            port=8601,
            username="admin",
            password="123",
        )
        self.assertIn("http://admin:123@10.247.72.55:8601/Interface/Cameras/GetSnapshot", url)
        self.assertIn("Camera=VO2C01", url)
        self.assertIn("Width=1920", url)
        self.assertIn("Quality=100", url)

    def test_acquisition_redirect(self):
        resp = self.client.get("/acquisition", follow_redirects=False)
        self.assertEqual(resp.status_code, 307)
        self.assertEqual(resp.headers["location"], "/acquisition/digifort")

    @patch("app.modules.acquisition.router.list_projects")
    def test_digifort_wizard_page_renders(self, mock_projects):
        mock_projects.return_value = [{"id": 1, "name": "Projeto Teste"}]
        resp = self.client.get("/acquisition/digifort")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Aquisição de Câmeras", resp.text)
        self.assertIn("Digifort VMS", resp.text)
        self.assertIn("step-nav-1", resp.text)
        self.assertIn("step-nav-4", resp.text)
        self.assertIn('value="200"', resp.text)
        self.assertIn("200 imagens por tarefa", resp.text)

    def test_workers_page_renders(self):
        resp = self.client.get("/acquisition/workers")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Painel de Workers", resp.text)

    def test_workers_cards_fragment(self):
        resp = self.client.get("/acquisition/workers/cards")
        self.assertEqual(resp.status_code, 200)

    @patch("app.core.acquisition_manager.httpx.Client")
    def test_test_snapshot_api_success(self, mock_httpx_cls):
        mock_client = MagicMock()
        mock_httpx_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "image/jpeg"}
        mock_resp.content = b"fake_jpeg_content"
        mock_client.get.return_value = mock_resp

        payload = {
            "digifort_host": "10.247.72.55",
            "digifort_port": 8601,
            "camera_name": "VO2C01",
            "username": "user",
            "password": "pwd",
        }
        resp = self.client.post("/acquisition/test-snapshot", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        self.assertIn("data:image/jpeg;base64,", data.get("preview_base64", ""))

    def test_create_control_delete_worker(self):
        payload = {
            "camera_name": "TEST_CAM_01",
            "source_type": "digifort",
            "digifort_host": "10.247.72.55",
            "digifort_port": 8601,
            "cvat_project_name": "TEST_PROJ",
            "interval_seconds": 60,
            "images_per_task": 50,
            "auto_remove": True,
            "auto_start": False,  # Don't spin real background loop in unit test
        }
        # 1. Create
        create_resp = self.client.post("/acquisition/workers", json=payload)
        self.assertEqual(create_resp.status_code, 200)
        worker_data = create_resp.json()
        worker_id = worker_data["id"]
        self.assertEqual(worker_data["camera_name"], "TEST_CAM_01")

        # 2. Control
        pause_resp = self.client.post(f"/acquisition/workers/{worker_id}/control?action=pause")
        self.assertEqual(pause_resp.status_code, 200)

        # 3. List
        list_resp = self.client.get("/acquisition/workers/cards")
        self.assertEqual(list_resp.status_code, 200)
        self.assertIn("TEST_CAM_01", list_resp.text)

        # 4. Delete
        del_resp = self.client.delete(f"/acquisition/workers/{worker_id}")
        self.assertEqual(del_resp.status_code, 200)

    def test_saved_urls_api_lifecycle(self):
        # 1. Get initial saved urls
        resp = self.client.get("/acquisition/saved-urls")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("urls", data)
        self.assertTrue(isinstance(data["urls"], list))

        # 2. Delete an existing url
        if data["urls"]:
            target_url = data["urls"][0]
            del_resp = self.client.delete(f"/acquisition/saved-urls?url={target_url}")
            self.assertEqual(del_resp.status_code, 200)
            self.assertTrue(del_resp.json().get("success"))

    @patch("app.core.acquisition_manager.httpx.Client")
    def test_snapshot_with_direct_url_saves_history(self, mock_httpx_cls):
        mock_client = MagicMock()
        mock_httpx_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "image/jpeg"}
        mock_resp.content = b"fake_jpeg_content"
        mock_client.get.return_value = mock_resp

        direct_url = "http://10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera=CAM_HISTORY_TEST"
        payload = {"direct_url": direct_url}
        resp = self.client.post("/acquisition/test-snapshot", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("extracted_camera"), "CAM_HISTORY_TEST")

        # Verifica se URL foi para o histórico
        urls_resp = self.client.get("/acquisition/saved-urls")
        self.assertIn(direct_url, urls_resp.json().get("urls", []))

    def test_create_worker_with_direct_rtsp_url(self):
        payload = {
            "camera_name": "RTSP_CAM_01",
            "direct_url": "rtsp://ax12813:ims@2024@10.247.72.55/Interface/Cameras/Media?Camera=VO2C02",
            "interval_seconds": 300,
            "images_per_task": 50,
            "auto_start": False,
        }
        create_resp = self.client.post("/acquisition/workers", json=payload)
        self.assertEqual(create_resp.status_code, 200)
        worker_data = create_resp.json()
        self.assertEqual(worker_data["source_type"], "rtsp")
        self.assertEqual(worker_data["digifort_host"], "10.247.72.55")

        # Cleanup
        self.client.delete(f"/acquisition/workers/{worker_data['id']}")

    @patch("app.core.acquisition_manager.httpx.Client")
    def test_snapshot_ptz_telemetry_extraction(self, mock_httpx_cls):
        mock_client = MagicMock()
        mock_httpx_cls.return_value.__enter__.return_value = mock_client

        def mock_get(url, **kwargs):
            resp = MagicMock()
            if "GetSnapshot" in url:
                resp.status_code = 200
                resp.headers = {"content-type": "image/jpeg"}
                resp.content = b"fake_jpeg_content"
            elif "PTZ/GetStatus" in url:
                resp.status_code = 200
                resp.headers = {"content-type": "application/json"}
                resp.json.return_value = {
                    "Response": {
                        "Code": 0,
                        "Message": "OK",
                        "Data": {
                            "PTZStatus": [{
                                "Name": "VO2C01",
                                "CurrentPreset": 1,
                                "CurrentPosition": {
                                    "Supported": True,
                                    "Initialized": True,
                                    "Pan": 41,
                                    "Tilt": 39,
                                    "Zoom": 46
                                },
                                "Usage": {"InUse": False, "User": "", "IP": ""},
                            }]
                        }
                    }
                }
            elif "PTZ/GetPresets" in url:
                resp.status_code = 200
                resp.headers = {"content-type": "application/json"}
                resp.json.return_value = {
                    "Response": {
                        "Code": 0,
                        "Message": "OK",
                        "Data": {
                            "Presets": [{"ID": 1, "Description": "Medicao"}]
                        }
                    }
                }
            else:
                resp.status_code = 200
                resp.headers = {"content-type": "application/json"}
                resp.json.return_value = {
                    "Response": {
                        "Code": 0,
                        "Message": "OK",
                        "Data": {
                            "Cameras": [{"Name": "VO2C01", "Active": True, "Working": True}]
                        }
                    }
                }
            return resp

        mock_client.get.side_effect = mock_get

        payload = {
            "direct_url": "http://admin:123@10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera=VO2C01",
            "camera_name": "VO2C01"
        }
        resp = self.client.post("/acquisition/test-snapshot", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        info = data.get("camera_info", {})
        self.assertTrue(info.get("available"))
        self.assertTrue(info.get("ptz_supported"))
        self.assertEqual(info.get("pan"), 41)
        self.assertEqual(info.get("tilt"), 39)
        self.assertEqual(info.get("zoom"), 46)
        self.assertEqual(info.get("current_preset"), 1)
        self.assertEqual(info.get("current_preset_name"), "Medicao")

    def test_create_worker_with_ptz_presets_and_historical_mode(self):
        payload = {
            "camera_name": "VO2C01_TEST",
            "direct_url": "http://user:pwd@10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera=VO2C01",
            "cvat_project_name": "Projeto_Monitoramento",
            "cvat_organization": "VOD",
            "interval_seconds": 600,
            "images_per_task": 50,
            "auto_remove": True,
            "auto_start": False,
            "ptz_mode": "presets",
            "selected_presets": [1, 5],
            "capture_mode": "history",
            "start_date": "2026-11-01T08:00:00",
            "end_date": "2026-11-02T18:00:00",
            "sync_pipeline": False,
        }
        create_resp = self.client.post("/acquisition/workers", json=payload)
        self.assertEqual(create_resp.status_code, 200)
        worker_data = create_resp.json()
        self.assertEqual(worker_data["ptz_mode"], "presets")
        self.assertEqual(worker_data["selected_presets"], [1, 5])
        self.assertEqual(worker_data["capture_mode"], "history")
        self.assertEqual(worker_data["start_date"], "2026-11-01T08:00:00")
        self.assertEqual(worker_data["cvat_project_name"], "Projeto_Monitoramento")

        # Cleanup
        self.client.delete(f"/acquisition/workers/{worker_data['id']}")

    def test_create_worker_auto_resolves_camera_name_from_url(self):
        # Envia payload sem camera_name explícito
        payload = {
            "direct_url": "http://user:pwd@10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera=CAM_AUTO_EXTRACT",
            "cvat_project_name": "Projeto_Auto",
            "interval_seconds": 60,
            "images_per_task": 50,
            "auto_start": False,
        }
        create_resp = self.client.post("/acquisition/workers", json=payload)
        self.assertEqual(create_resp.status_code, 200)
        worker_data = create_resp.json()
        self.assertEqual(worker_data["camera_name"], "CAM_AUTO_EXTRACT")

        # Cleanup
        self.client.delete(f"/acquisition/workers/{worker_data['id']}")

    def test_checkpoint_persistence_and_resume(self):
        """Valida que o checkpoint .history.json persiste a posição e retoma exatamente onde parou."""
        from datetime import datetime
        import shutil
        from app.core.acquisition_manager import ActiveWorker, AcquisitionWorkerConfig

        cam_name = "CAM_TEST_CHECKPOINT"
        cfg = AcquisitionWorkerConfig(
            id="worker_test_cp",
            camera_name=cam_name,
            capture_mode="history",
            start_date="2026-11-01T10:00:00",
            end_date="2026-11-05T18:00:00",
            interval_seconds=60.0,
            auto_start=False,
        )
        worker = ActiveWorker(cfg)
        test_dt = datetime(2026, 11, 2, 14, 30, 0)
        worker.history_cursor = test_dt.timestamp()
        worker.captured_count = 120
        worker.tasks_created = 2
        worker.save_checkpoint(last_filename="2026-11-02_14-30-00_000000.jpg", last_file_dt=test_dt)

        # Verifica se o arquivo .history.json foi criado no disco
        self.assertTrue(worker.history_file.is_file())

        # Cria novo worker simulando reinício completo do servidor
        worker_rebooted = ActiveWorker(cfg)
        self.assertTrue(worker_rebooted.load_checkpoint())
        self.assertEqual(worker_rebooted.history_cursor, test_dt.timestamp())
        self.assertEqual(worker_rebooted.captured_count, 120)
        self.assertEqual(worker_rebooted.tasks_created, 2)
        self.assertEqual(worker_rebooted.current_recording_time, "02/11/2026 14:30:00")

        # Verifica dados no to_dict()
        worker_dict = worker_rebooted.to_dict()
        self.assertEqual(worker_dict["current_recording_time"], "02/11/2026 14:30:00")
        self.assertTrue(worker_dict["has_checkpoint"])
        self.assertIsNotNone(worker_dict["history_progress_percent"])
        self.assertGreater(worker_dict["history_progress_percent"], 0)

        # Cleanup
        if worker.camera_folder.is_dir():
            shutil.rmtree(worker.camera_folder, ignore_errors=True)

    def test_workers_cards_renders_recording_time_and_checkpoint_status(self):
        """Valida que o fragmento HTML dos cards renderiza o tempo da gravação e status de checkpoint."""
        payload = {
            "camera_name": "CAM_UI_RECORDING_TEST",
            "direct_url": "http://10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera=CAM_UI_RECORDING_TEST",
            "capture_mode": "history",
            "start_date": "2026-11-01T10:00:00",
            "end_date": "2026-11-05T18:00:00",
            "interval_seconds": 60,
            "images_per_task": 100,
            "auto_start": False,
        }
        create_resp = self.client.post("/acquisition/workers", json=payload)
        self.assertEqual(create_resp.status_code, 200)
        w_id = create_resp.json()["id"]

        resp = self.client.get("/acquisition/workers/cards")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Posição Atual da Gravação", resp.text)
        self.assertIn(".history.json", resp.text)
        self.assertIn("Retomada Contínua", resp.text)

        # Cleanup
        self.client.delete(f"/acquisition/workers/{w_id}", params={"delete_images": True})

    def test_format_position_tag(self):
        # 1. Preset com nome
        t1 = {
            "available": True,
            "ptz_supported": True,
            "current_preset": 1,
            "current_preset_name": "Medição",
        }
        self.assertEqual(format_position_tag(t1), "_Preset-1_Medicao")

        # 2. Preset sem nome
        t2 = {
            "available": True,
            "ptz_supported": True,
            "current_preset": 3,
            "current_preset_name": None,
        }
        self.assertEqual(format_position_tag(t2), "_Preset-3")

        # 3. PTZ sem preset ativo, mas com Pan/Tilt/Zoom
        t3 = {
            "available": True,
            "ptz_supported": True,
            "current_preset": None,
            "pan": 41.2,
            "tilt": 39.0,
            "zoom": 46.0,
        }
        self.assertEqual(format_position_tag(t3), "_Pan41_Tilt39_Zoom46")

        # 4. Câmera fixa ou sem telemetria
        t4 = {"available": False}
        self.assertEqual(format_position_tag(t4), "")
        self.assertEqual(format_position_tag(None), "")


if __name__ == "__main__":
    unittest.main()
