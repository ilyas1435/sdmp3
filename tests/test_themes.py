import hashlib
import json
import re
import tempfile
import unittest
from unittest import mock

import project
import sdmp3_state
import sdmp3_themes


class ThemePaletteTests(unittest.TestCase):
    def test_original_palette_remains_unchanged(self):
        expected = {
            'coffee-blues': '52fe6ccc9de842332b31148ed585bedf2b1bd4ac61755148b3ee347be6a834a4',
        }
        definitions = {item['id']: item for item in sdmp3_themes._THEME_DEFS}
        for theme_id, expected_hash in expected.items():
            palette = {key: definitions[theme_id][key] for key in sdmp3_themes.BASE_KEYS}
            actual_hash = hashlib.sha256(
                json.dumps(palette, sort_keys=True, separators=(',', ':')).encode('utf-8')
            ).hexdigest()
            self.assertEqual(actual_hash, expected_hash, theme_id)

    def test_all_palettes_meet_text_contrast_and_volume_separation(self):
        for definition in sdmp3_themes._THEME_DEFS:
            with self.subTest(theme=definition['id']):
                self.assertEqual(sdmp3_themes.check_contrast(definition), [])
                self.assertEqual(sdmp3_themes.check_kind_hues(definition), [])

    def test_theme_setting_round_trips_and_returns_matching_swatches(self):
        original = dict(project.APP_SETTINGS)
        client = project.app.test_client()
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(sdmp3_state, 'STATE_DIR', state_dir), \
                mock.patch.object(project, 'APP_SETTINGS', original.copy()):
            response = client.post('/api/settings', json={'theme': 'slate-sand'})
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['settings']['theme'], 'slate-sand')
            self.assertEqual(
                payload['tokens'],
                sdmp3_themes.theme_tokens('slate-sand'),
            )

            saved = client.get('/api/settings').get_json()
            self.assertEqual(saved['settings']['theme'], 'slate-sand')
            slate = next(theme for theme in saved['themes'] if theme['id'] == 'slate-sand')
            self.assertEqual(
                slate['swatches'],
                sdmp3_themes.get_theme('slate-sand')['swatches'],
            )
            restored = sdmp3_state.load_settings(
                sdmp3_themes.theme_ids(),
                sdmp3_themes.DEFAULT_THEME,
                project.font_ids(),
                project.DEFAULT_FONT,
                project.font_size_ids(),
                project.DEFAULT_FONT_SIZE,
            )
            self.assertEqual(restored['theme'], 'slate-sand')
            page = client.get('/').get_data(as_text=True)
            self.assertIn('value="slate-sand" data-theme-id="slate-sand"', page)

    def test_settings_page_renders_selected_theme_preview_and_palette_data(self):
        with mock.patch.object(project, 'APP_SETTINGS', dict(project.APP_SETTINGS)):
            page = project.app.test_client().get('/')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('id="themeColorPreview"', html)
        self.assertIn('id="themePreviewName"', html)
        self.assertIn('data-swatches=', html)

    def test_about_panel_has_version_local_notice_and_profile_link(self):
        page = project.app.test_client().get('/').get_data(as_text=True)
        self.assertIn('id="settingsPaneAbout"', page)
        self.assertIn('class="about-version-value num">v', page)
        self.assertIn('إدارة الملفات والنسخ والإعدادات تتم محليًا', page)
        self.assertIn('تحويل النص إلى MP3 يحتاج اتصالًا بالإنترنت', page)
        self.assertIn('href="https://github.com/ilyas1435"', page)
        self.assertIn('aria-label="حساب GitHub لـ ilyas1435"', page)

    def test_settings_controls_keep_ids_and_have_svg_cues(self):
        page = project.app.test_client().get('/').get_data(as_text=True)
        for control_id in ('themeSelect', 'fontSelect', 'sizeSeg', 'jocStart',
                           'jocLimit', 'jocMode', 'jocTemplate', 'jocMp3Index',
                           'ttsVoice', 'ttsRate'):
            with self.subTest(control=control_id):
                self.assertIn('id="{}"'.format(control_id), page)
        self.assertIn('href="#ico-palette"', page)
        self.assertIn('href="#ico-text-size"', page)
        self.assertIn('href="#ico-voice"', page)
        self.assertIn('href="#ico-review"', page)
        symbols = set(re.findall(r'<symbol id="([^"]+)"', page))
        references = set(re.findall(r'<use href="#([^"]+)"', page))
        dynamic_references = {'ico-...', "' + id + '"}
        self.assertEqual(references - symbols - dynamic_references, set(),
                         'every static SVG cue resolves to a sprite symbol')


if __name__ == '__main__':
    unittest.main()
