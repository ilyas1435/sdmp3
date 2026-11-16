"""Small, atomic JSON stores for SD Copy MP3 Manager.

Everything the app remembers between runs that is *not* the app itself lives in
`~/.local/state/sdmp3/` (next to the CLI's `instance.json` and `sdmp3.log`), so
`git pull` never has to deal with user data.

Two rules keep this safe on a phone:

* every write is atomic (`tmp` + `os.replace`), so a battery cut mid-write can
  never leave a half JSON that would break the next start;
* nothing here raises. A corrupt or unreadable file means "use defaults", and
  the broken file is moved aside as `.bak` so the user can still inspect it.

No Flask, no globals: testable with `python sdmp3_state.py`.
"""

import json
import os
import time

STATE_DIR = os.path.join(os.path.expanduser('~'), '.local', 'state', 'sdmp3')

SETTINGS_FILE = 'settings.json'
SEARCH_HISTORY_FILE = 'search_history.json'
INDEX_SETTINGS_FILE = 'index_settings.json'

SETTINGS_VERSION = 2
HISTORY_VERSION = 1
INDEX_SETTINGS_VERSION = 2

# "the last 25" is the product decision; keep it in one place.
HISTORY_ITEM_LIMIT = 25
HISTORY_QUERY_LIMIT = 25

# Default JOC / index settings. Kept in one place so the server and the page
# agree on the shape. Nothing here raises: unknown or missing keys fall back to
# these defaults. The output *name* is deliberately absent - it is generated and
# edited in the create dialog, never remembered between runs.
DEFAULT_INDEX_SETTINGS = {
    'version': INDEX_SETTINGS_VERSION,
    'joc_start': 1,
    'joc_limit': 93,
    'joc_mode': '1',
    'joc_template': '4',
    'joc_strip': True,
    'joc_download_txt': False,
    'joc_review_txt': False,
    'joc_mp3_index': False,
    'mp3_to_dest': True,
    'tts_voice': 'ar-AE-HamdanNeural',
    'tts_rate': '+0%',
    'mask_mode': 'noise',
}


def state_file(name):
    return os.path.join(STATE_DIR, name)


def _ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def _quarantine(path):
    """Move a broken file aside instead of silently deleting the evidence."""
    try:
        if os.path.exists(path):
            os.replace(path, path + '.bak')
    except OSError:
        pass


def load_json(path, default):
    """Read a JSON object, or return `default` for missing/corrupt content."""
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        _quarantine(path)
        return default
    if not isinstance(data, dict):
        _quarantine(path)
        return default
    return data


def save_json(path, payload, mode=0o600):
    """Atomically replace `path` with `payload`. Never raises; returns bool."""
    try:
        _ensure_dir()
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(',', ':'))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def load_settings(valid_theme_ids, default_theme,
                  valid_font_ids=None, default_font='system',
                  valid_font_size_ids=None, default_font_size='normal'):
    """Return the saved settings, with every unknown value replaced.

    Unknown keys are dropped on purpose: the file is user-editable and a typo
    must not end up echoed into the page. `font` and `font_size` are validated
    against the registries in sdmp3_fonts.py, the same way themes are validated
    against sdmp3_themes.py.
    """
    raw = load_json(state_file(SETTINGS_FILE), {})
    theme = raw.get('theme')
    if theme not in valid_theme_ids:
        theme = default_theme
    font = raw.get('font')
    if font not in (valid_font_ids or []):
        font = default_font
    font_size = raw.get('font_size')
    if font_size not in (valid_font_size_ids or []):
        font_size = default_font_size
    settings = {'version': SETTINGS_VERSION, 'theme': theme,
                'font': font, 'font_size': font_size}
    raw_version = raw.get('version')
    if isinstance(raw_version, int) and raw_version > SETTINGS_VERSION:
        # A newer app wrote this file. Keep what we understand, but do not
        # pretend to understand the rest.
        settings['version'] = raw_version
    return settings


def save_settings(settings):
    payload = {
        'version': SETTINGS_VERSION,
        'theme': settings.get('theme'),
        'font': settings.get('font'),
        'font_size': settings.get('font_size'),
        'updated': int(time.time()),
    }
    return save_json(state_file(SETTINGS_FILE), payload)


# --------------------------------------------------------------------------
# search history
# --------------------------------------------------------------------------

def _empty_history():
    return {'version': HISTORY_VERSION, 'items': [], 'queries': []}


def load_search_history():
    raw = load_json(state_file(SEARCH_HISTORY_FILE), _empty_history())
    items = raw.get('items')
    queries = raw.get('queries')
    history = {
        'version': HISTORY_VERSION,
        'items': [i for i in items if isinstance(i, dict) and i.get('path')][:HISTORY_ITEM_LIMIT]
        if isinstance(items, list) else [],
        'queries': [q for q in queries if isinstance(q, dict) and q.get('q')][:HISTORY_QUERY_LIMIT]
        if isinstance(queries, list) else [],
    }
    return history


def save_search_history(history):
    payload = {
        'version': HISTORY_VERSION,
        'items': (history.get('items') or [])[:HISTORY_ITEM_LIMIT],
        'queries': (history.get('queries') or [])[:HISTORY_QUERY_LIMIT],
        'updated': int(time.time()),
    }
    return save_json(state_file(SEARCH_HISTORY_FILE), payload)


def load_index_settings():
    """Return the saved JOC index settings, filling missing keys from defaults.

    Never raises: a corrupt file falls back to defaults and is quarantined.
    """
    raw = load_json(state_file(INDEX_SETTINGS_FILE), {})
    settings = dict(DEFAULT_INDEX_SETTINGS)
    for key in DEFAULT_INDEX_SETTINGS:
        if key in raw:
            settings[key] = raw[key]
    settings['version'] = INDEX_SETTINGS_VERSION
    return settings


def save_index_settings(settings):
    payload = {
        'version': INDEX_SETTINGS_VERSION,
        'updated': int(time.time()),
    }
    for key in DEFAULT_INDEX_SETTINGS:
        if key in settings:
            payload[key] = settings[key]
    return save_json(state_file(INDEX_SETTINGS_FILE), payload)


def upsert_recent(entries, entry, key, limit):
    """Put `entry` at the front, drop any previous copy of the same `key`.

    Pure function (no I/O) so the dedupe rule is testable: a user who searches
    the same folder ten times should see it once, at the top.
    """
    value = entry.get(key)
    out = []
    for existing in entries or []:
        if not isinstance(existing, dict):
            continue
        if existing.get(key) == value:
            continue
        out.append(existing)
    out.insert(0, entry)
    return out[:limit]


def prune_history_to_roots(history, root_exists):
    """Drop saved results whose storage root disappeared (card pulled out).

    Called once at startup: doing it per request would stat every path on a
    volume that may be slow or asleep.
    """
    items = []
    for item in history.get('items') or []:
        root = item.get('root') or ''
        try:
            if root and not root_exists(root):
                continue
        except Exception:
            continue
        items.append(item)
    queries = []
    for query in history.get('queries') or []:
        root = query.get('root') or ''
        try:
            if root and not root_exists(root):
                continue
        except Exception:
            continue
        queries.append(query)
    history['items'] = items
    history['queries'] = queries
    return history


if __name__ == '__main__':  # pragma: no cover - manual report
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix='sdmp3-state-')
    real_dir = STATE_DIR
    STATE_DIR = tmpdir
    try:
        # settings: theme + font + font size, all validated against registries
        assert load_settings(['a', 'b'], 'a') == {
            'version': SETTINGS_VERSION, 'theme': 'a',
            'font': 'system', 'font_size': 'normal'}
        assert save_settings({'theme': 'b', 'font': 'f1', 'font_size': 'big'}) is True
        saved = load_settings(['a', 'b'], 'a', ['f1', 'f2'], 'system', ['big', 'small'], 'normal')
        assert saved['theme'] == 'b' and saved['font'] == 'f1' and saved['font_size'] == 'big', saved
        bad = load_settings(['a'], 'a', ['other-font'], 'system', ['huge'], 'normal')
        assert bad['font'] == 'system' and bad['font_size'] == 'normal', bad
        rows = []
        for i in range(40):
            rows = upsert_recent(rows, {'path': '/p/{}'.format(i)}, 'path', 25)
        assert len(rows) == 25 and rows[0]['path'] == '/p/39'
        rows = upsert_recent(rows, {'path': '/p/39'}, 'path', 25)
        assert len(rows) == 25 and rows[0]['path'] == '/p/39', 'dedupe failed'
        save_search_history({'items': rows, 'queries': []})
        assert len(load_search_history()['items']) == 25
        with open(state_file(SETTINGS_FILE), 'w', encoding='utf-8') as fh:
            fh.write('{broken')
        assert load_settings(['a'], 'a')['theme'] == 'a', 'corrupt file not handled'
        assert os.path.exists(state_file(SETTINGS_FILE) + '.bak')
        # index settings round-trip + defaults
        assert load_index_settings() == DEFAULT_INDEX_SETTINGS
        custom = dict(DEFAULT_INDEX_SETTINGS)
        custom['joc_limit'] = 200
        custom['tts_rate'] = '+20%'
        assert save_index_settings(custom) is True
        loaded = load_index_settings()
        assert loaded['joc_limit'] == 200
        assert loaded['tts_rate'] == '+20%'
        assert loaded['joc_start'] == DEFAULT_INDEX_SETTINGS['joc_start']
        # the MP3 destination flag is part of the saved contract
        assert loaded['mp3_to_dest'] is True
        # the output name is never persisted (generated per creation)
        custom['joc_filename'] = 'stale.txt'
        save_index_settings(custom)
        assert 'joc_filename' not in load_index_settings()
    finally:
        STATE_DIR = real_dir
    print('sdmp3_state: all checks passed')
