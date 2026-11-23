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
SAVED_OPERATIONS_FILE = 'saved_operations.json'

SETTINGS_VERSION = 3
HISTORY_VERSION = 1
INDEX_SETTINGS_VERSION = 2
SAVED_OPERATIONS_VERSION = 1

# "the last 25" is the product decision; keep it in one place.
HISTORY_ITEM_LIMIT = 25
HISTORY_QUERY_LIMIT = 25
SAVED_OPERATIONS_LIMIT = 50
SAVED_OPERATION_PATH_LIMIT = 3000
SAVED_OPERATION_GROUP_LIMIT = 500

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
    # Additive v2 key: a hand-written/older file simply means "off". POST
    # coercion lives in project.py; stored files only ever hold a real bool.
    auto_spoof = raw.get('auto_spoof')
    if not isinstance(auto_spoof, bool):
        auto_spoof = False
    # Additive v2 key: how many path segments «تنظيم ذكي» keeps. 1 is the
    # default because a single folder is what a normal copy wants; 3 is the
    # deepest group the server accepts.
    smart_depth = raw.get('smart_depth')
    if (not isinstance(smart_depth, int) or isinstance(smart_depth, bool)
            or smart_depth < 1 or smart_depth > 3):
        smart_depth = 1
    settings = {'version': SETTINGS_VERSION, 'theme': theme,
                'font': font, 'font_size': font_size,
                'auto_spoof': auto_spoof, 'smart_depth': smart_depth}
    raw_version = raw.get('version')
    newer_version = isinstance(raw_version, int) and raw_version > SETTINGS_VERSION
    if newer_version:
        # A newer app wrote this file. Keep what we understand, but do not
        # pretend to understand the rest.
        settings['version'] = raw_version
    elif 'custom_colors' in raw or (
            'theme' in raw and raw.get('theme') not in valid_theme_ids):
        # Remove the retired custom palette and reset any removed preset to the
        # default in-place. Do not overwrite settings from a newer app version.
        save_settings(settings)
    return settings


def save_settings(settings):
    payload = {
        'version': SETTINGS_VERSION,
        'theme': settings.get('theme'),
        'font': settings.get('font'),
        'font_size': settings.get('font_size'),
        'auto_spoof': bool(settings.get('auto_spoof')),
        'smart_depth': settings.get('smart_depth') or 1,
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


def _saved_operation_record(raw):
    """Return a safe persisted operation shape, or None for a broken record."""
    if not isinstance(raw, dict):
        return None
    operation_id = raw.get('id')
    name = raw.get('name')
    paths = raw.get('paths')
    excludes = raw.get('excludes')
    groups = raw.get('groups')
    if (not isinstance(operation_id, str) or not operation_id
            or len(operation_id) > 64
            or not all(ch.isalnum() or ch in '-_' for ch in operation_id)):
        return None
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        return None
    if not isinstance(paths, list) or len(paths) > SAVED_OPERATION_PATH_LIMIT:
        return None
    if not isinstance(excludes, list) or len(excludes) > SAVED_OPERATION_PATH_LIMIT:
        return None
    if not isinstance(groups, list) or len(groups) > SAVED_OPERATION_GROUP_LIMIT:
        return None

    clean_paths = []
    seen_paths = set()
    for item in paths:
        if not isinstance(item, dict):
            return None
        path = item.get('path')
        is_dir = item.get('is_dir')
        if (not isinstance(path, str) or not path.startswith('/')
                or len(path) > 4096 or '\x00' in path
                or not isinstance(is_dir, bool) or path in seen_paths):
            return None
        seen_paths.add(path)
        clean_paths.append({'path': path, 'is_dir': is_dir})

    clean_excludes = []
    seen_excludes = set()
    for path in excludes:
        if (not isinstance(path, str) or not path.startswith('/')
                or len(path) > 4096 or '\x00' in path or path in seen_excludes):
            return None
        seen_excludes.add(path)
        clean_excludes.append(path)

    clean_groups = []
    for group in groups:
        if not isinstance(group, dict):
            return None
        group_name = group.get('name')
        group_paths = group.get('paths')
        if (not isinstance(group_name, str) or not group_name
                or len(group_name) > 180 or not isinstance(group_paths, list)
                or len(group_paths) > SAVED_OPERATION_PATH_LIMIT):
            return None
        if any(not isinstance(path, str) or path not in seen_paths for path in group_paths):
            return None
        clean_groups.append({'name': group_name, 'paths': list(group_paths)})

    created = raw.get('created', 0)
    updated = raw.get('updated', created)
    if (not isinstance(created, int) or isinstance(created, bool)
            or not isinstance(updated, int) or isinstance(updated, bool)):
        return None
    return {
        'id': operation_id,
        'name': name.strip(),
        'paths': clean_paths,
        'excludes': clean_excludes,
        'groups': clean_groups,
        'created': created,
        'updated': updated,
    }


def load_saved_operations():
    """Load saved copy operations, dropping only malformed individual records."""
    raw = load_json(state_file(SAVED_OPERATIONS_FILE), {})
    operations = raw.get('operations')
    if not isinstance(operations, list):
        return []
    result = []
    seen_ids = set()
    for item in operations[:SAVED_OPERATIONS_LIMIT]:
        clean = _saved_operation_record(item)
        if clean is None or clean['id'] in seen_ids:
            continue
        seen_ids.add(clean['id'])
        result.append(clean)
    return result


def save_saved_operations(operations):
    """Atomically persist validated saved operations; return False on bad data."""
    if not isinstance(operations, list) or len(operations) > SAVED_OPERATIONS_LIMIT:
        return False
    clean = []
    seen_ids = set()
    for item in operations:
        record = _saved_operation_record(item)
        if record is None or record['id'] in seen_ids:
            return False
        seen_ids.add(record['id'])
        clean.append(record)
    return save_json(state_file(SAVED_OPERATIONS_FILE), {
        'version': SAVED_OPERATIONS_VERSION,
        'operations': clean,
        'updated': int(time.time()),
    })


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
            'font': 'system', 'font_size': 'normal', 'auto_spoof': False,
            'smart_depth': 1}
        assert save_settings({'theme': 'b', 'font': 'f1', 'font_size': 'big',
                              'auto_spoof': True}) is True
        saved = load_settings(['a', 'b'], 'a', ['f1', 'f2'], 'system', ['big', 'small'], 'normal')
        assert saved['theme'] == 'b' and saved['font'] == 'f1' and saved['font_size'] == 'big', saved
        assert saved['auto_spoof'] is True, saved
        assert save_settings({'theme': 'b', 'smart_depth': 3}) is True
        assert load_settings(['a', 'b'], 'a')['smart_depth'] == 3
        # Older versions stored custom colors and presets that are no longer
        # available. Loading migrates to a built-in preset and drops the palette.
        legacy = {'version': 2, 'theme': 'retired-theme',
                  'custom_colors': {'bg': '#192133'}}
        with open(state_file(SETTINGS_FILE), 'w', encoding='utf-8') as fh:
            json.dump(legacy, fh)
        migrated = load_settings(['a', 'b'], 'a')
        assert migrated['theme'] == 'a' and 'custom_colors' not in migrated, migrated
        migrated_file = load_json(state_file(SETTINGS_FILE), {})
        assert 'custom_colors' not in migrated_file, migrated_file
        assert migrated_file['version'] == SETTINGS_VERSION, migrated_file
        # save writes what it is given; load is the validator
        assert save_settings({'theme': 'b', 'smart_depth': 9}) is True
        assert load_settings(['a', 'b'], 'a')['smart_depth'] == 1, 'load must clamp 9'
        assert save_settings({'theme': 'b', 'smart_depth': 0}) is True
        assert load_settings(['a', 'b'], 'a')['smart_depth'] == 1, 'load must clamp 0'
        assert save_settings({'theme': 'b', 'smart_depth': True}) is True
        assert load_settings(['a', 'b'], 'a')['smart_depth'] == 1, 'bool is not a depth'
        assert save_settings({'theme': 'b', 'smart_depth': 1}) is True
        with open(state_file(SETTINGS_FILE), 'w', encoding='utf-8') as fh:
            json.dump({'version': SETTINGS_VERSION, 'theme': 'b', 'font': 'f1',
                       'font_size': 'big'}, fh)
        assert load_settings(['a', 'b'], 'a')['auto_spoof'] is False, 'legacy file not additive'
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
