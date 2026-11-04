import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import main
from test_main import images


class AutomaticProjectTests(unittest.TestCase):
    def config(self, folder):
        return dict(IMAGES_DIR=folder, CVAT_PROJECT_ID=None, IMAGES_PER_TASK=2,
                    CVAT_URL="http://cvat.test", CVAT_USERNAME="user", CVAT_PASSWORD="secret",
                    CVAT_ORGANIZATION="VOD", CVAT_LABELS=[], AUTO_REMOVE=False)

    def test_existing_camera_project_is_reused_case_insensitively(self):
        client = MagicMock()
        existing = SimpleNamespace(name="vo2c01", id=10)
        client.projects.list.return_value = [SimpleNamespace(name="VO2C02", id=11), existing]
        self.assertIs(main.resolve_project(client, self.config(Path("VO2C01")), False), existing)
        client.projects.create.assert_not_called()

    def test_missing_project_is_created_without_labels(self):
        client = MagicMock()
        client.projects.list.return_value = []
        result = main.resolve_project(client, self.config(Path("VO2C01")), False)
        self.assertIs(result, client.projects.create.return_value)
        client.projects.create.assert_called_once_with(spec={"name": "VO2C01", "labels": []})

    def test_creation_uses_labels_only_when_configured(self):
        client = MagicMock()
        client.projects.list.return_value = []
        config = self.config(Path("VO2C01")) | dict(CVAT_LABELS=["pessoa", "veiculo"])
        main.resolve_project(client, config, False)
        self.assertEqual(client.projects.create.call_args.kwargs["spec"]["labels"],
                         [{"name": "pessoa"}, {"name": "veiculo"}])

    def test_dry_run_never_creates_missing_project(self):
        client = MagicMock()
        client.projects.list.return_value = []
        self.assertIsNone(main.resolve_project(client, self.config(Path("VO2C01")), True))
        client.projects.create.assert_not_called()

    def test_duplicate_project_names_require_explicit_id(self):
        client = MagicMock()
        client.projects.list.return_value = [SimpleNamespace(name="VO2C01", id=1),
                                              SimpleNamespace(name="VO2C01", id=2)]
        with self.assertRaises(ValueError):
            main.resolve_project(client, self.config(Path("VO2C01")), False)
        client.projects.create.assert_not_called()

    def test_explicit_project_id_still_works(self):
        client = MagicMock()
        config = self.config(Path("VO2C01")) | dict(CVAT_PROJECT_ID=8)
        self.assertIs(main.resolve_project(client, config, False), client.projects.retrieve.return_value)
        client.projects.retrieve.assert_called_once_with(8)
        client.projects.list.assert_not_called()

    def test_organization_is_required_for_automatic_mode(self):
        with self.assertRaises(ValueError):
            main.resolve_project(MagicMock(), self.config(Path("VO2C01")) | dict(CVAT_ORGANIZATION=""), False)

    def test_empty_folder_creates_project_once_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "VO2C01"
            folder.mkdir()
            with patch.object(main, "make_client") as factory:
                client = factory.return_value.__enter__.return_value
                project = SimpleNamespace(name="VO2C01", id=10)
                client.projects.list.side_effect = [[], [project]]
                client.projects.create.return_value = project
                main.run(self.config(folder), False)
                main.run(self.config(folder), False)
                self.assertEqual(client.organization_slug, "VOD")
                client.projects.create.assert_called_once()
                client.tasks.create.assert_not_called()

    def test_dry_run_plans_tasks_for_future_project_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "VO2C01"
            folder.mkdir()
            images(folder, (0, 1))
            with patch.object(main, "make_client") as factory:
                client = factory.return_value.__enter__.return_value
                client.projects.list.return_value = []
                main.run(self.config(folder), True)
                client.projects.create.assert_not_called()
                client.tasks.create.assert_not_called()

    def test_new_tasks_use_id_of_created_project(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "VO2C01"
            folder.mkdir()
            source = images(folder, (0, 1))
            with patch.object(main, "make_client") as factory:
                client = factory.return_value.__enter__.return_value
                client.projects.list.return_value = []
                client.projects.create.return_value = SimpleNamespace(name="VO2C01", id=99, get_tasks=MagicMock(return_value=[]))
                remote = client.tasks.create.return_value
                remote.size = 2
                remote.get_frames_info.return_value = [SimpleNamespace(name=p.name) for _, p in source]
                main.run(self.config(folder), False)
                self.assertEqual(client.organization_slug, "VOD")
                self.assertEqual(client.tasks.create.call_args.kwargs["spec"]["project_id"], 99)

    def test_json_folders_no_longer_need_project_id(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(CVAT_URL="http://cvat.test", CVAT_USERNAME="user", CVAT_PASSWORD="secret",
                       CVAT_ORGANIZATION="VOD", CVAT_LABELS="[]",
                       FOLDERS=json.dumps([dict(images_dir="VO2C01", images_per_task=50)]))
            with patch.object(main, "BASE_DIR", Path(directory)), patch.dict(os.environ, env, clear=True):
                config = main.load_config()["FOLDERS"][0]
            self.assertIsNone(config["CVAT_PROJECT_ID"])
            self.assertEqual(config["CVAT_ORGANIZATION"], "VOD")
            self.assertEqual(config["CVAT_LABELS"], [])

    def test_label_validation_and_folder_override(self):
        for invalid in ("pessoa", [""], ["pessoa", "Pessoa"], [1]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                main.label_names(invalid)
        self.assertEqual(main.label_names([]), [])
        self.assertEqual(main.label_names([" pessoa "]), ["pessoa"])


if __name__ == "__main__":
    unittest.main()
