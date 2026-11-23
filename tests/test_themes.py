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
        definitions = {item['id']: item for item in sdmp3_themes._THEME_DEFS}
        palette = {key: definitions['coffee-blues'][key]
                   for key in sdmp3_themes.BASE_KEYS}
        actual_hash = hashlib.sha256(
            json.dumps(palette, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()
        self.assertEqual(
            actual_hash,
            '52fe6ccc9de842332b31148ed585bedf2b1bd4ac61755148b3ee347be6a834a4',
        )

    def test_presets_have_contrast_and_six_preview_colors(self):
        for definition in sdmp3_themes._THEME_DEFS:
            with self.subTest(theme=definition['id']):
                self.assertEqual(sdmp3_themes.check_contrast(definition), [])
                self.assertEqual(sdmp3_themes.check_kind_hues(definition), [])
                theme = sdmp3_themes.get_theme(definition['id'])
                self.assertEqual(len(theme['swatches']), 6)
                self.assertEqual(theme['swatches'], [
                    definition['bg'], definition['card_2'], definition['accent'],
                    definition['kind_internal'], definition['kind_sdcard'],
                    definition['text'],
                ])

    def test_retired_presets_are_absent_and_new_palettes_are_available(self):
        ids = set(sdmp3_themes.theme_ids())
        self.assertTrue(sdmp3_themes.REMOVED_THEME_IDS.issubset({
            'dusk-plum', 'forest-mist', 'deep-charcoal', 'night-oxford',
            'clay-night', 'clay-rose', 'olive-grove', 'midnight-teal', 'slate-sand',
        }))
        self.assertTrue(sdmp3_themes.REMOVED_THEME_IDS.isdisjoint(ids))
        self.assertTrue({
            'aurora-night', 'cobalt-evening', 'sage-evening', 'cloud-azure',
        }.issubset(ids))
        self.assertEqual(sdmp3_themes.DEFAULT_THEME, 'coffee-blues')

    def test_theme_setting_round_trips_and_returns_matching_swatches(self):
        client = project.app.test_client()
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(sdmp3_state, 'STATE_DIR', state_dir), \
                mock.patch.object(project, 'APP_SETTINGS', dict(project.APP_SETTINGS)):
            response = client.post('/api/settings', json={'theme': 'aurora-night'})
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['settings']['theme'], 'aurora-night')
            self.assertNotIn('custom_colors', payload['settings'])
            self.assertEqual(payload['tokens'],
                             sdmp3_themes.theme_tokens('aurora-night'))
            self.assertEqual(payload['swatches'],
                             sdmp3_themes.get_theme('aurora-night')['swatches'])

            restored = sdmp3_state.load_settings(
                sdmp3_themes.theme_ids(), sdmp3_themes.DEFAULT_THEME,
                project.font_ids(), project.DEFAULT_FONT,
                project.font_size_ids(), project.DEFAULT_FONT_SIZE)
            self.assertEqual(restored['theme'], 'aurora-night')

    def test_legacy_custom_colors_and_retired_theme_are_removed(self):
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(sdmp3_state, 'STATE_DIR', state_dir):
            legacy = {
                'version': 2,
                'theme': 'clay-night',
                'custom_colors': {'bg': '#171717', 'accent': '#5A5A5A'},
            }
            with open(sdmp3_state.state_file(sdmp3_state.SETTINGS_FILE),
                      'w', encoding='utf-8') as handle:
                json.dump(legacy, handle)

            settings = sdmp3_state.load_settings(
                sdmp3_themes.theme_ids(), sdmp3_themes.DEFAULT_THEME,
                project.font_ids(), project.DEFAULT_FONT,
                project.font_size_ids(), project.DEFAULT_FONT_SIZE)
            self.assertEqual(settings['theme'], sdmp3_themes.DEFAULT_THEME)
            self.assertNotIn('custom_colors', settings)
            stored = sdmp3_state.load_json(
                sdmp3_state.state_file(sdmp3_state.SETTINGS_FILE), {})
            self.assertNotIn('custom_colors', stored)
            self.assertEqual(stored['version'], sdmp3_state.SETTINGS_VERSION)

    def test_custom_palette_input_and_preview_route_are_removed(self):
        client = project.app.test_client()
        with tempfile.TemporaryDirectory() as state_dir, \
                mock.patch.object(sdmp3_state, 'STATE_DIR', state_dir), \
                mock.patch.object(project, 'APP_SETTINGS', dict(project.APP_SETTINGS)):
            response = client.post('/api/settings', json={
                'theme': 'cloud-azure',
                'custom_colors': {'bg': '#000000', 'text': '#000000'},
            })
            self.assertEqual(response.status_code, 200)
            result = response.get_json()
            self.assertNotIn('custom_colors', result['settings'])
            self.assertEqual(result['tokens'],
                             sdmp3_themes.theme_tokens('cloud-azure'))
            self.assertEqual(
                client.post('/api/settings/theme-preview', json={}).status_code,
                404,
            )
            self.assertEqual(
                client.post('/api/settings', json={'theme': 'slate-sand'}).status_code,
                400,
            )

    def test_settings_page_renders_only_builtin_six_color_previews(self):
        with mock.patch.object(project, 'APP_SETTINGS', dict(project.APP_SETTINGS)):
            response = project.app.test_client().get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="themeColorPreview"', html)
        self.assertIn('id="themePreviewName"', html)
        self.assertIn('data-swatches=', html)
        preview = html.split('<div id="themeColorPreview"', 1)[1].split('</div>', 1)[0]
        self.assertEqual(preview.count('theme-preview-chip'), 6)
        self.assertNotIn('customTheme', html)
        self.assertNotIn('value="custom"', html)
        self.assertNotIn('/api/settings/theme-preview', html)

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


class PhotoBackgroundUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = project.app.test_client().get('/').get_data(as_text=True)
        cls.photo_script = cls.html.split(
            '// ---- local background photo -----------------------------------------', 1
        )[1].split('// Typography is two CSS variables', 1)[0]

    def test_settings_include_picker_preview_toggle_and_opacity(self):
        for control_id in (
                'photoBackgroundEnabled', 'photoBackgroundFile',
                'photoBackgroundPreview', 'removePhotoBackgroundBtn',
                'photoOverlayRange', 'photoOverlayValue', 'photoBackgroundState'):
            with self.subTest(control=control_id):
                self.assertIn('id="{}"'.format(control_id), self.html)
        self.assertIn('accept="image/png,image/jpeg,image/webp"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('min="0" max="85" step="5" value="60"', self.html)

    def test_image_is_saved_restored_and_removed_locally_without_upload(self):
        for feature in (
                "store.put(value, PHOTO_DB_KEY)",
                "store.get(PHOTO_DB_KEY)",
                "store.delete(PHOTO_DB_KEY)",
                "photoDatabaseRequest('put', record)",
                "photoDatabaseRequest('get')",
                "photoDatabaseRequest('delete')",
                "async function initializePhotoBackground()",
                "sdmp3_photo_background_enabled",
                "sdmp3_photo_background_opacity"):
            with self.subTest(feature=feature):
                self.assertIn(feature, self.photo_script)
        self.assertNotIn('fetch(', self.photo_script)
        self.assertNotIn('/api/', self.photo_script)

    def test_unsupported_oversized_and_huge_images_show_clear_errors(self):
        self.assertIn('const PHOTO_MAX_BYTES = 8 * 1024 * 1024', self.photo_script)
        self.assertIn('const PHOTO_MAX_PIXELS = 48000000', self.photo_script)
        self.assertIn('const PHOTO_MAX_EDGE = 12000', self.photo_script)
        self.assertIn('نوع الصورة غير مدعوم', self.photo_script)
        self.assertIn('الصورة كبيرة جداً. الحد الأقصى 8 ميجابايت.', self.photo_script)
        self.assertIn('أبعاد الصورة كبيرة جداً', self.photo_script)

    def test_photo_is_a_tinted_page_background_and_preview_fits_mobile_width(self):
        self.assertIn('body.photo-background-enabled', self.html)
        self.assertIn('linear-gradient(var(--photo-overlay), var(--photo-overlay))', self.html)
        self.assertIn('.photo-background-preview { display:block; width:100%;', self.html)
        self.assertIn('background-size:cover, cover', self.html)


if __name__ == '__main__':
    unittest.main()
