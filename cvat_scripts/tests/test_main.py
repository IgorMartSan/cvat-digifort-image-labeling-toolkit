import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import main

START = datetime(2026, 10, 29, 8)


def images(folder, minutes):
    result = []
    for minute in minutes:
        stamp = START + timedelta(minutes=minute)
        path = folder / (stamp.strftime("%Y-%m-%d_%H-%M-%S") + ".jpg")
        path.write_bytes(b"image")
        result.append((stamp, path))
    return result


def task(camera="VO2C01", start=0, end=1, id=1):
    name = f"{camera} | {(START + timedelta(minutes=start)).isoformat()} | {(START + timedelta(minutes=end)).isoformat()}"
    return SimpleNamespace(id=id, name=name, size=2, get_frames_info=MagicMock())


class PlanningTests(unittest.TestCase):
    def test_accepts_new_and_legacy_timestamps(self):
        for name in ("2026-10-29_08-00-00.jpg", "2026_10_29_08_00_00.png"):
            self.assertEqual(main.image_date(name), START)
        self.assertEqual(main.image_date("2026-10-29_08-00-00_123456.jpg"), START.replace(microsecond=123456))
        self.assertEqual(main.image_date("2026_10_29_08_00_00_123456.png"), START.replace(microsecond=123456))
        self.assertIsNone(main.image_date("image.jpg"))

    def test_batches_have_limit_and_chronological_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            source = images(Path(directory), range(5))
            batches, skipped = main.plan_batches({"VO2C01": source}, [], 2)
            self.assertEqual([len(batch) for _, batch in batches], [2, 2])
            self.assertEqual(skipped, 0)
            self.assertEqual(main.task_period(main.task_name(*batches[0])), ("VO2C01", START, START + timedelta(minutes=1)))

    def test_resumes_after_latest_task_end_even_with_older_unsent_images(self):
        with tempfile.TemporaryDirectory() as directory:
            source = images(Path(directory), range(6))
            periods = [dict(camera="vo2c01", start=START + timedelta(minutes=1),
                            end=START + timedelta(minutes=3), id=7)]
            with self.assertLogs(level="INFO"):
                batches, skipped = main.plan_batches({"VO2C01": source}, periods, 3)
            self.assertEqual(skipped, 4)
            self.assertEqual(batches, [])

    def test_older_gap_is_not_backfilled_after_latest_task(self):
        with tempfile.TemporaryDirectory() as directory:
            source = images(Path(directory), (0, 5))
            periods = [dict(camera="vo2c01", start=START + timedelta(minutes=1),
                            end=START + timedelta(minutes=3), id=7)]
            batches, skipped = main.plan_batches({"VO2C01": source}, periods, 2)
            self.assertEqual(skipped, 1)
            self.assertEqual(batches, [])

    def test_other_cameras_do_not_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            source = images(Path(directory), (0, 1))
            periods = [dict(camera="vo2c02", start=START, end=START + timedelta(minutes=1), id=7)]
            batches, skipped = main.plan_batches({"VO2C01": source}, periods, 2)
            self.assertEqual(skipped, 0)
            self.assertEqual(len(batches), 1)

    def test_latest_end_is_selected_from_all_tasks_of_this_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            source = images(Path(directory), range(6))
            periods = [dict(camera="vo2c01", start=START, end=START + timedelta(minutes=3), id=7),
                       dict(camera="vo2c01", start=START, end=START + timedelta(minutes=1), id=6),
                       dict(camera="vo2c02", start=START, end=START + timedelta(minutes=20), id=8)]
            batches, skipped = main.plan_batches({"VO2C01": source}, periods, 2)
            self.assertEqual(skipped, 4)
            self.assertEqual([path for _, batch in batches for _, path in batch], [source[4][1], source[5][1]])

    def test_same_timestamp_group_is_not_split_between_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            source = images(Path(directory), (0, 1))
            duplicate = source[1][1].with_suffix(".png")
            duplicate.write_bytes(b"png")
            source.append((source[1][0], duplicate))
            batches, _ = main.plan_batches({"cam": source}, [], 3)
            self.assertEqual([len(batch) for _, batch in batches], [3])
            with self.assertRaises(ValueError):
                main.plan_batches({"cam": source}, [], 2)
            with self.assertRaises(ValueError):
                main.plan_batches({"cam": source}, [], 1)

    def test_camera_is_always_the_selected_folder_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("cam1", "cam2"):
                folder = root / name
                folder.mkdir()
                images(folder, (0, 1))
            self.assertEqual(set(main.scan_cameras(root / "cam1")), {"cam1"})
            self.assertEqual(set(main.scan_cameras(root / "cam2")), {"cam2"})
            with self.assertRaises(ValueError):
                main.scan_cameras(root)

    def test_scan_ignores_partial_empty_and_invalid_images(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            images(folder, (0,))
            (folder / "2026-10-29_08-01-00.jpg").touch()
            (folder / "invalid.jpg").write_bytes(b"image")
            (folder / "2026-10-29_08-02-00.part").write_bytes(b"partial")
            with self.assertLogs(level="WARNING"):
                cameras = main.scan_cameras(folder)
            self.assertEqual(len(cameras[folder.name]), 1)


class ExistingTaskTests(unittest.TestCase):
    def test_standard_name_detects_period_without_loading_all_frames(self):
        remote = task()
        periods, issues = main.existing_periods([remote], {})
        self.assertFalse(issues)
        self.assertEqual(periods[0]["camera"], "vo2c01")
        remote.get_frames_info.assert_not_called()

    def test_legacy_task_identifies_camera_and_dates_from_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            source = images(Path(directory), (0, 1))
            remote = SimpleNamespace(id=2, name="VO2C01 Camera Capture", get_frames_info=lambda: [
                SimpleNamespace(name=p.name) for _, p in source])
            periods, issues = main.existing_periods([remote], {"VO2C01": source})
            self.assertFalse(issues)
            self.assertEqual(periods[0]["start"], START)
            self.assertEqual(periods[0]["end"], START + timedelta(minutes=1))

    def test_legacy_camera_can_come_from_frame_parent_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            source = images(Path(directory), (0,))
            remote = SimpleNamespace(id=2, name="Camera Capture", get_frames_info=lambda: [
                SimpleNamespace(name="VO2C01/" + source[0][1].name)])
            periods, issues = main.existing_periods([remote], {"VO2C01": source})
            self.assertFalse(issues)
            self.assertEqual(periods[0]["camera"], "vo2c01")

    def test_ambiguous_camera_with_overlapping_dates_requires_review(self):
        with tempfile.TemporaryDirectory() as directory:
            source = images(Path(directory), (0, 1))
            remote = SimpleNamespace(id=23, name="Camera Capture", get_frames_info=lambda: [
                SimpleNamespace(name=p.name) for _, p in source])
            periods, issues = main.existing_periods([remote], {"VO2C01": source})
            self.assertFalse(periods)
            self.assertIn("Task 23", issues[0])

    def test_non_overlapping_unknown_camera_does_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            source = images(Path(directory), (0,))
            remote = SimpleNamespace(id=23, name="Camera Capture", get_frames_info=lambda: [
                SimpleNamespace(name="2026-10-28_08-00-00.jpg")])
            self.assertEqual(main.existing_periods([remote], {"VO2C01": source}), ([], []))

    def test_unknown_image_dates_require_review(self):
        remote = SimpleNamespace(id=23, name="Camera Capture", get_frames_info=lambda: [
            SimpleNamespace(name="image.jpg")])
        periods, issues = main.existing_periods([remote], {})
        self.assertFalse(periods)
        self.assertTrue(issues)


class UploadTests(unittest.TestCase):
    def config(self, directory):
        return dict(IMAGES_DIR=directory, IMAGES_PER_TASK=2,
                    CVAT_URL="http://cvat.test", CVAT_USERNAME="user", CVAT_PASSWORD="secret",
                    CVAT_PROJECT_ID=8, CVAT_ORGANIZATION="org")

    def client(self, factory):
        client = factory.return_value.__enter__.return_value
        client.projects.retrieve.return_value = SimpleNamespace(name="Projeto", id=8, get_tasks=MagicMock(return_value=[]))
        return client

    def test_dry_run_does_not_create_or_upload_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "VO2C01"
            folder.mkdir()
            images(folder, range(3))
            with patch.object(main, "make_client") as factory:
                client = self.client(factory)
                main.run(self.config(folder), True)
                client.tasks.create.assert_not_called()
                client.projects.retrieve.return_value.get_tasks.assert_called_once_with()

    def test_send_uses_project_limits_and_upload_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "VO2C01"
            folder.mkdir()
            source = images(folder, range(3))
            with patch.object(main, "make_client") as factory:
                client = self.client(factory)
                first, second = MagicMock(id=100), MagicMock(id=101)
                first.size = 2
                first.get_frames_info.return_value = [SimpleNamespace(name=p.name) for _, p in source[:2]]
                client.tasks.create.side_effect = [first, second]
                main.run(self.config(folder), False)
                self.assertEqual(client.organization_slug, "org")
                self.assertEqual(client.tasks.create.call_count, 1)
                self.assertEqual(client.tasks.create.call_args_list[0].kwargs["spec"]["project_id"], 8)
                self.assertTrue(client.tasks.create.call_args_list[0].kwargs["spec"]["name"].startswith("VO2C01 | "))
                self.assertEqual(len(first.upload_data.call_args.kwargs["resources"]), 2)
                second.upload_data.assert_not_called()
                first.update.assert_called_once_with({"name": main.task_name("VO2C01", source[:2])})
                self.assertEqual(first.upload_data.call_args.kwargs["params"],
                                 dict(image_quality=100, sorting_method="predefined"))
                self.assertTrue(all(p.exists() for _, p in source))

    def test_every_image_already_sent_creates_no_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "VO2C01"
            folder.mkdir()
            images(folder, (0, 1))
            with patch.object(main, "make_client") as factory:
                client = self.client(factory)
                client.projects.retrieve.return_value.get_tasks.return_value = [task()]
                main.run(self.config(folder), False)
                client.tasks.create.assert_not_called()

    def test_empty_remote_task_requires_review_before_advancing(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "VO2C01"
            folder.mkdir()
            images(folder, (0, 1, 2))
            remote = task()
            remote.size = 0
            with patch.object(main, "make_client") as factory:
                client = self.client(factory)
                client.projects.retrieve.return_value.get_tasks.return_value = [remote]
                with self.assertLogs(level="ERROR"), self.assertRaises(ValueError):
                    main.run(self.config(folder), False)
                client.tasks.create.assert_not_called()

    def test_new_later_task_changes_resume_point_and_blocks_old_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "VO2C01"
            folder.mkdir()
            images(folder, (0, 1))
            with patch.object(main, "make_client") as factory:
                client = self.client(factory)
                client.projects.retrieve.return_value.get_tasks.side_effect = [[], [task(start=4, end=5)]]
                with self.assertRaises(ValueError):
                    main.run(self.config(folder), False)
                client.tasks.create.assert_not_called()

    def test_new_server_overlap_blocks_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "VO2C01"
            folder.mkdir()
            images(folder, (0, 1))
            with patch.object(main, "make_client") as factory:
                client = self.client(factory)
                client.projects.retrieve.return_value.get_tasks.side_effect = [[], [task()]]
                with self.assertRaises(ValueError):
                    main.run(self.config(folder), False)
                client.tasks.create.assert_not_called()

    def test_ambiguous_remote_task_blocks_send(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "VO2C01"
            folder.mkdir()
            source = images(folder, (0, 1))
            remote = SimpleNamespace(id=23, name="Camera Capture", get_frames_info=lambda: [
                SimpleNamespace(name=p.name) for _, p in source])
            with patch.object(main, "make_client") as factory:
                client = self.client(factory)
                client.projects.retrieve.return_value.get_tasks.return_value = [remote]
                with self.assertLogs(level="ERROR"), self.assertRaises(ValueError):
                    main.run(self.config(folder), False)
                client.tasks.create.assert_not_called()

    def test_failed_upload_preserves_local_images_and_reports_task_id(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "VO2C01"
            folder.mkdir()
            source = images(folder, (0, 1))
            with patch.object(main, "make_client") as factory:
                client = self.client(factory)
                remote = client.tasks.create.return_value
                remote.id = 100
                remote.upload_data.side_effect = RuntimeError("upload interrupted")
                with self.assertLogs(level="ERROR") as logs, self.assertRaises(RuntimeError):
                    main.run(self.config(folder), False)
                self.assertIn("100", " ".join(logs.output))
                self.assertTrue(all(p.exists() for _, p in source))


class ConfigTests(unittest.TestCase):
    def env(self, folders):
        return dict(CVAT_URL="http://cvat.test", CVAT_USERNAME="user", CVAT_PASSWORD="secret",
                    FOLDERS=json.dumps(folders), LOOP="true", POLL_INTERVAL_SECONDS="5")

    def test_multiple_folders_have_independent_projects_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            folders = [dict(images_dir="images/VO2C01", project_id=8, images_per_task=50, auto_remove=True),
                       dict(images_dir="images/VO2C02", project_id=12, images_per_task=100, organization="team")]
            with patch.object(main, "BASE_DIR", Path(directory)), patch.dict(os.environ, self.env(folders), clear=True):
                config = main.load_config()
            first, second = config["FOLDERS"]
            self.assertEqual(first["IMAGES_DIR"], Path(directory) / "images/VO2C01")
            self.assertEqual((first["CVAT_PROJECT_ID"], first["IMAGES_PER_TASK"]), (8, 50))
            self.assertEqual((second["CVAT_PROJECT_ID"], second["IMAGES_PER_TASK"]), (12, 100))
            self.assertTrue(first["AUTO_REMOVE"])
            self.assertFalse(second["AUTO_REMOVE"])
            self.assertEqual(second["CVAT_ORGANIZATION"], "team")
            self.assertTrue(all(folder["IMAGES_DIR"].is_dir() for folder in config["FOLDERS"]))
            self.assertTrue(config["DRY_RUN"])
            self.assertEqual(config["POLL_INTERVAL_SECONDS"], 5)

    def test_folder_defaults_inherit_global_options(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.env([dict(images_dir="VO2C01", project_id=8)]) | dict(AUTO_REMOVE="true", CVAT_ORGANIZATION="team")
            with patch.object(main, "BASE_DIR", Path(directory)), patch.dict(os.environ, env, clear=True):
                folder = main.load_config()["FOLDERS"][0]
            self.assertTrue(folder["AUTO_REMOVE"])
            self.assertEqual(folder["CVAT_ORGANIZATION"], "team")
            self.assertEqual(folder["IMAGES_PER_TASK"], 50)

    def test_same_camera_name_is_allowed_in_different_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            folders = [dict(images_dir="a/VO2C01", project_id=8), dict(images_dir="b/VO2C01", project_id=12)]
            with patch.object(main, "BASE_DIR", Path(directory)), patch.dict(os.environ, self.env(folders), clear=True):
                self.assertEqual(len(main.load_config()["FOLDERS"]), 2)

    def test_invalid_json_list_or_folder_fields_are_rejected(self):
        valid = dict(images_dir="VO2C01", project_id=8)
        cases = [[], {}, ["folder"], [{}], *[[valid | change] for change in (
            {"images_dir": ""}, {"images_dir": 1}, {"project_id": 0}, {"images_per_task": 0},
            {"auto_remove": "false"}, {"organization": None}, {"images_dir": "invalid name"})]]
        with tempfile.TemporaryDirectory() as directory:
            for folders in cases:
                with self.subTest(folders=folders), patch.object(main, "BASE_DIR", Path(directory)):
                    with patch.dict(os.environ, self.env(folders), clear=True):
                        with self.assertRaises(ValueError):
                            main.load_config()

    def test_duplicate_path_is_rejected_even_for_different_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            folders = [dict(images_dir="VO2C01", project_id=8), dict(images_dir="VO2C01", project_id=12)]
            with patch.object(main, "BASE_DIR", Path(directory)), patch.dict(os.environ, self.env(folders), clear=True):
                with self.assertRaises(ValueError):
                    main.load_config()

    def test_duplicate_camera_in_same_project_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            folders = [dict(images_dir="a/VO2C01", project_id=8), dict(images_dir="b/vo2c01", project_id=8)]
            with patch.object(main, "BASE_DIR", Path(directory)), patch.dict(os.environ, self.env(folders), clear=True):
                with self.assertRaises(ValueError):
                    main.load_config()

    def test_monitor_options_reject_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self.env([dict(images_dir="VO2C01", project_id=8)])
            for invalid in ({"AUTO_REMOVE": "yes"}, {"LOOP": "1"}, {"POLL_INTERVAL_SECONDS": "0"}):
                with self.subTest(invalid=invalid), patch.object(main, "BASE_DIR", Path(directory)):
                    with patch.dict(os.environ, env | invalid, clear=True):
                        with self.assertRaises(ValueError):
                            main.load_config()


if __name__ == "__main__":
    unittest.main()
