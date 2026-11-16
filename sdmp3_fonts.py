"""Font registry for SD Copy MP3 Manager.

Ten Arabic Google Fonts, **self-hosted** in ``static/fonts/``: the phone is
often offline while copying to a card, and AGENTS.md forbids remote assets, so a
``<link>`` to fonts.googleapis.com would block the first paint on a bad network.

A font is one dict. Every ``@font-face`` rule and the CSS stack are derived from
it, so adding a font is:

1. append a dict to ``FONTS`` (``id`` + ``family`` + Arabic label),
2. run ``python fetch_fonts.py`` to download its arabic/latin subsets,
3. run ``python sdmp3_fonts.py`` to check the registry and the files.

The default is ``system``: the platform face, so the page downloads no font at
all until the user picks one. That keeps first paint as fast as it is today.

No Flask, no globals: testable with ``python sdmp3_fonts.py``.
"""

import os

# The platform stack. Used as the default *and* as the fallback behind every
# Google font, so a missing glyph never renders as tofu.
SYSTEM_STACK = ("system-ui, -apple-system, 'Segoe UI', Roboto, "
                "'Noto Sans Arabic', 'IBM Plex Sans Arabic', sans-serif")

# Google Fonts' own subset ranges. All ten families are cut with the same
# subsetter, so the ranges are identical for every family (fetch_fonts.py
# asserts this) and live here once instead of inside each font dict.
ARABIC_RANGE = (
    'U+0600-06FF, U+0750-077F, U+0870-0891, U+0897-08E1, U+08E3-08FF, '
    'U+200C-200E, U+2010-2011, U+204F, U+2E41, U+FB50-FDFF, U+FE70-FE74, '
    'U+FE76-FEFC, U+102E0-102FB, U+10E60-10E7E, U+10EC2-10EC7, U+10EC9-10EEE, '
    'U+10EF0-10EFF, U+1EE00-1EE03, U+1EE05-1EE1F, U+1EE21-1EE22, U+1EE24, '
    'U+1EE27, U+1EE29-1EE32, U+1EE34-1EE37, U+1EE39, U+1EE3B, U+1EE42, '
    'U+1EE47, U+1EE49, U+1EE4B, U+1EE4D-1EE4F, U+1EE51-1EE52, U+1EE54, '
    'U+1EE57, U+1EE59, U+1EE5B, U+1EE5D, U+1EE5F, U+1EE61-1EE62, U+1EE64, '
    'U+1EE67-1EE6A, U+1EE6C-1EE72, U+1EE74-1EE77, U+1EE79-1EE7C, U+1EE7E, '
    'U+1EE80-1EE89, U+1EE8B-1EE9B, U+1EEA1-1EEA3, U+1EEA5-1EEA9, '
    'U+1EEAB-1EEBB, U+1EEF0-1EEF1'
)
LATIN_RANGE = (
    'U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, '
    'U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, '
    'U+2212, U+2215, U+FEFF, U+FFFD'
)

# Every family below ships one variable woff2 per subset covering this range.
FONT_WEIGHT_RANGE = '400 700'

FONT_DIR = 'static/fonts'
FONT_URL_PREFIX = '/static/fonts'

# Ordered for the settings grid: the two neutral UI faces first, then the
# display/kufi/naskh faces. `desc` is shown under the name in the picker.
FONTS = [
    {'id': 'cairo', 'family': 'Cairo', 'name': 'القاهرة',
     'desc': 'حديث ومتوازن — مناسب للواجهة'},
    {'id': 'noto-sans-arabic', 'family': 'Noto Sans Arabic', 'name': 'نوتو سانس عربي',
     'desc': 'محايد عالي الوضوح'},
    {'id': 'readex-pro', 'family': 'Readex Pro', 'name': 'ريدكس برو',
     'desc': 'واسع ومريح للقراءة الطويلة'},
    {'id': 'alexandria', 'family': 'Alexandria', 'name': 'الإسكندرية',
     'desc': 'عصري بنسب عريضة'},
    {'id': 'el-messiri', 'family': 'El Messiri', 'name': 'المصيري',
     'desc': 'أنيق بحروف مرنة'},
    {'id': 'changa', 'family': 'Changa', 'name': 'تشانغا',
     'desc': 'هندسي حاد الزوايا'},
    {'id': 'kufam', 'family': 'Kufam', 'name': 'كوفام',
     'desc': 'كوفي معاصر'},
    {'id': 'noto-kufi-arabic', 'family': 'Noto Kufi Arabic', 'name': 'نوتو كوفي',
     'desc': 'كوفي هندسي واضح'},
    {'id': 'noto-naskh-arabic', 'family': 'Noto Naskh Arabic', 'name': 'نوتو نسخ',
     'desc': 'نسخ تقليدي للقراءة'},
    {'id': 'rubik', 'family': 'Rubik', 'name': 'روبيك',
     'desc': 'هندسي ناعم للعربية واللاتينية'},
]

# Size presets multiply every font-size in the stylesheet (they are all written
# as `calc(Npx * var(--font-scale))`, so one variable resizes the whole UI).
FONT_SIZES = [
    {'id': 'small', 'name': 'صغير', 'scale': 0.92},
    {'id': 'normal', 'name': 'عادي', 'scale': 1.0},
    {'id': 'large', 'name': 'كبير', 'scale': 1.10},
    {'id': 'xlarge', 'name': 'أكبر', 'scale': 1.22},
]

DEFAULT_FONT = 'system'
DEFAULT_FONT_SIZE = 'normal'

_FONT_INDEX = {f['id']: f for f in FONTS}
_SIZE_INDEX = {s['id']: s for s in FONT_SIZES}


def font_ids():
    """Valid font ids, including the `system` default (not a Google font)."""
    return [DEFAULT_FONT] + [f['id'] for f in FONTS]


def font_size_ids():
    return [s['id'] for s in FONT_SIZES]


def get_font(font_id):
    """Return the font dict, or the `system` pseudo-font for an unknown id."""
    if font_id == DEFAULT_FONT or font_id not in _FONT_INDEX:
        return {'id': DEFAULT_FONT, 'family': '', 'name': 'خط النظام',
                'desc': 'خط الجهاز — لا يُحمَّل أي ملف'}
    return _FONT_INDEX[font_id]


def font_file(font_id, subset):
    """Relative path of a font file inside FONT_DIR ('' for `system`)."""
    if font_id not in _FONT_INDEX or subset not in ('arabic', 'latin'):
        return ''
    return '{}-{}.woff2'.format(font_id, subset)


def font_stack(font_id):
    """CSS `font-family` value for a font id (always with the system fallback)."""
    font = get_font(font_id)
    if font['id'] == DEFAULT_FONT:
        return SYSTEM_STACK
    return "'{}', {}".format(font['family'], SYSTEM_STACK)


def font_scale(size_id):
    """Multiplier for a size preset; unknown ids fall back to 1.0."""
    return float((_SIZE_INDEX.get(size_id) or _SIZE_INDEX[DEFAULT_FONT_SIZE])['scale'])


def font_face_css(version=''):
    """Every `@font-face` rule, pointing at the self-hosted files.

    The rules are inert until the family is actually used, so shipping all ten
    costs nothing on first paint. `version` is appended as a query string: the
    files are cached hard, and a new release must not serve a stale font.
    """
    suffix = ('?v=' + str(version)) if version else ''
    rules = []
    for font in FONTS:
        for subset, unicode_range in (('arabic', ARABIC_RANGE), ('latin', LATIN_RANGE)):
            # %-formatting, not str.format: the rule is full of CSS braces.
            rules.append(
                "@font-face{font-family:'%s';font-style:normal;font-weight:%s;"
                "font-display:swap;src:url(%s/%s%s) format('woff2');"
                "unicode-range:%s;}" % (
                    font['family'], FONT_WEIGHT_RANGE, FONT_URL_PREFIX,
                    font_file(font['id'], subset), suffix, unicode_range)
            )
    return '\n'.join(rules)


def catalog():
    """Font list for the settings pane (no file paths, no CSS)."""
    items = [{'id': DEFAULT_FONT, 'name': 'خط النظام',
              'desc': 'خط الجهاز — لا يُحمَّل أي ملف'}]
    items += [{'id': f['id'], 'name': f['name'], 'desc': f['desc']} for f in FONTS]
    return items


def missing_files(root=''):
    """Subset files that are not on disk (used by the self-test and doctor)."""
    missing = []
    for font in FONTS:
        for subset in ('arabic', 'latin'):
            path = os.path.join(root or os.path.dirname(os.path.abspath(__file__)),
                                FONT_DIR, font_file(font['id'], subset))
            if not os.path.isfile(path) or os.path.getsize(path) < 1000:
                missing.append(path)
    return missing


if __name__ == '__main__':  # pragma: no cover - manual report
    ids = font_ids()
    assert len(ids) == len(set(ids)), 'duplicate font id'
    assert len(FONTS) == 10, 'the settings grid expects exactly 10 Google fonts'
    assert DEFAULT_FONT not in [f['id'] for f in FONTS]
    assert DEFAULT_FONT_SIZE in font_size_ids()
    for f in FONTS:
        assert f['family'] and f['name'] and f['desc'], f
    scales = [s['scale'] for s in FONT_SIZES]
    assert scales == sorted(scales), 'size presets must stay ordered'
    assert 0.8 <= scales[0] and scales[-1] <= 1.3, scales
    # `system` must be spelled without a leading quote: it is a keyword stack.
    assert font_stack(DEFAULT_FONT).startswith('system-ui'), font_stack(DEFAULT_FONT)
    assert font_stack('cairo').startswith("'Cairo', ")
    assert font_stack('nope') == SYSTEM_STACK
    assert font_scale('nope') == 1.0 and font_scale('large') == 1.10
    css = font_face_css('1.7.0')
    assert css.count('@font-face') == len(FONTS) * 2, css.count('@font-face')
    assert '/static/fonts/cairo-arabic.woff2?v=1.7.0' in css
    assert 'unicode-range:U+0600-06FF' in css
    missing = missing_files()
    print('{} fonts, {} subsets, {} size presets, {} bytes of @font-face CSS'.format(
        len(FONTS), len(FONTS) * 2, len(FONT_SIZES), len(css)))
    print('default: {} / {} (scale {})'.format(DEFAULT_FONT, DEFAULT_FONT_SIZE,
                                               font_scale(DEFAULT_FONT_SIZE)))
    for f in FONTS:
        print('  {:<18} {:<20} arabic+{:<6} latin'.format(
            f['id'], f['family'], 'ok' if os.path.isfile(os.path.join(
                FONT_DIR, font_file(f['id'], 'arabic'))) else 'MISSING'))
    if missing:
        raise SystemExit('missing font files (run: python fetch_fonts.py):\n  '
                         + '\n  '.join(missing))
    print('sdmp3_fonts: all checks passed')
