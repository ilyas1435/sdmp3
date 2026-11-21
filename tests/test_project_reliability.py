import os
import tempfile
import unittest
from unittest import mock

import project
import sdmp3_cli


class PlanTransferTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tempdir.name, 'source')
        self.dest = os.path.join(self.tempdir.name, 'destination')
        os.makedirs(self.root)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_skips_symlinks_without_leaving_the_selected_tree(self):
        external = os.path.join(self.tempdir.name, 'external')
        os.makedirs(external)
        with open(os.path.join(self.root, 'ordinary.mp3'), 'wb') as fh:
            fh.write(b'abc')
        with open(os.path.join(external, 'outside.mp3'), 'wb') as fh:
            fh.write(b'not part of the selection')

        external_file_link = os.path.join(self.root, 'linked-file.mp3')
        external_dir_link = os.path.join(self.root, 'linked-folder')
        cycle_link = os.path.join(self.root, 'cycle')
        root_link = os.path.join(self.tempdir.name, 'source-link')
        try:
            os.symlink(os.path.join(external, 'outside.mp3'), external_file_link)
            os.symlink(external, external_dir_link)
            os.symlink(self.root, cycle_link)
            os.symlink(self.root, root_link)
        except (NotImplementedError, OSError) as exc:
            self.skipTest('symlinks are not available: {}'.format(exc))

        tasks, total_files, total_bytes, skipped = project.plan_transfer(
            [self.root, root_link], self.dest)

        copied_sources = [src for src, _dst, kind in tasks if kind == 'file']
        self.assertEqual(copied_sources, [os.path.join(self.root, 'ordinary.mp3')])
        self.assertEqual(total_files, 1)
        self.assertEqual(total_bytes, 3)
        skipped_paths = {path for path, _reason in skipped}
        self.assertTrue({external_file_link, external_dir_link, cycle_link, root_link}
                        .issubset(skipped_paths))

    def test_keeps_natural_depth_first_copy_order(self):
        for path in (
            os.path.join(self.root, 'Disc 10'),
            os.path.join(self.root, 'Disc 2'),
        ):
            os.makedirs(path)
            with open(os.path.join(path, '01.mp3'), 'wb') as fh:
                fh.write(b'x')
        for name in ('track 10.mp3', 'track 2.mp3'):
            with open(os.path.join(self.root, name), 'wb') as fh:
                fh.write(b'x')

        tasks, _total_files, _total_bytes, _skipped = project.plan_transfer(
            [self.root], self.dest)

        relative_order = [os.path.relpath(src, self.root) for src, _dst, _kind in tasks]
        self.assertEqual(relative_order, [
            '.',
            'Disc 2',
            os.path.join('Disc 2', '01.mp3'),
            'Disc 10',
            os.path.join('Disc 10', '01.mp3'),
            'track 2.mp3',
            'track 10.mp3',
        ])


class SearchRankingTests(unittest.TestCase):
    def test_cached_search_returns_only_the_best_forty_matches(self):
        root = tempfile.mkdtemp(prefix='sdmp3-search-test-')
        items = [
            {'name': 'Track {:03d}.mp3'.format(i),
             'name_lower': 'track {:03d}.mp3'.format(i).lower(),
             'path': os.path.join(root, 'Track {:03d}.mp3'.format(i)),
             'is_dir': False}
            for i in range(250)
        ]
        with project.folder_cache_lock:
            project.FOLDER_CACHE[root] = {
                'items': items, 'count': len(items), 'built_at': 0,
            }
        try:
            project.app.testing = True
            response = project.app.test_client().get(
                '/api/search',
                query_string={'q': 'track', 'root': root, 'folders_only': 'false'})
            body = response.get_json()
        finally:
            with project.folder_cache_lock:
                project.FOLDER_CACHE.pop(root, None)
            project.app.testing = False
            os.rmdir(root)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(body['from_cache'])
        self.assertEqual(len(body['results']), project.SEARCH_MAX_RESULTS)
        self.assertEqual(
            [item['name'] for item in body['results']],
            ['Track {:03d}.mp3'.format(i) for i in range(project.SEARCH_MAX_RESULTS)])


class CliProcessIdentityTests(unittest.TestCase):
    def test_stale_pid_file_does_not_match_a_reused_pid(self):
        with tempfile.TemporaryDirectory() as tempdir:
            proc_root = os.path.join(tempdir, 'proc')
            state_file = os.path.join(tempdir, 'instance.json')
            pid_dir = os.path.join(proc_root, '12345')
            os.makedirs(pid_dir)
            with open(state_file, 'w', encoding='utf-8') as fh:
                fh.write('{"pid":12345,"port":5050,"host":"0.0.0.0"}')
            with open(os.path.join(pid_dir, 'cmdline'), 'wb') as fh:
                fh.write(b'/usr/bin/python\0/other/program.py\0')

            with mock.patch.object(sdmp3_cli, 'PROC_ROOT', proc_root):
                with mock.patch.object(sdmp3_cli, 'STATE_FILE', state_file):
                    with mock.patch.object(sdmp3_cli, 'APP_PY', '/opt/sdmp3/project.py'):
                        with mock.patch.object(sdmp3_cli, 'pid_alive', return_value=True):
                            self.assertIsNone(sdmp3_cli.read_instance())

    def test_live_sdmp3_pid_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as tempdir:
            proc_root = os.path.join(tempdir, 'proc')
            state_file = os.path.join(tempdir, 'instance.json')
            pid_dir = os.path.join(proc_root, '54321')
            os.makedirs(pid_dir)
            with open(state_file, 'w', encoding='utf-8') as fh:
                fh.write('{"pid":54321,"port":5050,"host":"0.0.0.0"}')
            with open(os.path.join(pid_dir, 'cmdline'), 'wb') as fh:
                fh.write(b'/usr/bin/python\0/opt/sdmp3/project.py\0')

            with mock.patch.object(sdmp3_cli, 'PROC_ROOT', proc_root):
                with mock.patch.object(sdmp3_cli, 'STATE_FILE', state_file):
                    with mock.patch.object(sdmp3_cli, 'APP_PY', '/opt/sdmp3/project.py'):
                        with mock.patch.object(sdmp3_cli, 'pid_alive', return_value=True):
                            instance = sdmp3_cli.read_instance()

        self.assertEqual(instance['pid'], 54321)


if __name__ == '__main__':
    unittest.main()
