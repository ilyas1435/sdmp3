import os
import tempfile
import unittest
from unittest import mock

import project
import sdmp3_state


class SavedOperationApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_patch = mock.patch.object(sdmp3_state, 'STATE_DIR', self.tempdir.name)
        self.state_patch.start()
        self.client = project.app.test_client()

    def tearDown(self):
        self.state_patch.stop()
        self.tempdir.cleanup()

    def payload(self, name='Weekend music'):
        return {
            'name': name,
            'paths': [
                {'path': '/sdcard/Music/Album', 'is_dir': True},
                {'path': '/sdcard/Music/track.mp3', 'is_dir': False},
            ],
            'excludes': ['/sdcard/Music/Album/skip.mp3'],
            'groups': [
                {'name': 'Audio/Albums', 'paths': ['/sdcard/Music/Album']},
                {'name': 'Loose tracks', 'paths': ['/sdcard/Music/track.mp3']},
                {'name': 'Empty folder', 'paths': []},
            ],
            # The server must never retain a destination, even if a client sends it.
            'dest': '/sdcard/Backup',
        }

    def test_create_read_update_and_delete_round_trip_without_destination(self):
        response = self.client.post('/api/saved_operations', json=self.payload())
        self.assertEqual(response.status_code, 201)
        operation = response.get_json()['operation']
        operation_id = operation['id']
        self.assertEqual(operation['name'], 'Weekend music')
        self.assertNotIn('dest', operation)

        detail = self.client.get('/api/saved_operations/' + operation_id)
        self.assertEqual(detail.status_code, 200)
        loaded = detail.get_json()['operation']
        self.assertEqual(
            [item['path'] for item in loaded['paths']],
            ['/sdcard/Music/Album', '/sdcard/Music/track.mp3'],
        )
        self.assertEqual(loaded['excludes'], ['/sdcard/Music/Album/skip.mp3'])
        self.assertEqual(loaded['groups'][-1], {'name': 'Empty folder', 'paths': []})
        self.assertNotIn('dest', loaded)

        listing = self.client.get('/api/saved_operations').get_json()['operations']
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['item_count'], 2)
        self.assertNotIn('paths', listing[0])

        changed = self.payload('Weekend music edited')
        changed['groups'] = [{'name': 'Audio/Albums', 'paths': ['/sdcard/Music/Album']}]
        updated = self.client.put(
            '/api/saved_operations/' + operation_id, json=changed
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()['operation']['name'], 'Weekend music edited')
        self.assertEqual(len(updated.get_json()['operation']['groups']), 1)

        deleted = self.client.delete('/api/saved_operations/' + operation_id)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get('/api/saved_operations').get_json()['operations'], [])
        self.assertEqual(
            self.client.get('/api/saved_operations/' + operation_id).status_code, 404
        )

    def test_rename_only_and_duplicate_names_are_handled(self):
        first = self.client.post('/api/saved_operations', json=self.payload()).get_json()['operation']
        second_payload = self.payload('Other operation')
        second = self.client.post('/api/saved_operations', json=second_payload)
        self.assertEqual(second.status_code, 201)
        renamed = self.client.put(
            '/api/saved_operations/' + first['id'], json={'name': 'Renamed'}
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.get_json()['operation']['name'], 'Renamed')

        duplicate = self.client.put(
            '/api/saved_operations/' + first['id'], json={'name': 'Other operation'}
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_rejects_invalid_paths_groups_and_empty_operations(self):
        empty = self.client.post('/api/saved_operations', json={
            'name': 'Empty', 'paths': [], 'excludes': [], 'groups': [],
        })
        self.assertEqual(empty.status_code, 400)

        invalid_group = self.payload()
        invalid_group['groups'] = [{'name': '../outside', 'paths': []}]
        self.assertEqual(
            self.client.post('/api/saved_operations', json=invalid_group).status_code,
            400,
        )

        nested_roots = self.payload()
        nested_roots['paths'] = [
            {'path': '/sdcard/Music', 'is_dir': True},
            {'path': '/sdcard/Music/Album', 'is_dir': True},
        ]
        self.assertEqual(
            self.client.post('/api/saved_operations', json=nested_roots).status_code,
            400,
        )

        outside_exclusion = self.payload()
        outside_exclusion['excludes'] = ['/sdcard/private.mp3']
        self.assertEqual(
            self.client.post('/api/saved_operations', json=outside_exclusion).status_code,
            400,
        )

    def test_persisted_store_is_atomic_json_and_survives_reload(self):
        response = self.client.post('/api/saved_operations', json=self.payload())
        self.assertEqual(response.status_code, 201)
        saved_path = sdmp3_state.state_file(sdmp3_state.SAVED_OPERATIONS_FILE)
        self.assertTrue(os.path.isfile(saved_path))
        stored = sdmp3_state.load_saved_operations()
        self.assertEqual(len(stored), 1)
        self.assertNotIn('dest', stored[0])

    def test_saved_operations_controls_and_no_auto_copy_are_rendered(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        for marker in (
            'id="saveOperationBtn"',
            'id="savedOperationsModal"',
            'id="updateSavedOperationBtn"',
            'applySavedOperation(operation, sizeData, unavailablePaths, sizeWarning)',
            'اختر الوجهة يدوياً عند النسخ',
            'id="groupStatusHint" class="check-hint" role="status"',
        ):
            self.assertIn(marker, page)

    def test_loading_saved_operation_has_visible_busy_and_completion_states(self):
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('aria-live="polite" aria-atomic="true" aria-busy="false"', page)
        self.assertIn('.settings-save-state.is-loading::before', page)
        self.assertIn('data-saved-operation-load', page)
        self.assertIn('جارٍ تحميل العملية المحفوظة...', page)
        self.assertIn('جارٍ فحص الملفات والمجلدات المحفوظة...', page)

        list_function = page.split(
            'async function loadSavedOperationsList() {', 1
        )[1].split('function openSaveOperationDialog', 1)[0]
        self.assertLess(
            list_function.index("if (list) list.innerHTML = '';"),
            list_function.index("await savedOperationsRequest('/api/saved_operations')"),
        )

        load_function = page.split(
            'async function loadSavedOperation(operationId, activeButton) {', 1
        )[1].split('function applySavedOperation', 1)[0]
        self.assertIn('if (savedOperationLoadingId) return;', load_function)
        self.assertIn("setSavedOperationsStatus(error.message || 'تعذر تحميل العملية المحفوظة', 'err')",
                      load_function)
        self.assertIn('finally {', load_function)
        self.assertIn('setSavedOperationLoadState(false)', load_function)
        self.assertIn('showConfirmModal(message, proceed)', load_function)
        self.assertIn("savedOperationsRequest('/api/path_status'", load_function)
        self.assertIn("savedOperationsRequest('/api/sizes'", load_function)
        self.assertIn('catch (sizeError)', load_function)
        self.assertIn('تعذر حساب الأحجام الآن، لكن تم تحميل العناصر المتاحة', load_function)
        self.assertIn('if (applySavedOperation(operation, sizeData, unavailablePaths, sizeWarning))',
                      load_function)

        apply_function = page.split(
            'function applySavedOperation(operation, sizeData, unavailablePaths, sizeWarning) {', 1
        )[1].split('function closeSelectedModal', 1)[0]
        self.assertIn('return true;', apply_function)
        self.assertNotIn('closeSavedOperationsModal()', apply_function)

    def test_unavailable_paths_are_skipped_with_names_and_counts(self):
        page = self.client.get('/').get_data(as_text=True)
        load_function = page.split(
            'async function loadSavedOperation(operationId, activeButton) {', 1
        )[1].split('function applySavedOperation', 1)[0]
        self.assertIn('let unavailableItems = operation.paths.filter', load_function)
        self.assertIn('if (different)', load_function)
        self.assertNotIn('if (unavailableItems.length || different)', load_function)
        self.assertIn('} else {\n                proceed();', load_function)

        apply_function = page.split(
            'function applySavedOperation(operation, sizeData, unavailablePaths, sizeWarning) {', 1
        )[1].split('function closeSelectedModal', 1)[0]
        self.assertIn('return !missingSet.has(item.path);', apply_function)
        self.assertIn('savedOperationIssueMessage(unavailableItems)', apply_function)
        self.assertIn("setGroupStatus(issueMessage, 'warning')", apply_function)

        issue_function = page.split(
            'function savedOperationIssueMessage(items) {', 1
        )[1].split('async function loadSavedOperation', 1)[0]
        self.assertIn('items.length', issue_function)
        self.assertIn('folders +', issue_function)
        self.assertIn('files +', issue_function)
        self.assertIn('items.map(function(item)', issue_function)
        self.assertIn('savedOperationPathLabel(item.path)', issue_function)
        self.assertIn('تعذّر الوصول', issue_function)
        self.assertIn('حُذف أو نُقل', issue_function)
        self.assertIn('max-height:140px; overflow-y:auto;', page)

    def test_size_lookup_can_validate_saved_path_types(self):
        with tempfile.TemporaryDirectory() as source:
            directory = os.path.join(source, 'folder')
            file_path = os.path.join(source, 'track.mp3')
            os.mkdir(directory)
            with open(file_path, 'wb') as output:
                output.write(b'audio')
            result = self.client.post('/api/sizes', json={
                'paths': [directory, file_path],
                'excludes': [],
                'include_types': True,
            })
        self.assertEqual(result.status_code, 200)
        data = result.get_json()
        self.assertEqual(data['types'][directory], 'directory')
        self.assertEqual(data['types'][file_path], 'file')
        self.assertEqual(data['missing'], [])

    def test_path_status_reports_existing_types_and_missing_paths(self):
        with tempfile.TemporaryDirectory() as source:
            directory = os.path.join(source, 'folder')
            file_path = os.path.join(source, 'track.mp3')
            missing_file = os.path.join(source, 'removed.mp3')
            os.mkdir(directory)
            with open(file_path, 'wb') as output:
                output.write(b'audio')

            response = self.client.post('/api/path_status', json={
                'paths': [
                    {'path': directory, 'is_dir': True},
                    {'path': file_path, 'is_dir': False},
                    {'path': missing_file, 'is_dir': False},
                ],
            })

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['missing'], [missing_file])
        self.assertEqual(data['types'][directory], 'directory')
        self.assertEqual(data['types'][file_path], 'file')
        self.assertEqual(data['types'][missing_file], 'missing')

    def test_path_status_handles_reported_missing_download_path(self):
        missing_file = '/sdcard/Download/intro8.mp3'
        real_stat = os.stat

        def stat_with_missing_saved_file(path, *args, **kwargs):
            if os.fspath(path) == missing_file:
                raise FileNotFoundError(2, 'No such file or directory', missing_file)
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(project.os, 'stat', side_effect=stat_with_missing_saved_file):
            response = self.client.post('/api/path_status', json={
                'paths': [{'path': missing_file, 'is_dir': False}],
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['missing'], [missing_file])
        self.assertEqual(response.get_json()['types'][missing_file], 'missing')

    def test_size_lookup_reports_missing_saved_paths_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as source:
            missing_file = os.path.join(source, 'removed.mp3')
            result = self.client.post('/api/sizes', json={
                'paths': [missing_file],
                'excludes': [],
                'include_types': True,
            })
        self.assertEqual(result.status_code, 200)
        data = result.get_json()
        self.assertEqual(data['missing'], [missing_file])
        self.assertEqual(data['types'][missing_file], 'missing')

    def test_reported_missing_download_path_never_returns_internal_error(self):
        missing_file = '/sdcard/Download/intro8.mp3'
        real_stat = os.stat

        def stat_with_missing_saved_file(path, *args, **kwargs):
            if os.fspath(path) == missing_file:
                raise FileNotFoundError(2, 'No such file or directory', missing_file)
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(project.os, 'stat', side_effect=stat_with_missing_saved_file):
            response = self.client.post('/api/sizes', json={
                'paths': [missing_file], 'excludes': [], 'include_types': True,
            })
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['missing'], [missing_file])
        self.assertEqual(data['types'][missing_file], 'missing')


if __name__ == '__main__':
    unittest.main()
