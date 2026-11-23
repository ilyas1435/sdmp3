"""Copy-group validation and folder layout (pure functions).

A copy group is one or more selected roots that must land inside a named folder
in the destination instead of straight into it. A group name may be a path
(`العلماء/ابن باز`), which is how a group is nested inside another group.

Everything here is pure: no os.scandir, no globals, no Flask. project.py calls
`prepare_copy_groups()` from the HTTP route, and the copy worker uses the same
`group_segments()`/`group_dir_chain()` so both always agree on where files go.
"""

import os

# Fat32: a path component is limited to 255 UTF-16 units, but a name a human
# types is far shorter. 80 is generous and keeps the tree readable.
GROUP_NAME_MAX = 80
# Whole breadcrumb. Leaves room for the destination path on a FAT volume.
GROUP_PATH_MAX = 180
# Three levels is "group inside group inside group": enough for
# العلماء/ابن باز/دروس, and still readable in a 300px-wide list.
GROUP_MAX_DEPTH = 3
GROUP_SEPARATOR = '/'

# Characters FAT does not allow in a name. The separator is handled before this
# check (it splits the breadcrumb), so it stays in the set for a literal `\`.
_FAT_BAD_CHARS = set('/\\:*?"<>|')


def validate_group_name(name):
    """Return (clean_segment, error) for ONE folder name.

    Empty input returns ('', None) so callers can distinguish "not provided"
    from "invalid".
    """
    if not isinstance(name, str):
        return '', 'اسم المجلد غير صالح'
    name = name.strip()
    if not name:
        return '', None
    if len(name) > GROUP_NAME_MAX:
        return '', 'اسم المجلد طويل جداً (الحد {} حرفاً)'.format(GROUP_NAME_MAX)
    if name in ('.', '..'):
        return '', 'اسم المجلد غير صالح'
    if any(c in _FAT_BAD_CHARS or ord(c) < 32 for c in name):
        return '', 'اسم المجلد يحتوي رموزاً غير مسموحة: / \\ : * ? " < > |'
    if name.endswith(' ') or name.endswith('.'):
        return '', 'لا يمكن أن ينتهي اسم المجلد بمسافة أو نقطة'
    return name, None


def validate_group_path(raw):
    """Validate a group breadcrumb like 'العلماء/ابن باز'.

    Returns (clean_path, error). Repeated slashes collapse, a leading/trailing
    slash is ignored, and every segment must be a legal FAT folder name. The
    result is what gets created on the card, so it is always relative.
    """
    if not isinstance(raw, str):
        return '', 'اسم المجلد غير صالح'
    text = raw.strip()
    if not text:
        return '', None
    if len(text) > GROUP_PATH_MAX:
        return '', 'مسار المجلد طويل جداً (الحد {} حرفاً)'.format(GROUP_PATH_MAX)

    segments = []
    for part in text.split(GROUP_SEPARATOR):
        segment = part.strip()
        if not segment:
            continue
        clean, error = validate_group_name(segment)
        if error:
            return '', error
        segments.append(clean)

    if not segments:
        return '', 'اكتب اسماً للمجلد'
    if len(segments) > GROUP_MAX_DEPTH:
        return '', 'أقصى تداخل {} مستويات (مثال: العلماء/ابن باز)'.format(GROUP_MAX_DEPTH)
    return GROUP_SEPARATOR.join(segments), None


def group_segments(clean_path):
    """['العلماء', 'ابن باز'] for 'العلماء/ابن باز'."""
    return [part for part in (clean_path or '').split(GROUP_SEPARATOR) if part]


def group_dir_chain(dest, clean_path):
    """Absolute folder chain to create, parents first: [dest/A, dest/A/B]."""
    chain = []
    current = dest
    for segment in group_segments(clean_path):
        current = os.path.join(current, segment)
        chain.append(current)
    return chain


def prepare_copy_groups(raw_groups, paths):
    """Normalise the optional copy groups from a request.

    Each group is ``{name, paths}`` and becomes one folder (possibly nested)
    inside the destination. Returns ``(groups, error)`` where ``groups`` is a
    list of ``(clean_path, [paths])`` with empty groups dropped.

    Names must be valid and unique (FAT is case-insensitive, so "علوم" and
    "علوم " collide; "A" and "A/B" do not - that is nesting), every path must be
    part of this transfer's selection, and no path may sit in two groups -
    otherwise the same folder would be copied twice into two parents.
    """
    if not raw_groups:
        return [], None
    if not isinstance(raw_groups, list):
        return [], 'صيغة المجموعات غير صالحة'

    by_norm = {}
    for p in paths:
        try:
            by_norm[os.path.normpath(p)] = p
        except (TypeError, ValueError):
            return [], 'مسار غير صالح داخل التحديد'

    groups = []
    seen_names = set()
    assigned = {}
    for item in raw_groups:
        if not isinstance(item, dict):
            return [], 'صيغة المجموعات غير صالحة'
        raw_name = item.get('name')
        if not isinstance(raw_name, str) or not raw_name.strip():
            return [], 'اسم مجلد الضم مطلوب'
        name, error = validate_group_path(raw_name)
        if error:
            return [], error
        key = name.casefold()
        if key in seen_names:
            return [], 'اسم المجلد «{}» مستخدم في أكثر من مجموعة'.format(name)
        seen_names.add(key)

        group_paths = []
        for p in (item.get('paths') or []):
            try:
                norm = os.path.normpath(p)
            except (TypeError, ValueError):
                return [], 'مسار غير صالح داخل المجموعة'
            if norm not in by_norm:
                return [], 'العنصر «{}» ليس ضمن التحديد'.format(p)
            if norm in assigned:
                return [], ('العنصر «{}» موجود في مجموعتين: «{}» و«{}»'
                            .format(p, assigned[norm], name))
            assigned[norm] = name
            group_paths.append(by_norm[norm])
        if group_paths:
            groups.append((name, group_paths))
    return groups, None


if __name__ == '__main__':  # pragma: no cover - manual report
    # Single names keep working exactly as before.
    assert validate_group_path('العلماء')[0] == 'العلماء'
    assert validate_group_path('  A//B/ ')[0] == 'A/B'
    assert validate_group_path('A/B/C')[0] == 'A/B/C'
    assert validate_group_path('A/B/C/D')[1], 'depth 4 must be rejected'
    assert validate_group_path('../etc')[1], '.. must be rejected'
    assert validate_group_path('A:B')[1], 'bad FAT char must be rejected'
    assert validate_group_path('')[0] == ''
    assert group_segments('A/B') == ['A', 'B']
    assert group_dir_chain('/d', 'A/B') == ['/d/A', '/d/A/B']

    selection = ['/src/one', '/src/two', '/src/three']
    groups, error = prepare_copy_groups([
        {'name': 'A', 'paths': ['/src/one']},
        {'name': 'A/B', 'paths': ['/src/two']},
    ], selection)
    assert error is None and groups == [('A', ['/src/one']), ('A/B', ['/src/two'])], groups

    # Same item in two groups: one file, two parents, refused.
    _g, error = prepare_copy_groups([
        {'name': 'A', 'paths': ['/src/one']},
        {'name': 'B', 'paths': ['/src/one']},
    ], selection)
    assert error and 'مجموعتين' in error

    # a/b collides with A/B on a case-insensitive card; empty groups are dropped.
    _g, error = prepare_copy_groups([
        {'name': 'A/B', 'paths': ['/src/one']},
        {'name': 'a/b', 'paths': ['/src/two']},
    ], selection)
    assert error and 'أكثر من مجموعة' in error
    groups, error = prepare_copy_groups([{'name': 'A', 'paths': []}], selection)
    assert groups == [] and error is None
    print('sdmp3_groups: all checks passed')
