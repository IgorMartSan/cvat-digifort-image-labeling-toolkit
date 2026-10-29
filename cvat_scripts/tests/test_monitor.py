import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import main
from test_main import images, task


class MonitorTests(unittest.TestCase):
    def test_failure_in_one_folder_does_not_stop_the_next(self):
        first = dict(IMAGES_DIR=Path("VO2C01"), CVAT_PROJECT_ID=8)
        second = dict(IMAGES_DIR=Path("VO2C02"), CVAT_PROJECT_ID=12)
        config = dict(FOLDERS=[first, second], LOOP=False, POLL_INTERVAL_SECONDS=5)
        with patch.object(main, "run", side_effect=[RuntimeError("failed"), None]) as run:
            with self.assertLogs(level="ERROR"), self.assertRaises(ValueError):
                main.monitor(config, True)
            self.assertEqual(run.call_args_list, [call(first, True), call(second, True)])

    def test_two_folders_upload_to_their_own_projects_and_batch_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            first_path, second_path = Path(directory) / "VO2C01", Path(directory) / "VO2C02"
            first_path.mkdir()
            second_path.mkdir()
            first_images = images(first_path, (0, 1))
            second_images = images(second_path, (0, 1, 2))
            first = self.config(first_path) | dict(AUTO_REMOVE=False)
            second = self.config(second_path) | dict(AUTO_REMOVE=False, CVAT_PROJECT_ID=12, IMAGES_PER_TASK=3)
            config = dict(FOLDERS=[first, second], LOOP=False, POLL_INTERVAL_SECONDS=5)
            with patch.object(main, "make_client") as factory:
                client, _ = self.setup_client(factory, first_images)
                client.projects.retrieve.side_effect = [
                    SimpleNamespace(name="Projeto A", id=8, get_tasks=MagicMock(return_value=[])),
                    SimpleNamespace(name="Projeto B", id=12, get_tasks=MagicMock(return_value=[])),
                ]
                tasks = [MagicMock(id=100), MagicMock(id=101)]
                for remote, source in zip(tasks, (first_images, second_images)):
                    remote.size = len(source)
                    remote.get_frames_info.return_value = [SimpleNamespace(name=p.name) for _, p in source]
                client.tasks.create.side_effect = tasks
                main.monitor(config, False)
                self.assertEqual(client.projects.retrieve.call_args_list, [call(8), call(12)])
                self.assertEqual([item.kwargs["spec"]["project_id"] for item in client.tasks.create.call_args_list], [8, 12])
                self.assertEqual([len(remote.upload_data.call_args.kwargs["resources"]) for remote in tasks], [2, 3])
                self.assertTrue(client.tasks.create.call_args_list[0].kwargs["spec"]["name"].startswith("VO2C01 | "))
                self.assertTrue(client.tasks.create.call_args_list[1].kwargs["spec"]["name"].startswith("VO2C02 | "))

    def config(self, folder):
        config = dict(IMAGES_DIR=folder, IMAGES_PER_TASK=2, AUTO_REMOVE=True,
                    LOOP=True, POLL_INTERVAL_SECONDS=5, CVAT_URL="http://cvat.test",
                    CVAT_USERNAME="user", CVAT_PASSWORD="secret", CVAT_PROJECT_ID=8,
                    CVAT_ORGANIZATION="")
        return config | {"FOLDERS": [config.copy()]}

    def setup_client(self, factory, source):
        client = factory.return_value.__enter__.return_value
        project = SimpleNamespace(name="Projeto", id=8, get_tasks=MagicMock(return_value=[]))
        client.projects.retrieve.return_value = project
        remote = client.tasks.create.return_value
        remote.id = 100
        remote.size = len(source)
        remote.get_frames_info.return_value = [SimpleNamespace(name=p.name) for _, p in source]
        return client, remote

    def test_empty_folder_waits_without_connecting(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "make_client") as factory:
            main.run(self.config(Path(directory)), False)
            factory.assert_not_called()

    def test_incomplete_batch_waits_without_creating_task(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = images(folder, (0,))
            with patch.object(main, "make_client") as factory:
                client, _ = self.setup_client(factory, source)
                main.run(self.config(folder), False)
                client.tasks.create.assert_not_called()
                self.assertTrue(source[0][1].exists())

    def test_only_confirmed_batch_is_removed_and_remainder_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = images(folder, (0, 1, 2))
            (folder / ".history.json").write_text("progress")
            (folder / "download.part").write_bytes(b"partial")
            with patch.object(main, "make_client") as factory:
                client, remote = self.setup_client(factory, source[:2])
                remote.upload_data.side_effect = lambda **kwargs: self.assertTrue(all(p.exists() for _, p in source))
                remote.update.side_effect = lambda values: self.assertTrue(all(p.exists() for _, p in source))
                main.run(self.config(folder), False)
                remote.fetch.assert_called_once()
                remote.update.assert_called_once()
                self.assertTrue(client.tasks.create.call_args.kwargs["spec"]["name"].endswith(main.PENDING_SUFFIX))
            self.assertFalse(source[0][1].exists())
            self.assertFalse(source[1][1].exists())
            self.assertTrue(source[2][1].exists())
            self.assertTrue((folder / ".history.json").exists())
            self.assertTrue((folder / "download.part").exists())

    def test_auto_remove_disabled_keeps_confirmed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = images(folder, (0, 1))
            with patch.object(main, "make_client") as factory:
                self.setup_client(factory, source)
                main.run(self.config(folder) | {"AUTO_REMOVE": False}, False)
            self.assertTrue(all(p.exists() for _, p in source))

    def test_wrong_remote_filenames_never_delete_local_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = images(folder, (0, 1))
            with patch.object(main, "make_client") as factory:
                _, remote = self.setup_client(factory, source)
                remote.get_frames_info.return_value = [SimpleNamespace(name="wrong.jpg")] * 2
                with self.assertLogs(level="ERROR"), self.assertRaises(RuntimeError):
                    main.run(self.config(folder), False)
                remote.update.assert_not_called()
            self.assertTrue(all(p.exists() for _, p in source))

    def test_wrong_remote_size_never_deletes_local_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = images(folder, (0, 1))
            with patch.object(main, "make_client") as factory:
                _, remote = self.setup_client(factory, source)
                remote.size = 1
                with self.assertLogs(level="ERROR"), self.assertRaises(RuntimeError):
                    main.run(self.config(folder), False)
            self.assertTrue(all(p.exists() for _, p in source))

    def test_failed_upload_never_deletes_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = images(folder, (0, 1))
            with patch.object(main, "make_client") as factory:
                _, remote = self.setup_client(factory, source)
                remote.upload_data.side_effect = RuntimeError("network failure")
                with self.assertLogs(level="ERROR"), self.assertRaises(RuntimeError):
                    main.run(self.config(folder), False)
                remote.fetch.assert_not_called()
            self.assertTrue(all(p.exists() for _, p in source))

    def test_failed_final_name_update_never_deletes_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = images(folder, (0, 1))
            with patch.object(main, "make_client") as factory:
                _, remote = self.setup_client(factory, source)
                remote.update.side_effect = RuntimeError("rename failed")
                with self.assertLogs(level="ERROR"), self.assertRaises(RuntimeError):
                    main.run(self.config(folder), False)
            self.assertTrue(all(p.exists() for _, p in source))

    def test_file_changed_during_upload_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = images(folder, (0, 1))
            signatures = {p: main.file_signature(p) for _, p in source}
            source[0][1].write_bytes(b"replacement image")
            with self.assertLogs(level="WARNING"):
                main.remove_uploaded(source, signatures, folder)
            self.assertTrue(source[0][1].exists())
            self.assertFalse(source[1][1].exists())

    def test_pending_task_blocks_advance_even_with_nonzero_size(self):
        remote = task()
        remote.name += main.PENDING_SUFFIX
        periods, issues = main.existing_periods([remote], {"VO2C01": []})
        self.assertFalse(periods)
        self.assertTrue(issues)
        remote.get_frames_info.assert_not_called()

    def test_poll_interval_and_error_retry(self):
        config = {"LOOP": True, "POLL_INTERVAL_SECONDS": 5, "FOLDERS": [{"IMAGES_DIR": Path("VO2C01"), "CVAT_PROJECT_ID": 8}]}
        with patch.object(main, "run", side_effect=[RuntimeError("failed"), None]) as run:
            with patch.object(main.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleep:
                with self.assertLogs(level="ERROR"), self.assertRaises(KeyboardInterrupt):
                    main.monitor(config, False)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(sleep.call_args_list, [call(5), call(5)])

    def test_dry_run_is_once_and_never_removes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = images(folder, (0, 1))
            with patch.object(main, "make_client") as factory, patch.object(main.time, "sleep") as sleep:
                client, _ = self.setup_client(factory, source)
                main.monitor(self.config(folder), True)
                sleep.assert_not_called()
                client.tasks.create.assert_not_called()
            self.assertTrue(all(p.exists() for _, p in source))

    def test_new_images_complete_batch_on_next_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = images(folder, (0,))
            with patch.object(main, "make_client") as factory:
                client, remote = self.setup_client(factory, source)
                def next_cycle(seconds):
                    self.assertEqual(seconds, 5)
                    if not client.tasks.create.called:
                        source.extend(images(folder, (1,)))
                        remote.size = 2
                        remote.get_frames_info.return_value = [SimpleNamespace(name=p.name) for _, p in source]
                    else:
                        raise KeyboardInterrupt
                with patch.object(main.time, "sleep", side_effect=next_cycle):
                    with self.assertRaises(KeyboardInterrupt):
                        main.monitor(self.config(folder), False)
                client.tasks.create.assert_called_once()
            self.assertFalse(list(folder.glob("*.jpg")))


if __name__ == "__main__":
    unittest.main()
