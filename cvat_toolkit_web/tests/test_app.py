import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app


class TestCvatToolkitWebApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.core.config import MODELS_DIR
        cls.test_run_dir = MODELS_DIR / "detection" / "run_001"
        training_dir = cls.test_run_dir / "training"
        training_dir.mkdir(parents=True, exist_ok=True)
        (cls.test_run_dir / "best.pt").write_bytes(b"dummy weights")
        (cls.test_run_dir / "metadata.json").write_text(
            '{"model_id": "detection/run_001", "framework": "yolo11", "mode": "detection", "run_name": "run_001", "task_ids": [1], "best_map50": 0.85}',
            encoding="utf-8",
        )
        (training_dir / "results.png").write_bytes(b"dummy png")
        (training_dir / "args.yaml").write_text("epochs: 10\nbatch: 16\n", encoding="utf-8")
        (training_dir / "results.csv").write_text("epoch,train/loss\n1,0.5\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        import shutil
        from app.core.config import MODELS_DIR
        test_mode_dir = MODELS_DIR / "detection"
        if test_mode_dir.exists():
            shutil.rmtree(test_mode_dir, ignore_errors=True)

    def setUp(self):
        self.client = TestClient(app)

    def test_root_redirect(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/training")

    @patch("app.modules.training.router.list_projects")
    def test_training_page_renders(self, mock_list_projects):
        mock_list_projects.return_value = [{"id": 1, "name": "Projeto Teste"}]
        response = self.client.get("/training")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Treinamento de Modelos", response.text)
        self.assertIn("Projeto Teste", response.text)
        self.assertIn("step-nav-5", response.text)

    @patch("app.modules.prediction.router.list_projects")
    def test_prediction_page_renders(self, mock_projects):
        mock_projects.return_value = [{"id": 2, "name": "Projeto Destino"}]
        response = self.client.get("/prediction")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Auto-Anotação", response.text)
        self.assertIn("wizard-stepper", response.text)
        self.assertIn("step-nav-1", response.text)
        self.assertIn("step-nav-2", response.text)
        self.assertIn("step-nav-3", response.text)
        self.assertIn("step-nav-4", response.text)
        self.assertIn("model-select", response.text)
        self.assertIn("dest-project-select", response.text)
        self.assertIn("Projeto Destino", response.text)

    def test_prediction_model_info_endpoint(self):
        # detection/run_001 existe no disco de testes
        response = self.client.get("/prediction/model-info?model_id=detection/run_001")
        self.assertEqual(response.status_code, 200)
        self.assertIn("selected-model-details-card", response.text)
        self.assertIn("detection/run_001", response.text)
        self.assertIn("YOLO11", response.text)
        self.assertIn("Inspecionar Pasta & Gráficos YOLO", response.text)

    @patch("app.modules.prediction.router.get_project_tasks_summary")
    def test_prediction_tasks_endpoint_defaults_unselected(self, mock_tasks):
        mock_tasks.return_value = [
            {"id": 64, "name": "Task VO2C05", "completed": False, "state": "new", "images_count": 100},
            {"id": 74, "name": "Task VO2C05 Finished", "completed": True, "state": "completed", "images_count": 100},
        ]
        response = self.client.get("/prediction/tasks?project_id=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Marcar as Tarefas para Anotação", response.text)
        self.assertIn("Task VO2C05", response.text)
        # Nenhuma tag input checkbox deve vir com atributo 'checked' por padrão
        import re
        self.assertIsNone(re.search(r'<input[^>]*\bchecked\b', response.text))
        # Contador inicial padrão deve ser 0
        self.assertIn('id="selected-dest-tasks-count">\n                0', response.text)

    @patch("app.main.check_cvat_connection")
    def test_health_check(self, mock_check):
        mock_check.return_value = {"connected": True, "host": "http://localhost:8080"}
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")
        self.assertTrue(data["cvat"]["connected"])

    @patch("app.modules.training.router.get_project_tasks_summary")
    def test_training_tasks_endpoint(self, mock_tasks):
        mock_tasks.return_value = [
            {"id": 10, "name": "Task 1", "completed": True, "state": "completed", "images_count": 50}
        ]
        response = self.client.get("/training/tasks?project_id=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Marcar as Tarefas do Projeto", response.text)
        self.assertIn("Task 1", response.text)
        self.assertIn("step-2-card", response.text)

    @patch("app.modules.training.router.analyze_dataset_service")
    @patch("app.modules.training.router.list_available_models")
    def test_training_analyze_endpoint(self, mock_models, mock_analyze):
        mock_analyze.return_value = {
            "total_images": 100,
            "annotated_images": 80,
            "empty_images": 20,
            "rectangles": 150,
            "polygons": 0,
            "labels": {"car": 150},
            "incompatible": 0,
            "detection_compatible_frames": 80,
            "segmentation_compatible_frames": 0,
        }
        mock_models.return_value = []
        response = self.client.post("/training/analyze", data={"project_id": 1, "task_ids": [10]})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Escolha do Framework", response.text)
        self.assertIn("Configuração do Framework", response.text)
        self.assertIn("Iniciar Treinamento", response.text)
        self.assertIn("Conjunto de Imagens", response.text)
        self.assertIn("Apenas Imagens Marcadas / Anotadas", response.text)
        self.assertIn("Todas as Imagens da(s) Tarefa(s)", response.text)
        self.assertIn("YOLO11 Nano", response.text)
        self.assertIn("YOLO11 Medium", response.text)
        self.assertIn("step-3-card", response.text)
        self.assertIn("step-4-card", response.text)
        self.assertIn("step-5-card", response.text)

    def test_jobs_page_renders(self):
        response = self.client.get("/jobs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Fila de Tarefas & Histórico", response.text)
        self.assertIn("Em Execução", response.text)
        self.assertIn("Fila de Espera", response.text)
        self.assertIn("Limpar Logs", response.text)

    def test_jobs_snapshot_api(self):
        response = self.client.get("/jobs/snapshot")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("counts", data)
        self.assertIn("queued_jobs", data)
        self.assertIn("history", data)
        self.assertIn("tasks", data)
        self.assertIn("total", data["counts"])
        self.assertIn("waiting", data["counts"])
        self.assertIn("success", data["counts"])
        self.assertIn("error", data["counts"])

    def test_job_status_labels_and_descriptions(self):
        from app.core.job_queue import Job

        # Teste de job com sucesso
        job_success = Job(
            id="job_1",
            job_type="training",
            title="Treino 1",
            status="completed",
            created_at="2026-11-05T10:00:00+00:00",
            payload={"project_id": 5, "task_ids": [10, 11], "model_name": "yolo11n", "epochs": 50, "batch": 16, "image_size": 640},
        )
        self.assertEqual(job_success.status_label, "Finalizado com sucesso")
        self.assertEqual(job_success.status_category, "success")
        self.assertIn("Treinamento yolo11n", job_success.description)
        self.assertIn("Projeto #5", job_success.description)

        # Teste de job com erro
        job_failed = Job(
            id="job_2",
            job_type="prediction",
            title="Predição 1",
            status="failed",
            created_at="2026-11-05T10:05:00+00:00",
            error="CUDA out of memory",
        )
        self.assertEqual(job_failed.status_label, "Finalizado com erro")
        self.assertEqual(job_failed.status_category, "error")

        # Teste de job em espera
        job_queued = Job(
            id="job_3",
            job_type="training",
            title="Treino 2",
            status="queued",
            created_at="2026-11-05T10:10:00+00:00",
        )
        self.assertEqual(job_queued.status_label, "Em espera")
        self.assertEqual(job_queued.status_category, "waiting")

    def test_job_tasks_by_arrival_ordering(self):
        from app.core.job_queue import JobQueueManager, Job

        mgr = JobQueueManager.__new__(JobQueueManager)
        import threading
        mgr._lock = threading.RLock()
        mgr._queued_jobs = [
            Job(id="job_c", job_type="training", title="Job C", status="queued", created_at="2026-11-05T12:00:00+00:00"),
        ]
        mgr._active_job = Job(id="job_b", job_type="training", title="Job B", status="running", created_at="2026-11-05T11:00:00+00:00")
        mgr._history = [
            Job(id="job_a", job_type="training", title="Job A", status="completed", created_at="2026-11-05T10:00:00+00:00"),
        ]

        tasks = mgr.get_tasks_by_arrival()
        self.assertEqual(len(tasks), 3)
        # Organizado do mais novo para o mais velho (espera -> executando -> finalizado)
        self.assertEqual(tasks[0]["id"], "job_c")
        self.assertEqual(tasks[0]["arrival_order"], 1)
        self.assertEqual(tasks[0]["status_label"], "Em espera")

        self.assertEqual(tasks[1]["id"], "job_b")
        self.assertEqual(tasks[1]["arrival_order"], 2)
        self.assertIn("Executando", tasks[1]["status_label"])

        self.assertEqual(tasks[2]["id"], "job_a")
        self.assertEqual(tasks[2]["arrival_order"], 3)
        self.assertEqual(tasks[2]["status_label"], "Finalizado com sucesso")

    def test_trained_models_page_renders(self):
        response = self.client.get("/training/models")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Modelos Treinados", response.text)
        self.assertIn("Framework:", response.text)
        self.assertIn("framework-select", response.text)
        self.assertIn("mode-select", response.text)

    def test_model_details_modal_endpoint(self):
        # detection/run_001 existe no disco de testes
        response = self.client.get("/training/models/detection/run_001/details")
        self.assertEqual(response.status_code, 200)
        self.assertIn("modelDetailModal", response.text)
        self.assertIn("results.png", response.text)
        self.assertIn("args.yaml", response.text)
        self.assertIn("results.csv", response.text)

    def test_model_details_json_endpoint(self):
        response = self.client.get("/training/models/detection/run_001/json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "detection")
        self.assertEqual(data["run_name"], "run_001")
        self.assertIn("plots", data)
        self.assertIn("folder_files", data)
        self.assertIn("args", data)

    def test_cleanup_temp_endpoint(self):
        response = self.client.post("/training/cleanup-temp")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("cleaned_count", data)

    @patch("app.modules.training.router.delete_trained_model_service")
    def test_delete_model_endpoint(self, mock_delete):
        mock_delete.return_value = True
        response = self.client.delete("/training/models/detection/run_999")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        mock_delete.assert_called_once_with("detection", "run_999")

    def test_clear_logs_endpoint(self):
        response = self.client.delete("/jobs/logs")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("Logs limpos", data["message"])

    def test_circular_log_buffer(self):
        from app.core.config import MAX_LOG_ENTRIES
        from app.core.job_queue import Job, job_queue

        test_job = Job(
            id="job_buffer_test",
            job_type="training",
            title="Buffer Test",
            status="running",
            created_at="2026-11-05T10:00:00+00:00",
        )
        with job_queue._lock:
            job_queue._active_job = test_job

        # Adiciona mais logs que o limite MAX_LOG_ENTRIES
        for i in range(MAX_LOG_ENTRIES + 15):
            job_queue.append_log("job_buffer_test", f"Log line {i}")

        self.assertLessEqual(len(test_job.logs), MAX_LOG_ENTRIES)
        self.assertEqual(test_job.logs[-1], f"Log line {MAX_LOG_ENTRIES + 14}")

        # Limpeza
        with job_queue._lock:
            job_queue._active_job = None

    def test_system_resources_html_endpoint(self):
        response = self.client.get("/api/system/resources")
        self.assertEqual(response.status_code, 200)
        self.assertIn("system-telemetry-widget", response.text)
        self.assertIn("CPU", response.text)
        self.assertIn("RAM", response.text)
        self.assertIn("GPU", response.text)
        self.assertIn("CVAT Conectado", response.text)

    def test_system_resources_json_endpoint(self):
        response = self.client.get("/api/system/resources?format=json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("cpu", data)
        self.assertIn("ram", data)
        self.assertIn("gpu", data)
        self.assertIn("vram", data)
        self.assertIn("percent", data["cpu"])
        self.assertIn("percent", data["ram"])
        self.assertIn("available", data["gpu"])
        self.assertIn("available", data["vram"])



if __name__ == "__main__":
    unittest.main()
