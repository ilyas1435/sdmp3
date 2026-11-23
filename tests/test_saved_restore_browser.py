"""Exercise the actual rendered page, real HTTP routes, and selection state."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from werkzeug.serving import make_server

import project
import sdmp3_state


@unittest.skipUnless(
    shutil.which('node') and os.path.isfile('/repl/tools/bin/chromium'),
    'Full-page test requires Node.js and Chromium',
)
class SavedRestoreBrowserTests(unittest.TestCase):
    def test_restores_partial_and_all_missing_operations_in_real_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(sdmp3_state, 'STATE_DIR', temporary):
                good = os.path.join(temporary, 'track2.mp3')
                removed = os.path.join(temporary, 'removed.mp3')
                folder = os.path.join(temporary, 'album')
                os.mkdir(folder)
                for path in (good, removed):
                    Path(path).write_bytes(b'audio')
                skip = os.path.join(folder, 'skip.mp3')
                Path(skip).write_bytes(b'exclude')
                client = project.app.test_client()
                payload = {
                    'name': 'partial',
                    'paths': [
                        {'path': removed, 'is_dir': False},
                        {'path': good, 'is_dir': False},
                        {'path': folder, 'is_dir': True},
                        {'path': '/sdcard/Download/intro8.mp3', 'is_dir': False},
                    ],
                    'groups': [{'name': 'Audio/Albums', 'paths': [removed, good, folder]}],
                    'excludes': [skip],
                }
                partial = client.post('/api/saved_operations', json=payload)
                self.assertEqual(partial.status_code, 201)
                payload['name'] = 'partial with request failures'
                failing = client.post('/api/saved_operations', json=payload)
                self.assertEqual(failing.status_code, 201)
                failing_id = failing.get_json()['operation']['id']
                payload.update(name='all missing', paths=[{'path': removed, 'is_dir': False}],
                               groups=[], excludes=[])
                missing = client.post('/api/saved_operations', json=payload)
                self.assertEqual(missing.status_code, 201)
                os.remove(removed)
                server = make_server('127.0.0.1', 0, project.app, threaded=True)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    env = dict(os.environ, SDMP3_TEST_URL=f'http://127.0.0.1:{server.server_port}',
                               SDMP3_RESTORE_CASES=json.dumps([
                                   {'id': partial.get_json()['operation']['id'],
                                    'paths': [good, folder], 'warning': 'تم تجاهل 2'},
                                   {'id': missing.get_json()['operation']['id'],
                                    'paths': [], 'warning': 'تم تجاهل 1'},
                                   {'id': failing_id,
                                    'paths': [good, folder], 'warning': 'تم تجاهل 2'},
                               ]))
                    original_status = project.app.view_functions['get_paths_status']
                    original_sizes = project.app.view_functions['get_items_sizes']
                    inject_errors = {'enabled': False, 'batch': 0, 'item': 0, 'sizes': 0}

                    def read_with_injected_errors(operation_id):
                        response = original_read(operation_id)
                        inject_errors['enabled'] = operation_id == failing_id
                        return response

                    def status_with_injected_errors():
                        if inject_errors['enabled']:
                            entries = project.request.get_json()['paths']
                            if len(entries) > 1:
                                inject_errors['batch'] += 1
                                raise KeyError('/sdcard/Download/intro8.mp3')
                            if entries[0]['path'] == '/sdcard/Download/intro8.mp3':
                                inject_errors['item'] += 1
                                raise KeyError('/sdcard/Download/intro8.mp3')
                        return original_status()

                    def sizes_with_injected_errors():
                        if inject_errors['enabled']:
                            inject_errors['sizes'] += 1
                            raise KeyError('/sdcard/Download/intro8.mp3')
                        return original_sizes()

                    original_read = project.app.view_functions['api_saved_operation']
                    with mock.patch.dict(project.app.view_functions, {
                        'api_saved_operation': read_with_injected_errors,
                        'get_paths_status': status_with_injected_errors,
                        'get_items_sizes': sizes_with_injected_errors,
                    }):
                        result = subprocess.run(
                            ['node', str(Path(__file__).with_suffix('.js'))],
                            env=env, capture_output=True, text=True, timeout=55,
                        )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    loaded = json.loads(result.stdout.strip().splitlines()[-1])
                    self.assertEqual(loaded[0]['groups'],
                                     [{'name': 'Audio/Albums', 'paths': [good, folder]}])
                    self.assertEqual(loaded[0]['excludes'], [skip])
                    tasks, count, _, _ = project.plan_transfer(
                        loaded[0]['paths'], os.path.join(temporary, 'destination'),
                        excludes=set(loaded[0]['excludes']),
                    )
                    self.assertEqual(count, 1)
                    self.assertFalse(any(source == removed for source, _, _ in tasks))
                    self.assertEqual(inject_errors['batch'], 1)
                    self.assertEqual(inject_errors['item'], 1)
                    self.assertEqual(inject_errors['sizes'], 1)
                    self.assertIn('تعذر حساب الأحجام', loaded[2]['warning'])
                    self.assertEqual(loaded[2]['groups'], loaded[0]['groups'])
                    self.assertEqual(loaded[2]['excludes'], loaded[0]['excludes'])
                    print('Full-page restore: partial selection, groups, exclusions, '
                          'empty selection, failed batch/item/size requests, '
                          'modal closure, and copy plan passed.')
                finally:
                    server.shutdown()
                    worker.join(timeout=5)
                    server.server_close()
