import shutil
import unittest
from datetime import datetime
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

    def test_delete_worker_removes_folder_and_checkpoint(self):
        """Valida que excluir o worker remove o worker, .history.json e a pasta do disco."""
        payload = {
            "camera_name": "CAM_DELETE_CLEANUP_TEST",
            "direct_url": "http://10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera=CAM_DELETE_CLEANUP_TEST",
            "capture_mode": "history",
            "start_date": "2026-11-01T10:00:00",
            "auto_start": False,
        }
        create_resp = self.client.post("/acquisition/workers", json=payload)
        self.assertEqual(create_resp.status_code, 200)
        w_id = create_resp.json()["id"]

        worker = acquisition_manager.get_worker(w_id)
        self.assertIsNotNone(worker)
        # Salva checkpoint simulado
        worker.save_checkpoint()
        self.assertTrue(worker.history_file.is_file())
        self.assertTrue(worker.camera_folder.is_dir())

        # Exclui worker via API
        del_resp = self.client.delete(f"/acquisition/workers/{w_id}")
        self.assertEqual(del_resp.status_code, 200)

        # Worker não deve mais existir na memória nem no storage
        self.assertIsNone(acquisition_manager.get_worker(w_id))
        self.assertFalse(worker.history_file.is_file())
        self.assertFalse(worker.camera_folder.is_dir())

    def test_new_worker_with_different_start_date_resets_history(self):
        """Valida que criar um novo worker com outra data de início não herda o checkpoint antigo."""
        from datetime import datetime
        from app.core.acquisition_manager import ActiveWorker, AcquisitionWorkerConfig

        cam_name = "CAM_RESET_DATE_TEST"
        cfg1 = AcquisitionWorkerConfig(
            id="worker_old_1",
            camera_name=cam_name,
            capture_mode="history",
            start_date="2026-11-01T08:00:00",
            interval_seconds=60.0,
            auto_start=False,
        )
        w1 = ActiveWorker(cfg1)
        w1.history_cursor = datetime(2026, 11, 4, 12, 0, 0).timestamp()
        w1.captured_count = 500
        w1.tasks_created = 5
        w1.save_checkpoint()
        self.assertTrue(w1.history_file.is_file())

        # Novo worker com data de início diferente (06/11 em vez de 01/11)
        cfg2 = AcquisitionWorkerConfig(
            id="worker_new_2",
            camera_name=cam_name,
            capture_mode="history",
            start_date="2026-11-06T08:55:00",
            interval_seconds=60.0,
            auto_start=False,
        )
        w2 = ActiveWorker(cfg2)
        # Não deve carregar o checkpoint do worker antigo
        self.assertFalse(w2.load_checkpoint())
        self.assertIsNone(w2.history_cursor)
        self.assertEqual(w2.captured_count, 0)
        self.assertEqual(w2.tasks_created, 0)

        # Cleanup
        if w1.camera_folder.is_dir():
            shutil.rmtree(w1.camera_folder, ignore_errors=True)

    def test_new_worker_with_same_start_date_starts_fresh_and_does_not_inherit_history(self):
        """Valida que criar um novo worker com a MESMA data de início não herda o checkpoint nem fotos do worker anterior."""
        same_date = "2026-11-06T08:55:00"
        cam_name = "CAM_SAME_DATE_FRESH_TEST"

        # 1. Cria primeiro worker
        p1 = {
            "camera_name": cam_name,
            "direct_url": f"http://10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera={cam_name}",
            "capture_mode": "history",
            "start_date": same_date,
            "interval_seconds": 60,
            "auto_start": False,
        }
        resp1 = self.client.post("/acquisition/workers", json=p1)
        self.assertEqual(resp1.status_code, 200)
        w1_id = resp1.json()["id"]

        w1 = acquisition_manager.get_worker(w1_id)
        self.assertIsNotNone(w1)
        # Simula progresso do worker 1 (avançou 3 dias e tirou 450 fotos)
        advanced_dt = datetime(2026, 11, 9, 14, 0, 0)
        w1.history_cursor = advanced_dt.timestamp()
        w1.captured_count = 450
        w1.tasks_created = 4
        w1.save_checkpoint(last_filename="2026-11-09_14-00-00.jpg", last_file_dt=advanced_dt)
        # Cria arquivos fictícios na pasta da câmera
        dummy_img = w1.camera_folder / "2026-11-09_14-00-00.jpg"
        dummy_img.write_bytes(b"dummy")

        # 2. Cria segundo worker para a mesma câmera com a EXATA MESMA data de início
        p2 = {
            "camera_name": cam_name,
            "direct_url": f"http://10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera={cam_name}",
            "capture_mode": "history",
            "start_date": same_date,
            "interval_seconds": 60,
            "auto_start": False,
        }
        resp2 = self.client.post("/acquisition/workers", json=p2)
        self.assertEqual(resp2.status_code, 200)
        w2_id = resp2.json()["id"]

        w2 = acquisition_manager.get_worker(w2_id)
        self.assertIsNotNone(w2)
        # O novo worker deve começar do absoluto zero:
        expected_start_ts = datetime.fromisoformat(same_date).timestamp()
        self.assertEqual(w2.history_cursor, expected_start_ts)
        self.assertEqual(w2.captured_count, 0)
        self.assertEqual(w2.tasks_created, 0)
        self.assertEqual(w2.current_recording_time, "06/11/2026 08:55:00")
        # As imagens antigas do worker anterior foram apagadas
        self.assertFalse(dummy_img.exists())

        # Cleanup
        self.client.delete(f"/acquisition/workers/{w2_id}")

    def test_create_worker_replaces_existing_camera_worker(self):
        """Valida que criar um novo worker para a mesma câmera substitui o worker anterior."""
        p1 = {
            "camera_name": "CAM_REPLACE_TEST",
            "direct_url": "http://10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera=CAM_REPLACE_TEST",
            "interval_seconds": 60,
            "auto_start": False,
        }
        resp1 = self.client.post("/acquisition/workers", json=p1)
        self.assertEqual(resp1.status_code, 200)
        w1_id = resp1.json()["id"]

        p2 = {
            "camera_name": "CAM_REPLACE_TEST",
            "direct_url": "http://10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera=CAM_REPLACE_TEST",
            "interval_seconds": 300,
            "auto_start": False,
        }
        resp2 = self.client.post("/acquisition/workers", json=p2)
        self.assertEqual(resp2.status_code, 200)
        w2_id = resp2.json()["id"]

        self.assertNotEqual(w1_id, w2_id)
        # O worker 1 deve ter sido substituído e removido
        self.assertIsNone(acquisition_manager.get_worker(w1_id))
        self.assertIsNotNone(acquisition_manager.get_worker(w2_id))

        # Cleanup
        self.client.delete(f"/acquisition/workers/{w2_id}")

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

    def test_task_prefix_input_rendered_in_wizard_page(self):
        """Valida que a página do wizard contém os campos de prefixo/nome inicial da task no CVAT."""
        resp = self.client.get("/acquisition/digifort")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("panel-existing-project", html)
        self.assertIn('id="input_task_prefix"', html)
        self.assertIn('id="form_task_prefix"', html)
        self.assertIn("Nome Inicial / Prefixo de Cada Task no CVAT", html)
        self.assertIn("Diferenciar Múltiplos Workers", html)

    def test_create_worker_with_task_prefix(self):
        """Valida a criação de um worker com task_prefix customizado e sua exibição nos cards."""
        payload = {
            "camera_name": "CAM_PREFIX_TEST",
            "direct_url": "http://10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera=CAM_PREFIX_TEST",
            "cvat_project_name": "ARGUS_DEFEITOS",
            "task_prefix": "CAM_PREFIX_TEST_SETOR_A",
            "interval_seconds": 60,
            "images_per_task": 50,
            "auto_start": False,
        }
        resp = self.client.post("/acquisition/workers", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        w_id = data["id"]
        self.assertEqual(data.get("task_prefix"), "CAM_PREFIX_TEST_SETOR_A")

        # Verifica renderização nos cards
        cards_resp = self.client.get("/acquisition/workers/cards")
        self.assertEqual(cards_resp.status_code, 200)
        self.assertIn("CAM_PREFIX_TEST_SETOR_A", cards_resp.text)
        self.assertIn("Prefixo da Task:", cards_resp.text)

        # Cleanup
        self.client.delete(f"/acquisition/workers/{w_id}")

    def test_two_workers_same_project_different_task_prefixes(self):
        """Valida que dois workers coletando no mesmo projeto podem ter câmeras e prefixos de tasks diferentes sem colidir."""
        p1 = {
            "camera_name": "CAM_WORKER_1",
            "direct_url": "http://10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera=CAM_WORKER_1",
            "cvat_project_name": "ARGUS_DEFEITOS",
            "cvat_project_id": 2,
            "task_prefix": "CAM1_NORTE",
            "interval_seconds": 60,
            "images_per_task": 50,
            "auto_start": False,
        }
        r1 = self.client.post("/acquisition/workers", json=p1)
        self.assertEqual(r1.status_code, 200)
        w1_id = r1.json()["id"]

        p2 = {
            "camera_name": "CAM_WORKER_2",
            "direct_url": "http://10.247.72.55:8601/Interface/Cameras/GetSnapshot?Camera=CAM_WORKER_2",
            "cvat_project_name": "ARGUS_DEFEITOS",
            "cvat_project_id": 2,
            "task_prefix": "CAM2_SUL",
            "interval_seconds": 60,
            "images_per_task": 50,
            "auto_start": False,
        }
        r2 = self.client.post("/acquisition/workers", json=p2)
        self.assertEqual(r2.status_code, 200)
        w2_id = r2.json()["id"]

        self.assertNotEqual(w1_id, w2_id)
        w1 = acquisition_manager.get_worker(w1_id)
        w2 = acquisition_manager.get_worker(w2_id)
        self.assertIsNotNone(w1)
        self.assertIsNotNone(w2)
        self.assertEqual(w1.config.task_prefix, "CAM1_NORTE")
        self.assertEqual(w2.config.task_prefix, "CAM2_SUL")
        self.assertEqual(w1.config.cvat_project_name, "ARGUS_DEFEITOS")
        self.assertEqual(w2.config.cvat_project_name, "ARGUS_DEFEITOS")

        # Cleanup
        self.client.delete(f"/acquisition/workers/{w1_id}")
        self.client.delete(f"/acquisition/workers/{w2_id}")


if __name__ == "__main__":
    unittest.main()
