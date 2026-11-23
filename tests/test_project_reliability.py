import os
import tempfile
import errno
import ctypes
import threading
import time
import unittest
from unittest import mock

import project
import sdmp3_cli
import sdmp3_groups
from sdmp3_version import validate_version


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


class CopyWorkerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tempdir.name, 'source')
        self.dest = os.path.join(self.tempdir.name, 'destination')
        os.makedirs(self.root)
        os.makedirs(self.dest)
        project.reset_state()

    def tearDown(self):
        project.reset_state()
        self.tempdir.cleanup()

    def write_source(self, name, data):
        path = os.path.join(self.root, name)
        with open(path, 'wb') as fh:
            fh.write(data)
        return path

    def destination_path(self, name):
        return os.path.join(self.dest, os.path.basename(self.root), name)

    def run_worker(self, verify=False, verify_content=False):
        with mock.patch.object(project, 'has_enough_space', return_value=True), \
                mock.patch.object(project, 'acquire_wakelock'), \
                mock.patch.object(project, 'release_wakelock'), \
                mock.patch.object(project, 'invalidate_cache_for_path'), \
                mock.patch.object(project, 'flush_destination', return_value=''):
            project.copy_thread_task([self.root], self.dest, verify=verify,
                                     verify_content=verify_content)
        return project.get_state_snapshot()

    def test_cancel_during_a_file_removes_partial_mp3_and_finishes_cancelled(self):
        self.write_source('track.mp3', b'a' * (project.SENDFILE_CHUNK * 3))
        original_copy = project.copy_file_sequential

        def cancel_after_first_chunk(src, dst, cancel_check=None, on_bytes=None):
            def report_bytes(count):
                if on_bytes is not None:
                    on_bytes(count)
                project.set_state(cancel=True)
            return original_copy(src, dst, cancel_check=cancel_check,
                                 on_bytes=report_bytes)

        with mock.patch.object(project, 'copy_file_sequential',
                               side_effect=cancel_after_first_chunk):
            snapshot = self.run_worker()

        self.assertEqual(snapshot['status'], 'cancelled')
        self.assertFalse(os.path.exists(self.destination_path('track.mp3')))

    def test_flush_error_after_cancellation_finishes_with_an_error_status(self):
        self.write_source('track.mp3', b'a' * (project.SENDFILE_CHUNK * 3))
        original_copy = project.copy_file_sequential

        def cancel_after_first_chunk(src, dst, cancel_check=None, on_bytes=None):
            def report_bytes(count):
                if on_bytes is not None:
                    on_bytes(count)
                project.set_state(cancel=True)
            return original_copy(src, dst, cancel_check=cancel_check,
                                 on_bytes=report_bytes)

        with mock.patch.object(project, 'copy_file_sequential',
                               side_effect=cancel_after_first_chunk), \
                mock.patch.object(project, 'sync_with_heartbeat',
                                  side_effect=OSError(errno.EIO, 'simulated flush failure')):
            snapshot = self.run_worker()

        self.assertEqual(snapshot['status'], 'error')
        self.assertEqual(snapshot['phase'], 'error')
        self.assertIn('تم إلغاء النسخ', snapshot['file'])
        self.assertIn('تعذر تثبيت البيانات', snapshot['file'])
        self.assertFalse(os.path.exists(self.destination_path('track.mp3')))

    def test_buffered_copy_completes_short_writes_without_changing_bytes(self):
        payload = b'playback-order-preserved'
        src = self.write_source('track.mp3', payload)
        dst = os.path.join(self.tempdir.name, 'short-write.mp3')
        real_write = os.write
        writes = []

        def short_write(fd, data):
            count = real_write(fd, data[:3])
            writes.append(count)
            return count

        with mock.patch.object(project.os, 'sendfile',
                               side_effect=OSError(errno.EINVAL, 'unsupported')), \
                mock.patch.object(project.os, 'write', side_effect=short_write):
            written = project.copy_file_sequential(src, dst)

        with open(dst, 'rb') as fh:
            copied = fh.read()
        self.assertEqual(written, len(payload))
        self.assertEqual(copied, payload)
        self.assertGreater(len(writes), 1)
        self.assertTrue(all(count > 0 for count in writes))

    def test_write_error_after_a_short_write_removes_partial_file_and_reports_failure(self):
        self.write_source('track.mp3', b'not-a-complete-copy')
        real_write = os.write
        calls = []

        def short_then_fail(fd, data):
            calls.append(None)
            if len(calls) == 1:
                return real_write(fd, data[:4])
            raise OSError(errno.EIO, 'simulated card write failure')

        with mock.patch.object(project.os, 'sendfile',
                               side_effect=OSError(errno.EINVAL, 'unsupported')), \
                mock.patch.object(project.os, 'write', side_effect=short_then_fail):
            snapshot = self.run_worker()

        self.assertEqual(snapshot['status'], 'done')
        self.assertEqual(snapshot['failed'], 1)
        self.assertTrue(snapshot['errors'])
        self.assertFalse(os.path.exists(self.destination_path('track.mp3')))

    def test_final_flush_error_finishes_in_error_instead_of_staying_copying(self):
        self.write_source('track.mp3', b'complete file')
        with mock.patch.object(project, 'sync_with_heartbeat',
                               side_effect=OSError(errno.EIO, 'simulated flush failure')):
            snapshot = self.run_worker()

        self.assertEqual(snapshot['status'], 'error')
        self.assertEqual(snapshot['phase'], 'error')
        self.assertIn('flush failure', snapshot['log'])
        self.assertIn('تعذر إكمال النسخ', snapshot['file'])

    def test_periodic_flush_targets_only_the_copy_destination(self):
        self.write_source('track.mp3', b'flush me')
        calls = []

        def record_sync(phase_after='copying', dest=None):
            calls.append((phase_after, dest))

        with mock.patch.object(project, 'SYNC_THRESHOLD', 1), \
                mock.patch.object(project, 'sync_with_heartbeat',
                                  side_effect=record_sync):
            snapshot = self.run_worker()

        self.assertEqual(snapshot['status'], 'done')
        self.assertGreaterEqual(len(calls), 2)  # periodic checkpoint and final flush
        self.assertTrue(all(dest == self.dest for _phase, dest in calls))

    def test_verification_size_mismatch_is_reported_in_the_terminal_result(self):
        self.write_source('track.mp3', b'expected complete content')
        original_verify = project.verify_copied_files

        def alter_before_verify(planned, roots, **kwargs):
            destination = next(iter(planned))
            with open(destination, 'wb') as fh:
                fh.write(b'x')
            return original_verify(planned, roots, **kwargs)

        with mock.patch.object(project, 'verify_copied_files',
                               side_effect=alter_before_verify):
            snapshot = self.run_worker(verify=True)

        self.assertEqual(snapshot['status'], 'done')
        self.assertEqual(snapshot['verified'], 0)
        self.assertEqual(snapshot['verify_total'], 1)
        self.assertEqual(len(snapshot['verify_failed']), 1)
        self.assertIn('التحقق وجد 1 ملفاً مخالفاً', snapshot['file'])

    def test_content_verification_detects_same_size_corruption(self):
        self.write_source('track.mp3', b'original')
        original_copy = project.copy_file_sequential

        def corrupt_after_copy(src, dst, cancel_check=None, on_bytes=None):
            written = original_copy(src, dst, cancel_check=cancel_check,
                                    on_bytes=on_bytes)
            with open(dst, 'wb') as fh:
                fh.write(b'corrupt!')
            return written

        with mock.patch.object(project, 'copy_file_sequential',
                               side_effect=corrupt_after_copy):
            # Content mode must enable verification even if fast-size mode is off.
            snapshot = self.run_worker(verify=False, verify_content=True)

        self.assertEqual(snapshot['status'], 'done')
        self.assertEqual(snapshot['verify_mode'], 'content')
        self.assertTrue(snapshot['verify_content'])
        self.assertEqual(snapshot['verify_total'], 1)
        self.assertEqual(snapshot['verified'], 0)
        self.assertTrue(any('SHA-256' in failure for failure in snapshot['verify_failed']))
        self.assertIn('مقارنة محتوى الملفات وجدت 1 ملفاً مخالفاً', snapshot['file'])

    def test_content_verification_accepts_matching_bytes(self):
        src = self.write_source('track.mp3', b'identical content')
        dst = os.path.join(self.tempdir.name, 'copy.mp3')
        with open(dst, 'wb') as fh:
            fh.write(b'identical content')

        verified, failures, cancelled = project.verify_copied_files(
            {dst: os.path.getsize(src)}, [dst],
            source_by_dest={dst: src}, verify_content=True)

        self.assertEqual((verified, failures, cancelled), (1, [], False))

    def test_content_verification_can_be_cancelled_during_readback(self):
        src = self.write_source('track.mp3', b'content')
        dst = os.path.join(self.tempdir.name, 'copy.mp3')
        with open(dst, 'wb') as fh:
            fh.write(b'content')
        checks = [0]

        def cancel_after_readback_starts():
            checks[0] += 1
            return checks[0] >= 3

        verified, failures, cancelled = project.verify_copied_files(
            {dst: 7}, [dst], cancel_check=cancel_after_readback_starts,
            source_by_dest={dst: src}, verify_content=True)

        self.assertEqual(verified, 0)
        self.assertEqual(failures, [])
        self.assertTrue(cancelled)

    def test_flush_fallback_warning_is_kept_in_transfer_result(self):
        self.write_source('track.mp3', b'completed')
        warning = 'تعذر تثبيت الوجهة مباشرة: خطأ إدخال/إخراج'
        with mock.patch.object(project, 'sync_with_heartbeat', return_value=warning):
            snapshot = self.run_worker()

        self.assertEqual(snapshot['status'], 'done')
        self.assertIn(warning, snapshot['flush_warning'])
        self.assertIn(warning, snapshot['file'])

    def test_syncfs_failure_is_reported_after_global_fallback(self):
        with tempfile.TemporaryDirectory() as dest:
            def failed_syncfs(_fd):
                ctypes.set_errno(errno.EIO)
                return -1

            with mock.patch.object(project, '_syncfs_fn', return_value=failed_syncfs), \
                    mock.patch.object(project.os, 'sync') as global_sync:
                warning = project.flush_destination(dest)

        self.assertIn('تعذر تثبيت الوجهة مباشرة', warning)
        self.assertIn('تثبيت بيانات الكارت', warning)
        global_sync.assert_called_once_with()

    def test_unsupported_syncfs_uses_fallback_without_false_warning(self):
        with tempfile.TemporaryDirectory() as dest:
            calls = []

            def unsupported_syncfs(_fd):
                calls.append(None)
                ctypes.set_errno(errno.EINVAL)
                return -1

            with mock.patch.object(project, '_SYNCFS_UNSUPPORTED_DEVICES', set()), \
                    mock.patch.object(project, '_syncfs_fn',
                                      return_value=unsupported_syncfs), \
                    mock.patch.object(project.os, 'sync') as global_sync:
                first_warning = project.flush_destination(dest)
                second_warning = project.flush_destination(dest)

        self.assertEqual(first_warning, '')
        self.assertEqual(second_warning, '')
        self.assertEqual(len(calls), 1)  # Unsupported capability is cached per filesystem.
        self.assertEqual(global_sync.call_count, 2)

    def test_syncfs_permission_restriction_uses_fallback_without_false_warning(self):
        with tempfile.TemporaryDirectory() as dest:
            def restricted_syncfs(_fd):
                ctypes.set_errno(errno.EPERM)
                return -1

            with mock.patch.object(project, '_SYNCFS_UNSUPPORTED_DEVICES', set()), \
                    mock.patch.object(project, '_syncfs_fn',
                                      return_value=restricted_syncfs), \
                    mock.patch.object(project.os, 'sync') as global_sync:
                warning = project.flush_destination(dest)

        self.assertEqual(warning, '')
        global_sync.assert_called_once_with()

    def test_syncfs_enoent_uses_fallback_without_warning(self):
        with tempfile.TemporaryDirectory() as dest:
            calls = []

            def syncfs_reports_enoent(_fd):
                calls.append(None)
                ctypes.set_errno(errno.ENOENT)
                return -1

            with mock.patch.object(project, '_SYNCFS_UNSUPPORTED_DEVICES', set()), \
                    mock.patch.object(project, '_syncfs_fn',
                                      return_value=syncfs_reports_enoent), \
                    mock.patch.object(project.os, 'sync') as fallback:
                first_warning = project.flush_destination(dest)
                second_warning = project.flush_destination(dest)

        self.assertEqual(first_warning, '')
        self.assertEqual(second_warning, '')
        self.assertEqual(len(calls), 1)
        self.assertEqual(fallback.call_count, 2)

    def test_missing_destination_open_is_not_mistaken_for_syncfs_enoent(self):
        missing_destination = '/definitely-not-an-sdmp3-destination'
        with mock.patch.object(project, '_syncfs_fn'), \
                mock.patch.object(project.os, 'open',
                                  side_effect=FileNotFoundError(errno.ENOENT, 'missing')), \
                mock.patch.object(project.os, 'sync') as fallback:
            warning = project.flush_destination(missing_destination)
        self.assertIn('open; errno=2', warning)
        self.assertIn('غير موجود', warning)
        fallback.assert_called_once_with()

    def test_global_flush_failure_raises_a_clear_error(self):
        with mock.patch.object(project, '_syncfs_fn', return_value=None), \
                mock.patch.object(project.os, 'sync',
                                  side_effect=OSError(errno.EIO, 'simulated flush failure')):
            with self.assertRaisesRegex(RuntimeError, 'تعذر تثبيت البيانات على الوجهة'):
                project.flush_destination('/unused')

    def test_android_directory_access_restrictions_do_not_report_bad_card(self):
        for code in (errno.EACCES, errno.EPERM):
            with self.subTest(errno=code), \
                    mock.patch.object(project, '_syncfs_fn') as syncfs_factory, \
                    mock.patch.object(project.os, 'open',
                                      side_effect=OSError(code, 'directory restricted')), \
                    mock.patch.object(project.os, 'sync') as fallback:
                self.assertEqual(project.flush_destination('/storage/card'), '')
                fallback.assert_called_once_with()
                syncfs_factory.return_value.assert_not_called()

    def test_syncfs_access_denied_uses_global_fallback(self):
        with tempfile.TemporaryDirectory() as dest:
            def denied(_fd):
                ctypes.set_errno(errno.EACCES)
                return -1

            with mock.patch.object(project, '_SYNCFS_UNSUPPORTED_DEVICES', set()), \
                    mock.patch.object(project, '_syncfs_fn', return_value=denied), \
                    mock.patch.object(project.os, 'sync') as fallback:
                self.assertEqual(project.flush_destination(dest), '')
                fallback.assert_called_once_with()

    def test_restricted_directory_does_not_hide_global_flush_failure(self):
        with mock.patch.object(project, '_syncfs_fn'), \
                mock.patch.object(project.os, 'open',
                                  side_effect=PermissionError(errno.EACCES, 'denied')), \
                mock.patch.object(project.os, 'sync',
                                  side_effect=OSError(errno.EIO, 'writeback failed')):
            with self.assertRaisesRegex(RuntimeError, 'تعذر تثبيت البيانات'):
                project.flush_destination('/storage/card')

    def test_real_directory_io_error_is_not_hidden_by_fallback(self):
        with mock.patch.object(project, '_syncfs_fn'), \
                mock.patch.object(project.os, 'open',
                                  side_effect=OSError(errno.EIO, 'card I/O error')), \
                mock.patch.object(project.os, 'sync') as fallback:
            warning = project.flush_destination('/storage/card')
        self.assertIn('خطأ إدخال/إخراج', warning)
        self.assertIn('open; errno=5', warning)
        fallback.assert_called_once_with()

    def test_zero_byte_write_fails_instead_of_looping_forever(self):
        source = self.write_source('track.mp3', b'payload')
        destination = self.destination_path('track.mp3')
        os.makedirs(os.path.dirname(destination))
        with mock.patch.object(project.os, 'sendfile',
                               side_effect=OSError(errno.EINVAL, 'unsupported')), \
                mock.patch.object(project.os, 'write', return_value=0):
            with self.assertRaisesRegex(OSError, 'made no progress'):
                project.copy_file_sequential(source, destination)
        self.assertFalse(os.path.exists(destination))

    def test_fast_verification_only_checks_copied_paths_without_reading_content(self):
        destination = os.path.join(self.dest, 'track.mp3')
        with open(destination, 'wb') as fh:
            fh.write(b'abc')
        with mock.patch.object(project.os, 'scandir',
                               side_effect=AssertionError('must not scan destination')), \
                mock.patch('builtins.open',
                           side_effect=AssertionError('must not read file content')):
            result = project.verify_copied_files({destination: 3}, [self.dest])
        self.assertEqual(result, (1, [], False))

    def test_verification_reports_missing_and_nonregular_files(self):
        missing = os.path.join(self.dest, 'missing.mp3')
        result = project.verify_copied_files(
            {missing: 3, self.root: 0}, [self.dest])
        self.assertEqual(result[0], 0)
        self.assertEqual(len(result[1]), 2)
        self.assertFalse(result[2])

    def test_worker_copies_naturally_ordered_tracks_one_at_a_time(self):
        expected = ['track 2.mp3', 'track 10.mp3', 'track 100.mp3']
        for index, name in enumerate(reversed(expected)):
            self.write_source(name, ('track-{}'.format(index)).encode())

        original_copy = project.copy_file_sequential
        copied_sources = []
        active = 0
        max_active = 0
        guard = threading.Lock()

        def track_copy(src, dst, cancel_check=None, on_bytes=None):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            try:
                copied_sources.append(os.path.basename(src))
                time.sleep(0.01)
                return original_copy(src, dst, cancel_check=cancel_check,
                                     on_bytes=on_bytes)
            finally:
                with guard:
                    active -= 1

        with mock.patch.object(project, 'copy_file_sequential', side_effect=track_copy):
            snapshot = self.run_worker(verify=True)

        self.assertEqual(snapshot['status'], 'done')
        self.assertEqual(copied_sources, expected)
        self.assertEqual(max_active, 1)
        for name in expected:
            with open(self.destination_path(name), 'rb') as fh:
                self.assertTrue(fh.read().startswith(b'track-'))


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


class TransferRouteTests(unittest.TestCase):
    def test_main_page_loads_nested_group_and_bulk_assignment_controls(self):
        client = project.app.test_client()
        page = client.get('/')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('/static/sdmp3_group_ops.js', html)
        self.assertIn('v{}'.format(project.get_app_version()), html)
        self.assertIn('groupParentModal', html)
        self.assertIn('data-bulk-act', html)
        self.assertIn('bulk-item-check', html)
        self.assertIn('window.SDMP3GroupOps.leafName(name)', html)
        self.assertIn('إخراج إلى المستوى الرئيسي', html)
        self.assertIn('data-root-drop="1"', html)
        self.assertIn('اسحب المجلد هنا لإخراجه إلى المستوى الرئيسي', html)
        self.assertIn("spriteIcon(open ? 'ico-chevron-down' : 'ico-chevron-right')", html)
        self.assertIn('aria-expanded=', html)
        self.assertIn('selectedBrowseExpanded', html)
        self.assertIn('renderSelectedBrowseBranch', html)
        move_function = html.split('function movePathsToGroup(paths, gid) {', 1)[1].split(
            'function movePathToGroup', 1)[0]
        self.assertIn('bulkSelectedPaths.clear()', move_function)
        self.assertNotIn('selectedPaths.clear()', move_function)
        helper = client.get('/static/sdmp3_group_ops.js')
        self.assertEqual(helper.status_code, 200)
        helper.close()


    def test_selected_folder_browse_api_returns_children_for_exclusions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            folder = os.path.join(tempdir, 'selected')
            child_folder = os.path.join(folder, 'album')
            os.makedirs(child_folder)
            with open(os.path.join(folder, 'track.mp3'), 'wb') as output:
                output.write(b'audio')
            with open(os.path.join(child_folder, 'inside.mp3'), 'wb') as output:
                output.write(b'audio')

            response = project.app.test_client().get(
                '/api/items', query_string={'path': folder, 'sort': 'name'})

        self.assertEqual(response.status_code, 200)
        items = response.get_json()['items']
        self.assertEqual({item['name'] for item in items}, {'album', 'track.mp3'})
        self.assertEqual(
            {item['name']: item['is_dir'] for item in items},
            {'album': True, 'track.mp3': False},
        )

    def test_transfer_route_preserves_nested_copy_group_paths(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = os.path.join(tempdir, 'source')
            destination = os.path.join(tempdir, 'destination')
            os.makedirs(source)
            os.makedirs(destination)
            project.reset_state()
            try:
                with mock.patch.object(project, 'check_destination', return_value=None), \
                        mock.patch.object(project.threading, 'Thread') as thread_factory, \
                        mock.patch.object(project, 'ACTIVE_THREAD', None):
                    response = project.app.test_client().post('/transfer/start', json={
                        'paths': [source],
                        'dest': destination,
                        'groups': [{'name': 'المادة/الدرس', 'paths': [source]}],
                        'verify': False,
                    })

                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()['success'])
                args = thread_factory.call_args.kwargs['args']
                self.assertEqual(args[3], [('المادة/الدرس', [source])])
            finally:
                project.reset_state()

    def test_content_verification_flag_reaches_worker_and_enables_verification(self):
        with tempfile.TemporaryDirectory() as tempdir:
            source = os.path.join(tempdir, 'source')
            destination = os.path.join(tempdir, 'destination')
            os.makedirs(source)
            os.makedirs(destination)
            project.reset_state()
            try:
                with mock.patch.object(project, 'check_destination', return_value=None), \
                        mock.patch.object(project.threading, 'Thread') as thread_factory, \
                        mock.patch.object(project, 'ACTIVE_THREAD', None):
                    response = project.app.test_client().post('/transfer/start', json={
                        'paths': [source],
                        'dest': destination,
                        'verify': False,
                        'verify_content': True,
                    })

                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()['success'])
                args = thread_factory.call_args.kwargs['args']
                self.assertEqual(args[-2:], (True, True))
                snapshot = project.get_state_snapshot()
                self.assertEqual(snapshot['verify_mode'], 'content')
                self.assertTrue(snapshot['verify_content'])
            finally:
                project.reset_state()


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


class AppVersionTests(unittest.TestCase):
    def test_version_is_major_minor_and_matches_app_and_cli(self):
        version_path = os.path.join(os.path.dirname(project.__file__), 'VERSION')
        with open(version_path, encoding='utf-8') as handle:
            file_version = handle.read().strip()

        self.assertEqual(validate_version(file_version), file_version)
        self.assertEqual(project.get_app_version(), file_version)
        self.assertEqual(sdmp3_cli.app_version(), file_version)

        client = project.app.test_client()
        page = client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('v{}'.format(file_version), page.get_data(as_text=True))
        system = client.get('/api/system')
        self.assertEqual(system.status_code, 200)
        self.assertEqual(system.get_json()['version'], file_version)

    def test_version_validator_rejects_three_part_and_malformed_values(self):
        for invalid in ('3', '3.0.1', '3.1.0', 'v3.0', '3.x', '', ' 3.0.1 '):
            with self.subTest(version=invalid):
                with self.assertRaises(ValueError):
                    validate_version(invalid)

    def test_app_and_cli_reject_invalid_version_files(self):
        with mock.patch('builtins.open', mock.mock_open(read_data='3.0.1')):
            with self.assertRaisesRegex(ValueError, 'major.minor'):
                project._read_app_version()
        with mock.patch('builtins.open', mock.mock_open(read_data='3.0.1')):
            with self.assertRaisesRegex(ValueError, 'major.minor'):
                sdmp3_cli.app_version()

    def test_running_app_version_does_not_change_when_version_file_is_replaced(self):
        running_version = project.get_app_version()
        with mock.patch('builtins.open', mock.mock_open(read_data='99.99')):
            self.assertEqual(project.get_app_version(), running_version)


class CopyGroupValidationTests(unittest.TestCase):
    def test_non_string_group_name_is_a_validation_error_not_an_exception(self):
        groups, error = sdmp3_groups.prepare_copy_groups(
            [{'name': 123, 'paths': ['/sdcard/a.mp3']}], ['/sdcard/a.mp3'])
        self.assertEqual(groups, [])
        self.assertTrue(error)

    def test_valid_group_still_prepares(self):
        groups, error = sdmp3_groups.prepare_copy_groups(
            [{'name': 'دروس', 'paths': ['/sdcard/a.mp3']}], ['/sdcard/a.mp3'])
        self.assertEqual(error, None)
        self.assertEqual(groups, [('دروس', ['/sdcard/a.mp3'])])


if __name__ == '__main__':
    unittest.main()
