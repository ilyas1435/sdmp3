"""Theme registry for SD Copy MP3 Manager.

A theme is a plain dict of palette colours; every alpha variant (soft/line/tile)
is *derived* here, so a theme can never drift out of sync with the CSS and a new
theme is one dict entry. This module has no Flask and no global state: it can be
tested with `python sdmp3_themes.py`.

Adding a theme
--------------
1. Append a dict to `_THEME_DEFS` with the keys listed in `BASE_KEYS`.
2. Run `python sdmp3_themes.py` — it prints every contrast ratio and refuses to
   finish if one is below the accessibility floor.

That is the whole contract. Nothing in project.py needs to change.
"""

# --------------------------------------------------------------------------
# colour maths
# --------------------------------------------------------------------------

def _parse_hex(value):
    """Return (r, g, b) for '#rrggbb' (or '#rgb'). Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError('colour must be a string, got {!r}'.format(value))
    s = value.strip().lstrip('#')
    if len(s) == 3:
        s = ''.join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError('not a hex colour: {!r}'.format(value))
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def rgba(hex_colour, alpha):
    """'#39918c' + 0.18 -> 'rgba(57,145,140,0.18)' (the CSS form used here)."""
    r, g, b = _parse_hex(hex_colour)
    return 'rgba({},{},{},{:.2f})'.format(r, g, b, alpha)


def rgb_channels(hex_colour):
    """'#39918c' -> '57,145,140', for CSS rgba(var(--token), alpha)."""
    return '{},{},{}'.format(*_parse_hex(hex_colour))


def relative_luminance(hex_colour):
    """WCAG 2.1 relative luminance of a hex colour."""
    channels = []
    for raw in _parse_hex(hex_colour):
        c = raw / 255.0
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg):
    """WCAG contrast ratio between two hex colours (1.0 .. 21.0)."""
    l1 = relative_luminance(fg)
    l2 = relative_luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def colour_hue(hex_colour):
    """Hue of a colour in degrees (0..360). Neutral greys get 0."""
    r, g, b = (c / 255.0 for c in _parse_hex(hex_colour))
    high, low = max(r, g, b), min(r, g, b)
    if high == low:
        return 0.0
    delta = high - low
    if high == r:
        hue = ((g - b) / delta) % 6
    elif high == g:
        hue = (b - r) / delta + 2
    else:
        hue = (r - g) / delta + 4
    return hue * 60.0


def hue_gap(first, second):
    """Shortest angular distance between two hues, in degrees (0..180)."""
    delta = abs(colour_hue(first) - colour_hue(second)) % 360.0
    return min(delta, 360.0 - delta)


# Every volume kind is told apart by icon + caption, but the hue has to help and
# never blur: internal storage is sand in almost every theme, so the SD card may
# not sit on the same warm hue — two tiles then read as one volume.
KIND_HUE_RULES = (
    ('internal vs sd card', 'kind_internal', 'kind_sdcard', 45.0),
    ('sd card vs memory card', 'kind_sdcard', 'kind_card', 25.0),
)


def check_kind_hues(defn):
    """Return the (label, gap, floor) entries whose hues sit too close."""
    failures = []
    for label, first, second, floor in KIND_HUE_RULES:
        gap = hue_gap(defn[first], defn[second])
        if gap < floor:
            failures.append((label, round(gap, 1), floor))
    return failures


# --------------------------------------------------------------------------
# token schema
# --------------------------------------------------------------------------

# Every theme must provide these. They are the honest design decisions; all the
# translucent variants below are computed so they always match their base hue.
BASE_KEYS = (
    'bg', 'card', 'card_2', 'field', 'border', 'text', 'text_muted',
    'accent', 'accent_hover', 'accent_fill', 'accent_fill_hover', 'accent_text',
    'on_accent', 'on_fill', 'accent_ink',
    'success', 'success_fill', 'danger', 'danger_fill', 'danger_ink', 'warning',
    'sand_fill', 'sand_fill_hover',
    'kind_internal', 'kind_internal_ink',
    'kind_sdcard', 'kind_sdcard_ink',
    'kind_usb', 'kind_usb_ink',
    'kind_card', 'kind_card_ink',
    'scrim', 'sheet', 'shadow',
)

# Per-volume translucency. An SD card needs a stronger tint than USB; keeping
# them here (not in each theme) is what makes the four kinds look like a family.
_KIND_ALPHA = {
    'internal': (0.16, 0.22, 0.42),
    'sdcard': (0.20, 0.24, 0.50),
    'usb': (0.16, 0.22, 0.42),
    'card': (0.18, 0.24, 0.45),
}

# Order matters only for readability of the generated CSS.
TOKEN_ORDER = (
    '--bg', '--card', '--card-2', '--field', '--border', '--text', '--text-muted',
    '--accent', '--accent-hover', '--accent-fill', '--accent-fill-hover',
    '--accent-ink', '--accent-soft', '--accent-line', '--accent-text',
    '--accent-glow', '--focus',
    '--on-accent', '--on-fill', '--sand-fill', '--sand-fill-hover',
    '--success', '--success-fill', '--danger', '--danger-fill', '--danger-ink',
    '--danger-soft', '--danger-line', '--warning', '--warning-soft',
    '--warning-line', '--ok-soft', '--ok-line',
    '--kind-internal', '--kind-internal-ink', '--kind-internal-soft',
    '--kind-internal-tile', '--kind-internal-line',
    '--kind-sdcard', '--kind-sdcard-ink', '--kind-sdcard-soft',
    '--kind-sdcard-tile', '--kind-sdcard-line',
    '--kind-usb', '--kind-usb-ink', '--kind-usb-soft',
    '--kind-usb-tile', '--kind-usb-line',
    '--kind-card', '--kind-card-ink', '--kind-card-soft',
    '--kind-card-tile', '--kind-card-line',
    '--pill-bg', '--shine', '--scrim', '--sheet',
    '--shadow-soft', '--shadow-strong', '--shadow-card',
    '--rule', '--field-line', '--bar-bg', '--bar-line', '--num-font',
    '--card-rgb', '--card-2-rgb', '--field-rgb', '--bar-rgb',
    '--radius',
)

# Monospace digits, Latin only. The Arabic UI keeps `--app-font`; this stack is
# applied with `.num` to sizes, speeds, counts and paths so columns cannot jitter
# (tabular figures) and a path reads as data, not prose. No webfont: every
# platform has a mono face, and shipping one would be ~100 KB for digits.
NUM_FONT_STACK = ("ui-monospace, 'SFMono-Regular', 'Roboto Mono', "
                  "'DejaVu Sans Mono', monospace")


def build_tokens(defn):
    """Turn a palette dict into the complete CSS custom-property map."""
    missing = [k for k in BASE_KEYS if not defn.get(k)]
    if missing:
        raise ValueError('theme {!r} is missing: {}'.format(
            defn.get('id', '?'), ', '.join(missing)))

    tokens = {
        '--bg': defn['bg'],
        '--card': defn['card'],
        '--card-2': defn['card_2'],
        '--field': defn['field'],
        '--border': defn['border'],
        '--text': defn['text'],
        '--text-muted': defn['text_muted'],

        '--accent': defn['accent'],
        '--accent-hover': defn['accent_hover'],
        '--accent-fill': defn['accent_fill'],
        '--accent-fill-hover': defn['accent_fill_hover'],
        '--accent-ink': defn['accent_ink'],
        '--accent-text': defn['accent_text'],
        '--on-accent': defn['on_accent'],
        '--on-fill': defn['on_fill'],
        '--sand-fill': defn['sand_fill'],
        '--sand-fill-hover': defn['sand_fill_hover'],

        '--success': defn['success'],
        '--success-fill': defn['success_fill'],
        '--danger': defn['danger'],
        '--danger-fill': defn['danger_fill'],
        '--danger-ink': defn['danger_ink'],
        '--warning': defn['warning'],

        '--pill-bg': rgba(defn['text'], 0.04),
        '--shine': rgba(defn['text'], 0.55),
        '--scrim': defn['scrim'],
        '--sheet': defn['sheet'],
        '--shadow-soft': rgba(defn['shadow'], 0.50),
        '--shadow-strong': rgba(defn['shadow'], 0.60),
        '--shadow-card': rgba(defn['shadow'], 0.50),
        # Separators instead of a box per section: a 1px rule carries the same
        # grouping without five identical cards. Derived from the theme's own
        # border, so a light theme keeps a light rule.
        '--rule': rgba(defn['border'], 0.72),
        '--field-line': defn['border'],
        # The fixed action bar must be opaque: a translucent bar over a tree of
        # filenames is unreadable while scrolling, and backdrop-filter costs a
        # phone GPU for nothing.
        '--bar-bg': defn['card'],
        '--bar-line': rgba(defn['accent'], 0.30),
        '--num-font': NUM_FONT_STACK,
        # Channels, not another colour literal: the page combines these with a
        # single saved opacity so every surface fades with the active theme.
        '--card-rgb': rgb_channels(defn['card']),
        '--card-2-rgb': rgb_channels(defn['card_2']),
        '--field-rgb': rgb_channels(defn['field']),
        '--bar-rgb': rgb_channels(defn['card']),
        '--radius': defn.get('radius', '12px'),
    }

    # Accent family: all four alphas come from the same hue.
    tokens['--accent-soft'] = rgba(defn['accent'], 0.18)
    tokens['--accent-line'] = rgba(defn['accent'], 0.42)
    tokens['--accent-glow'] = rgba(defn['accent'], 0.40)
    tokens['--focus'] = rgba(defn['accent'], 0.45)

    tokens['--danger-soft'] = rgba(defn['danger'], 0.12)
    tokens['--danger-line'] = rgba(defn['danger'], 0.38)
    tokens['--warning-soft'] = rgba(defn['warning'], 0.10)
    tokens['--warning-line'] = rgba(defn['warning'], 0.38)
    tokens['--ok-soft'] = rgba(defn['success'], 0.10)
    tokens['--ok-line'] = rgba(defn['success'], 0.38)

    for kind in ('internal', 'sdcard', 'usb', 'card'):
        soft, tile, line = _KIND_ALPHA[kind]
        tokens['--kind-' + kind] = defn['kind_' + kind]
        tokens['--kind-' + kind + '-ink'] = defn['kind_' + kind + '_ink']
        tokens['--kind-' + kind + '-soft'] = rgba(defn['kind_' + kind], soft)
        tokens['--kind-' + kind + '-tile'] = rgba(defn['kind_' + kind], tile)
        tokens['--kind-' + kind + '-line'] = rgba(defn['kind_' + kind + '_ink'], line)

    return tokens


# --------------------------------------------------------------------------
# contrast contract
# --------------------------------------------------------------------------

# Ratios that must hold for every theme. Body text is 14px, so the 4.5 "normal
# text" floor applies everywhere; the primary text gets the 7.0 AAA-ish floor
# because it is the whole page.
CONTRAST_RULES = (
    ('text on card', 'text', 'card', 7.0),
    ('text on page', 'text', 'bg', 7.0),
    ('muted on card', 'text_muted', 'card', 4.5),
    ('muted on field', 'text_muted', 'field', 4.5),
    ('accent text on card', 'accent_text', 'card', 4.5),
    ('warning on card', 'warning', 'card', 4.5),
    ('success on card', 'success', 'card', 4.5),
    ('danger text on card', 'danger_ink', 'card', 4.5),
    ('label on primary', 'on_accent', 'accent_fill', 4.5),
    ('label on success', 'on_fill', 'success_fill', 4.5),
    ('label on danger', 'on_fill', 'danger_fill', 4.5),
    ('label on sand', 'on_fill', 'sand_fill', 4.5),
    ('internal ink on card', 'kind_internal_ink', 'card', 4.5),
    ('sd ink on card', 'kind_sdcard_ink', 'card', 4.5),
    ('usb ink on card', 'kind_usb_ink', 'card', 4.5),
    ('card ink on card', 'kind_card_ink', 'card', 4.5),
)


def check_contrast(defn):
    """Return a list of (label, ratio, floor) that fail for this theme."""
    failures = []
    for label, fg_key, bg_key, floor in CONTRAST_RULES:
        ratio = contrast_ratio(defn[fg_key], defn[bg_key])
        if ratio + 0.005 < floor:
            failures.append((label, round(ratio, 2), floor))
    return failures


# --------------------------------------------------------------------------
# the themes
# --------------------------------------------------------------------------
# "Coffee Blues" is the original palette (color-hex 102595) and must stay
# byte-for-byte identical to the hand-tuned values it replaces.

_THEME_DEFS = [{'id': 'coffee-blues',
  'name': 'قهوة ورمال',
  'desc': 'الأصلي: رمال دافئة على سليت بارد',
  'dark': True,
  'bg': '#192133',
  'card': '#222b40',
  'card_2': '#2d374d',
  'field': '#161c2b',
  'border': '#2f3c5a',
  'text': '#e9e2d1',
  'text_muted': '#aaa297',
  'accent': '#3d8e9c',
  'accent_hover': '#327883',
  'accent_fill': '#327883',
  'accent_fill_hover': '#2a6973',
  'accent_text': '#89c1c9',
  'on_accent': '#f7f4f0',
  'on_fill': '#151e28',
  'accent_ink': '#151e28',
  'success': '#7fbf93',
  'success_fill': '#7fbf93',
  'danger': '#f43f5e',
  'danger_fill': '#f43f5e',
  'danger_ink': '#fc8b9b',
  'warning': '#f59e0b',
  'sand_fill': '#d0b49f',
  'sand_fill_hover': '#c4a88f',
  'kind_internal': '#d0b49f',
  'kind_internal_ink': '#d0b49f',
  'kind_sdcard': '#2eb9cb',
  'kind_sdcard_ink': '#8fdde9',
  'kind_usb': '#8fa9c4',
  'kind_usb_ink': '#a8bfd6',
  'kind_card': '#a3b18a',
  'kind_card_ink': '#b7c39f',
  'scrim': 'rgba(8,12,16,0.88)',
  'sheet': 'rgba(24,33,44,0.97)',
  'shadow': '#000000'},
 {'id': 'midnight-teal',
   'name': 'ليل المحيط',
   'desc': 'أزرق بحري عميق مع فيروزي هادئ',
  'dark': True,
   'bg': '#101a22',
   'card': '#192832',
   'card_2': '#223744',
   'field': '#0c151b',
   'border': '#334955',
   'text': '#eaf2f4',
   'text_muted': '#a8bbc2',
   'accent': '#55c2bd',
   'accent_hover': '#40a9a2',
   'accent_fill': '#287c79',
   'accent_fill_hover': '#216966',
   'accent_text': '#8adbd5',
  'on_accent': '#f2fffd',
  'on_fill': '#08191b',
  'accent_ink': '#08191b',
  'success': '#74c79b',
  'success_fill': '#74c79b',
  'danger': '#f2607c',
  'danger_fill': '#f2607c',
  'danger_ink': '#ffa1b2',
  'warning': '#e9b44c',
  'sand_fill': '#d8c3a5',
  'sand_fill_hover': '#c9b192',
  'kind_internal': '#d8c3a5',
  'kind_internal_ink': '#d8c3a5',
  'kind_sdcard': '#25b6c6',
  'kind_sdcard_ink': '#8ce0ec',
  'kind_usb': '#86b0d8',
  'kind_usb_ink': '#a8c8ea',
  'kind_card': '#a4b981',
  'kind_card_ink': '#bfd29c',
  'scrim': 'rgba(4,12,13,0.90)',
  'sheet': 'rgba(16,36,38,0.97)',
  'shadow': '#000000'},
 {'id': 'slate-sand',
   'name': 'سليت أزرق',
    'desc': 'رمادي أزرق واضح بلمسة سماوية',
  'dark': True,
    'bg': '#151c27',
    'card': '#202a38',
    'card_2': '#2b3747',
    'field': '#101722',
    'border': '#3c4a5b',
    'text': '#eef2f7',
    'text_muted': '#aeb9c7',
    'accent': '#8db4e0',
    'accent_hover': '#729cc9',
    'accent_fill': '#4e719f',
    'accent_fill_hover': '#3c5d88',
    'accent_text': '#a9c9eb',
   'on_accent': '#ffffff',
  'on_fill': '#1a222c',
  'accent_ink': '#1a222c',
  'success': '#8ccfa6',
  'success_fill': '#8ccfa6',
  'danger': '#ef6b7f',
  'danger_fill': '#ef6b7f',
  'danger_ink': '#ffa4b2',
  'warning': '#e8b45a',
  'sand_fill': '#d0b49f',
  'sand_fill_hover': '#c4a88f',
  'kind_internal': '#d0b49f',
  'kind_internal_ink': '#d6bfae',
  'kind_sdcard': '#35b7c9',
  'kind_sdcard_ink': '#95e0ec',
  'kind_usb': '#88b0d8',
  'kind_usb_ink': '#b3cfe9',
  'kind_card': '#a8bb8c',
  'kind_card_ink': '#c3d3aa',
  'scrim': 'rgba(10,16,24,0.88)',
  'sheet': 'rgba(27,36,48,0.97)',
  'shadow': '#05080d'},
 {'id': 'olive-grove',
   'name': 'أخضر صنوبر',
    'desc': 'أخضر غابي هادئ ولمسات زيتونية',
  'dark': True,
    'bg': '#171d19',
    'card': '#222b25',
    'card_2': '#2d382f',
    'field': '#111712',
    'border': '#414e44',
    'text': '#eef3ea',
    'text_muted': '#b4c0b0',
    'accent': '#b6c98a',
    'accent_hover': '#9fb373',
    'accent_fill': '#657947',
    'accent_fill_hover': '#52633a',
    'accent_text': '#d1dfaa',
   'on_accent': '#ffffff',
  'on_fill': '#0f150a',
  'accent_ink': '#0f150a',
  'success': '#8fca8f',
  'success_fill': '#8fca8f',
  'danger': '#e8705f',
  'danger_fill': '#e8705f',
  'danger_ink': '#ffa99a',
  'warning': '#dcae4a',
  'sand_fill': '#cdb992',
  'sand_fill_hover': '#bfa87f',
  'kind_internal': '#cdb992',
  'kind_internal_ink': '#cdb992',
  'kind_sdcard': '#2fb9c6',
  'kind_sdcard_ink': '#8fe0e8',
  'kind_usb': '#87aec9',
  'kind_usb_ink': '#aacbe4',
  'kind_card': '#a0b87c',
  'kind_card_ink': '#bdd49a',
  'scrim': 'rgba(7,10,5,0.90)',
  'sheet': 'rgba(27,34,23,0.97)',
  'shadow': '#000000'},
 {'id': 'clay-rose',
   'name': 'ورد مطفأ',
    'desc': 'درجات توتية هادئة على خلفية دافئة',
  'dark': True,
    'bg': '#211a1c',
    'card': '#2d2327',
    'card_2': '#3a2c32',
    'field': '#181214',
    'border': '#4c3b42',
    'text': '#f4eceb',
    'text_muted': '#c0afb2',
    'accent': '#dc9cae',
    'accent_hover': '#c78296',
    'accent_fill': '#995568',
    'accent_fill_hover': '#80475a',
    'accent_text': '#f0b9c7',
   'on_accent': '#ffffff',
  'on_fill': '#1d1210',
  'accent_ink': '#1d1210',
  'success': '#8fc795',
  'success_fill': '#8fc795',
  'danger': '#f2687c',
  'danger_fill': '#f2687c',
  'danger_ink': '#ffa6b3',
  'warning': '#e6b05a',
  'sand_fill': '#d5b394',
  'sand_fill_hover': '#c6a382',
  'kind_internal': '#d5b394',
  'kind_internal_ink': '#d5b394',
  'kind_sdcard': '#2fb6c8',
  'kind_sdcard_ink': '#91deea',
  'kind_usb': '#8caac6',
  'kind_usb_ink': '#b0c8de',
  'kind_card': '#a7b383',
  'kind_card_ink': '#c4cfa2',
  'scrim': 'rgba(14,9,9,0.89)',
  'sheet': 'rgba(38,29,29,0.97)',
  'shadow': '#0a0505'},
 {'id': 'graphite',
   'name': 'جرافيت وعنبر',
    'desc': 'فحمي محايد مع تفاصيل ذهبية دافئة',
  'dark': True,
    'bg': '#171b20',
    'card': '#222830',
    'card_2': '#2d3540',
    'field': '#11151a',
    'border': '#3c4652',
    'text': '#eef1f4',
    'text_muted': '#aeb8c2',
    'accent': '#e4b56b',
    'accent_hover': '#ce9d50',
    'accent_fill': '#986c2d',
    'accent_fill_hover': '#7e5724',
    'accent_text': '#f1ca8e',
   'on_accent': '#ffffff',
  'on_fill': '#16181a',
  'accent_ink': '#16181a',
  'success': '#8ccfa6',
  'success_fill': '#8ccfa6',
  'danger': '#ef6b7f',
  'danger_fill': '#ef6b7f',
  'danger_ink': '#ffa4b2',
  'warning': '#e8b45a',
  'sand_fill': '#d8cbb8',
  'sand_fill_hover': '#c8b9a4',
  'kind_internal': '#d8cbb8',
  'kind_internal_ink': '#d8cbb8',
  'kind_sdcard': '#2fb8c9',
  'kind_sdcard_ink': '#8fdfeb',
  'kind_usb': '#8fb4cf',
  'kind_usb_ink': '#b3d0e6',
  'kind_card': '#aab98c',
  'kind_card_ink': '#c6d4a8',
  'scrim': 'rgba(6,7,8,0.90)',
  'sheet': 'rgba(26,29,32,0.97)',
  'shadow': '#000000'},
 {'id': 'paper',
   'name': 'عاجي وفيروزي',
    'desc': 'خلفية عاجية مريحة ولمسات بحرية',
  'dark': False,
    'bg': '#f5f1e8',
    'card': '#fffdf8',
    'card_2': '#ece5d8',
    'field': '#faf7f0',
    'border': '#ddd3c2',
    'text': '#292923',
    'text_muted': '#635f56',
    'accent': '#38857f',
    'accent_hover': '#2f706c',
    'accent_fill': '#27645f',
    'accent_fill_hover': '#1f5651',
    'accent_text': '#215c58',
  'on_accent': '#ffffff',
  'on_fill': '#ffffff',
  'accent_ink': '#222831',
  'success': '#2f7d4f',
  'success_fill': '#2f7d4f',
  'danger': '#c8334b',
  'danger_fill': '#c8334b',
  'danger_ink': '#b0243a',
  'warning': '#8a5a06',
  'sand_fill': '#96603a',
  'sand_fill_hover': '#84522f',
  'kind_internal': '#8a6a48',
  'kind_internal_ink': '#7c5c3c',
  'kind_sdcard': '#0e7490',
  'kind_sdcard_ink': '#0e7490',
  'kind_usb': '#3f6b96',
  'kind_usb_ink': '#35597e',
  'kind_card': '#5f7540',
  'kind_card_ink': '#546838',
  'scrim': 'rgba(34,30,25,0.55)',
  'sheet': 'rgba(255,253,248,0.97)',
  'shadow': '#7a6a55'},
 {'id': 'sandstone',
   'name': 'طين فاتح',
    'desc': 'درجات رملية دافئة ولمسات طينية',
  'dark': False,
    'bg': '#f6efea',
    'card': '#fffcf9',
    'card_2': '#efe1d8',
    'field': '#faf5f1',
    'border': '#e3d1c7',
    'text': '#322a27',
    'text_muted': '#705e57',
    'accent': '#aa6b53',
    'accent_hover': '#92583f',
    'accent_fill': '#824d39',
    'accent_fill_hover': '#6e4030',
    'accent_text': '#7e4835',
   'on_accent': '#ffffff',
  'on_fill': '#fff8f3',
  'accent_ink': '#2b2320',
  'success': '#2f7d4f',
  'success_fill': '#2f7d4f',
  'danger': '#b83349',
  'danger_fill': '#b83349',
  'danger_ink': '#a12539',
  'warning': '#7f5305',
  'sand_fill': '#8f6135',
  'sand_fill_hover': '#7e552d',
  'kind_internal': '#8a6a48',
  'kind_internal_ink': '#7a5b3a',
  'kind_sdcard': '#0f6f86',
  'kind_sdcard_ink': '#0f6f86',
  'kind_usb': '#3b6289',
  'kind_usb_ink': '#325271',
  'kind_card': '#5b7038',
  'kind_card_ink': '#516431',
  'scrim': 'rgba(38,30,22,0.55)',
  'sheet': 'rgba(251,245,236,0.97)',
  'shadow': '#8a7458'},
 {'id': 'night-oxford',
   'name': 'نيلي ليلي',
    'desc': 'أزرق نيلي عميق مع نصوص واضحة',
  'dark': True,
    'bg': '#161b2b',
    'card': '#20283b',
    'card_2': '#2b3650',
    'field': '#101523',
    'border': '#3a4968',
    'text': '#edf0f8',
    'text_muted': '#b1bbd0',
    'accent': '#9eaef4',
    'accent_hover': '#8799e2',
    'accent_fill': '#5b6db4',
    'accent_fill_hover': '#475995',
    'accent_text': '#c2ccff',
  'on_accent': '#ffffff',
  'on_fill': '#0f172a',
  'accent_ink': '#0f172a',
  'success': '#34d399',
  'success_fill': '#34d399',
  'danger': '#f87171',
  'danger_fill': '#f87171',
  'danger_ink': '#fca5a5',
  'warning': '#fbbf24',
  'sand_fill': '#fbbf24',
  'sand_fill_hover': '#d97706',
  'kind_internal': '#fbbf24',
  'kind_internal_ink': '#fbbf24',
  'kind_sdcard': '#22d3ee',
  'kind_sdcard_ink': '#a5f3fc',
  'kind_usb': '#60a5fa',
  'kind_usb_ink': '#93c5fd',
  'kind_card': '#a3e635',
  'kind_card_ink': '#ccff7d',
  'scrim': 'rgba(8,12,20,0.90)',
  'sheet': 'rgba(24,35,50,0.97)',
  'shadow': '#000000'},
 {'id': 'deep-charcoal',
   'name': 'صنوبر داكن',
    'desc': 'فحمي أخضر متوازن بلمسات بحرية',
  'dark': True,
    'bg': '#151c1b',
    'card': '#202a28',
    'card_2': '#2c3936',
    'field': '#101615',
    'border': '#3d514b',
    'text': '#edf3f0',
    'text_muted': '#acbfba',
    'accent': '#79c6b0',
    'accent_hover': '#5eb19a',
    'accent_fill': '#367f70',
    'accent_fill_hover': '#2b6a5d',
    'accent_text': '#a2decb',
  'on_accent': '#ffffff',
  'on_fill': '#121212',
  'accent_ink': '#121212',
  'success': '#4ade80',
  'success_fill': '#4ade80',
  'danger': '#fb7185',
  'danger_fill': '#fb7185',
  'danger_ink': '#fda4af',
  'warning': '#fbbf24',
  'sand_fill': '#2dd4bf',
  'sand_fill_hover': '#14b8a6',
  'kind_internal': '#d0b49f',
  'kind_internal_ink': '#e6d6c6',
  'kind_sdcard': '#2dd4bf',
  'kind_sdcard_ink': '#5eead4',
  'kind_usb': '#60a5fa',
  'kind_usb_ink': '#eaeaea',
  'kind_card': '#4ade80',
  'kind_card_ink': '#eaeaea',
  'scrim': 'rgba(4,4,4,0.90)',
  'sheet': 'rgba(24,24,24,0.97)',
  'shadow': '#000000'},
 {'id': 'clear-dawn',
   'name': 'سماء صافية',
    'desc': 'أزرق فاتح مريح بتباين واضح',
  'dark': False,
    'bg': '#f0f5fa',
   'card': '#ffffff',
    'card_2': '#e5edf5',
    'field': '#f7fafd',
    'border': '#d5e0eb',
    'text': '#222d3b',
    'text_muted': '#5e6d7d',
    'accent': '#4f78ae',
    'accent_hover': '#3f669a',
    'accent_fill': '#345a8e',
    'accent_fill_hover': '#294a78',
    'accent_text': '#31598d',
  'on_accent': '#ffffff',
  'on_fill': '#0f172a',
  'accent_ink': '#3f3f3f',
  'success': '#14532d',
  'success_fill': '#bbf7d0',
  'danger': '#7f1d1d',
  'danger_fill': '#fee2e2',
  'danger_ink': '#7f1d1d',
  'warning': '#b45309',
  'sand_fill': '#fef3c7',
  'sand_fill_hover': '#d97706',
  'kind_internal': '#d97706',
  'kind_internal_ink': '#92400e',
  'kind_sdcard': '#0e7490',
  'kind_sdcard_ink': '#0d6b84',
  'kind_usb': '#1d4ed8',
  'kind_usb_ink': '#1d4ed8',
  'kind_card': '#14532d',
  'kind_card_ink': '#14532d',
  'scrim': 'rgba(63,63,63,0.50)',
  'sheet': 'rgba(253,251,247,0.97)',
  'shadow': '#a8a292'},
 {'id': 'pearl-breath',
   'name': 'زجاج بحري',
    'desc': 'درجات لؤلؤية باردة مع فيروزي هادئ',
  'dark': False,
    'bg': '#edf5f4',
    'card': '#fcfefd',
    'card_2': '#e0ebea',
    'field': '#f5f9f8',
    'border': '#d0deda',
    'text': '#233330',
    'text_muted': '#5f7470',
    'accent': '#3e8580',
    'accent_hover': '#32736e',
    'accent_fill': '#286662',
    'accent_fill_hover': '#205754',
    'accent_text': '#24635f',
  'on_accent': '#ffffff',
  'on_fill': '#0f172a',
  'accent_ink': '#22303c',
  'success': '#14532d',
  'success_fill': '#bbf7d0',
  'danger': '#7f1d1d',
  'danger_fill': '#fee2e2',
  'danger_ink': '#7f1d1d',
  'warning': '#7f5305',
  'sand_fill': '#fef3c7',
  'sand_fill_hover': '#8f6135',
  'kind_internal': '#8f6135',
  'kind_internal_ink': '#7e552d',
  'kind_sdcard': '#0e7490',
  'kind_sdcard_ink': '#0e7490',
  'kind_usb': '#2f6f9f',
  'kind_usb_ink': '#2f6f9f',
  'kind_card': '#14532d',
  'kind_card_ink': '#14532d',
  'scrim': 'rgba(34,48,60,0.50)',
  'sheet': 'rgba(244,246,248,0.97)',
  'shadow': '#b9c4ce'},
 {'id': 'github-dark',
   'name': 'أزرق ليلي',
    'desc': 'فحمي بارد بلمسات زرقاء واضحة',
  'dark': True,
    'bg': '#111820',
    'card': '#1b2530',
    'card_2': '#263341',
    'field': '#0c131a',
    'border': '#344452',
    'text': '#e9f0f5',
    'text_muted': '#a9b7c4',
    'accent': '#77aaf7',
    'accent_hover': '#6594df',
    'accent_fill': '#376dab',
    'accent_fill_hover': '#2a5b91',
    'accent_text': '#a7c7ff',
  'on_accent': '#ffffff',
  'on_fill': '#0d1117',
  'accent_ink': '#0d1117',
  'success': '#3fb950',
  'success_fill': '#3fb950',
  'danger': '#f85149',
  'danger_fill': '#f85149',
  'danger_ink': '#ff7b72',
  'warning': '#d29922',
  'sand_fill': '#d29922',
  'sand_fill_hover': '#bb8009',
  'kind_internal': '#d29922',
  'kind_internal_ink': '#e3b341',
  'kind_sdcard': '#39c5cf',
  'kind_sdcard_ink': '#76e3ea',
  'kind_usb': '#58a6ff',
  'kind_usb_ink': '#79c0ff',
  'kind_card': '#3fb950',
  'kind_card_ink': '#56d364',
  'scrim': 'rgba(1,4,9,0.88)',
  'sheet': 'rgba(22,27,34,0.97)',
  'shadow': '#000000'},
 {'id': 'github-light',
   'name': 'أزرق فاتح',
    'desc': 'فاتح محايد مع أزرق هادئ',
  'dark': False,
    'bg': '#f3f6fa',
   'card': '#ffffff',
    'card_2': '#e8eef4',
    'field': '#f8fafc',
    'border': '#d7e0e8',
    'text': '#1f2933',
    'text_muted': '#5d6976',
    'accent': '#4b7eb8',
    'accent_hover': '#3d6ca4',
    'accent_fill': '#2f649f',
    'accent_fill_hover': '#285486',
    'accent_text': '#24558a',
  'on_accent': '#ffffff',
  'on_fill': '#ffffff',
  'accent_ink': '#1f2328',
  'success': '#1a7f37',
  'success_fill': '#1f883d',
  'danger': '#cf222e',
  'danger_fill': '#cf222e',
  'danger_ink': '#a40e26',
  'warning': '#9a6700',
  'sand_fill': '#9a6700',
  'sand_fill_hover': '#7d5200',
  'kind_internal': '#9a6700',
  'kind_internal_ink': '#7d4e00',
  'kind_sdcard': '#1b7c83',
  'kind_sdcard_ink': '#1b7c83',
  'kind_usb': '#0969da',
  'kind_usb_ink': '#0550ae',
  'kind_card': '#1a7f37',
  'kind_card_ink': '#116329',
  'scrim': 'rgba(31,35,40,0.50)',
  'sheet': 'rgba(246,248,250,0.97)',
  'shadow': '#8c959f'},
 {'id': 'forest-mist',
   'name': 'أخضر ضبابي',
    'desc': 'أخضر فاتح متزن بنبرة طبيعية',
  'dark': False,
    'bg': '#edf4ef',
    'card': '#fcfffc',
    'card_2': '#e2ece4',
    'field': '#f6faf7',
    'border': '#cfded2',
    'text': '#24332a',
    'text_muted': '#596b5e',
    'accent': '#578565',
    'accent_hover': '#476f55',
    'accent_fill': '#3c6849',
    'accent_fill_hover': '#31573c',
    'accent_text': '#315d3d',
  'on_accent': '#ffffff',
  'on_fill': '#ffffff',
  'accent_ink': '#1d2a20',
  'success': '#1f6b3a',
  'success_fill': '#1f6b3a',
  'danger': '#a8273a',
  'danger_fill': '#a8273a',
  'danger_ink': '#8f1f30',
  'warning': '#7a5205',
  'sand_fill': '#8a6a48',
  'sand_fill_hover': '#76583a',
  'kind_internal': '#8a6a48',
  'kind_internal_ink': '#795a3c',
  'kind_sdcard': '#0f766e',
  'kind_sdcard_ink': '#0f766e',
  'kind_usb': '#3f6b96',
  'kind_usb_ink': '#35597e',
  'kind_card': '#5f7540',
  'kind_card_ink': '#546838',
  'scrim': 'rgba(24,36,27,0.52)',
  'sheet': 'rgba(248,251,247,0.97)',
  'shadow': '#7b8a7c'},
 {'id': 'dusk-plum',
   'name': 'لافندر مسائي',
    'desc': 'بنفسجي داكن ناعم ولمسات لافندر',
  'dark': True,
    'bg': '#1d1825',
    'card': '#2a2333',
    'card_2': '#372e43',
    'field': '#15111b',
    'border': '#4a3d56',
    'text': '#f1edf5',
    'text_muted': '#b9aec3',
    'accent': '#c3a2de',
    'accent_hover': '#ad87cd',
    'accent_fill': '#795b94',
    'accent_fill_hover': '#654b7d',
    'accent_text': '#ddc8ef',
   'on_accent': '#ffffff',
  'on_fill': '#1c1724',
  'accent_ink': '#1c1724',
  'success': '#8fd0a6',
  'success_fill': '#8fd0a6',
  'danger': '#f2838f',
  'danger_fill': '#f2838f',
  'danger_ink': '#ffb0b8',
  'warning': '#e8bc6e',
  'sand_fill': '#d9bfa5',
  'sand_fill_hover': '#c8aa8d',
  'kind_internal': '#d9bfa5',
  'kind_internal_ink': '#d9bfa5',
  'kind_sdcard': '#38c3d6',
  'kind_sdcard_ink': '#9be7f2',
  'kind_usb': '#93b6dd',
  'kind_usb_ink': '#b6d0ea',
  'kind_card': '#a9bf8d',
  'kind_card_ink': '#c2d3a8',
  'scrim': 'rgba(10,7,14,0.90)',
  'sheet': 'rgba(34,27,44,0.97)',
  'shadow': '#000000'}]

REMOVED_THEME_IDS = frozenset({
    'dusk-plum', 'forest-mist', 'deep-charcoal', 'night-oxford',
    'clay-night', 'clay-rose', 'olive-grove', 'midnight-teal', 'slate-sand',
})
_THEME_DEFS = [definition for definition in _THEME_DEFS
               if definition['id'] not in REMOVED_THEME_IDS]


def _theme_variant(base_id, theme_id, name, desc, overrides):
    """Create a coherent preset from a vetted palette and a small color set."""
    base = next((item for item in _THEME_DEFS if item['id'] == base_id), None)
    if base is None:
        raise ValueError('base theme {!r} is missing'.format(base_id))
    variant = dict(base)
    variant.update(overrides)
    variant.update({'id': theme_id, 'name': name, 'desc': desc})
    return variant


_THEME_DEFS.extend([
    _theme_variant('github-dark', 'aurora-night', 'شفق زمردي',
                   'داكن عميق بلمسات خضراء ومائية', {
        'dark': True,
        'bg': '#101A1C', 'card': '#19282A', 'card_2': '#23383A',
        'field': '#0B1517', 'border': '#395254',
        'text': '#EDF6F2', 'text_muted': '#B2C8C0',
        'accent': '#72D1B9', 'accent_hover': '#4EB49C',
        'accent_fill': '#287668', 'accent_fill_hover': '#205F54',
        'accent_text': '#8DE0CB', 'on_accent': '#FFFFFF',
        'on_fill': '#101917', 'accent_ink': '#101917',
        'success': '#70C994', 'success_fill': '#70C994',
        'danger': '#E96C7B', 'danger_fill': '#E96C7B',
        'danger_ink': '#FFA2AE', 'warning': '#E8BD6A',
        'sand_fill': '#D2B894', 'sand_fill_hover': '#C2A77F',
        'kind_internal': '#D2B894', 'kind_internal_ink': '#D2B894',
        'kind_sdcard': '#34B9C8', 'kind_sdcard_ink': '#92E3E9',
        'kind_usb': '#86AFCB', 'kind_usb_ink': '#B1D0E5',
        'kind_card': '#9DB77B', 'kind_card_ink': '#C4D6A0',
        'scrim': 'rgba(4,10,11,0.90)', 'sheet': 'rgba(22,37,39,0.97)',
        'shadow': '#000000',
    }),
    _theme_variant('github-dark', 'cobalt-evening', 'كوبالت هادئ',
                   'أزرق ليلي متوازن مع تفاصيل سماوية', {
        'dark': True,
        'bg': '#171B2B', 'card': '#22283A', 'card_2': '#303950',
        'field': '#101522', 'border': '#46516D',
        'text': '#F0F3FA', 'text_muted': '#BAC3D4',
        'accent': '#9FB7F2', 'accent_hover': '#829BDA',
        'accent_fill': '#53679C', 'accent_fill_hover': '#43547F',
        'accent_text': '#B8C8F3', 'on_accent': '#FFFFFF',
        'on_fill': '#111827', 'accent_ink': '#111827',
        'success': '#80C99A', 'success_fill': '#80C99A',
        'danger': '#EF7180', 'danger_fill': '#EF7180',
        'danger_ink': '#FFA6B2', 'warning': '#EBC16B',
        'sand_fill': '#D3BD9A', 'sand_fill_hover': '#C4AA82',
        'kind_internal': '#D3BD9A', 'kind_internal_ink': '#D3BD9A',
        'kind_sdcard': '#3ABBCB', 'kind_sdcard_ink': '#95E0EA',
        'kind_usb': '#86AEDD', 'kind_usb_ink': '#B2CDED',
        'kind_card': '#A6B985', 'kind_card_ink': '#C4D2A6',
        'scrim': 'rgba(8,10,19,0.90)', 'sheet': 'rgba(30,35,52,0.97)',
        'shadow': '#000000',
    }),
    _theme_variant('graphite', 'sage-evening', 'حجر ومريمية',
                   'درجة متوسطة هادئة بين الفحمي والأخضر', {
        'dark': True,
        'bg': '#29332E', 'card': '#344039', 'card_2': '#414F47',
        'field': '#202923', 'border': '#5A6A60',
        'text': '#F1F6F1', 'text_muted': '#C2CEC4',
        'accent': '#C4D991', 'accent_hover': '#AEC77A',
        'accent_fill': '#596E3D', 'accent_fill_hover': '#485B32',
        'accent_text': '#D6E6AF', 'on_accent': '#FFFFFF',
        'on_fill': '#172016', 'accent_ink': '#172016',
        'success': '#8BCB91', 'success_fill': '#8BCB91',
        'danger': '#ED8178', 'danger_fill': '#ED8178',
        'danger_ink': '#FFB1A9', 'warning': '#E4BE70',
        'sand_fill': '#D7C19C', 'sand_fill_hover': '#C6AC81',
        'kind_internal': '#D7C19C', 'kind_internal_ink': '#D7C19C',
        'kind_sdcard': '#43BDC9', 'kind_sdcard_ink': '#9CE3E8',
        'kind_usb': '#8AAFD0', 'kind_usb_ink': '#B8D0E7',
        'kind_card': '#A8BD83', 'kind_card_ink': '#C9D8A9',
        'scrim': 'rgba(16,23,19,0.84)', 'sheet': 'rgba(45,57,49,0.97)',
        'shadow': '#0A100C',
    }),
    _theme_variant('github-light', 'cloud-azure', 'غيمة زرقاء',
                   'خلفية فاتحة باردة ولمسات زرقاء واضحة', {
        'dark': False,
        'bg': '#EAF2F9', 'card': '#FBFDFF', 'card_2': '#DCE8F4',
        'field': '#F4F8FC', 'border': '#C5D5E5',
        'text': '#1D3044', 'text_muted': '#526A81',
        'accent': '#4A83B8', 'accent_hover': '#376F9F',
        'accent_fill': '#2D6598', 'accent_fill_hover': '#24547F',
        'accent_text': '#286493', 'on_accent': '#FFFFFF',
        'on_fill': '#FFFFFF', 'accent_ink': '#102030',
        'scrim': 'rgba(30,45,61,0.54)', 'sheet': 'rgba(249,252,255,0.97)',
        'shadow': '#718294',
    }),
])

THEMES = {}
_THEME_FAILURES = []
for _defn in _THEME_DEFS:
    _tokens = build_tokens(_defn)
    for _fail in check_contrast(_defn):
        _THEME_FAILURES.append('{}: {}={:.2f} (<{})'.format(_defn['id'], *_fail))
    for _fail in check_kind_hues(_defn):
        _THEME_FAILURES.append('{}: {}={}deg (<{}deg)'.format(_defn['id'], *_fail))
    THEMES[_defn['id']] = {
        'id': _defn['id'],
        'name': _defn['name'],
        'desc': _defn['desc'],
        'dark': bool(_defn['dark']),
        'swatches': [
            _defn['bg'], _defn['card_2'], _defn['accent'],
            _defn['kind_internal'], _defn['kind_sdcard'], _defn['text'],
        ],
        'tokens': _tokens,
    }

DEFAULT_THEME = 'coffee-blues'

if _THEME_FAILURES:
    raise ValueError('theme failures: ' + '; '.join(_THEME_FAILURES))


def theme_ids():
    return list(THEMES.keys())


def get_theme(theme_id):
    """Theme dict, falling back to the default for an unknown id."""
    return THEMES.get(theme_id) or THEMES[DEFAULT_THEME]


def theme_tokens(theme_id):
    return get_theme(theme_id)['tokens']


def theme_css_pairs(theme_id):
    """[(name, value)] in TOKEN_ORDER, for rendering the <style> block."""
    tokens = theme_tokens(theme_id)
    return [(name, tokens[name]) for name in TOKEN_ORDER]


def theme_color(theme_id):
    """The Android status-bar colour: always the page background."""
    return theme_tokens(theme_id)['--bg']


def catalog():
    """Everything the settings page and the JS need, in one list."""
    return [THEMES[i] for i in THEMES]


if __name__ == '__main__':  # pragma: no cover - manual report
    print('{:<14} {:<12} {:>7}  {:<22} {:>6}'.format(
        'theme', 'text/card', 'min', 'worst check', 'int/sd'))
    for theme_id in THEMES:
        defn = next(d for d in _THEME_DEFS if d['id'] == theme_id)
        ratios = [(label, contrast_ratio(defn[f], defn[b]))
                  for label, f, b, _floor in CONTRAST_RULES]
        worst_label, worst = min(ratios, key=lambda kv: kv[1])
        print('{:<14} {:<12} {:>7.2f}  {:<22} {:>5.0f}°'.format(
            theme_id,
            '{:.2f}'.format(contrast_ratio(defn['text'], defn['card'])),
            worst, worst_label,
            hue_gap(defn['kind_internal'], defn['kind_sdcard'])))
    print('\n{} themes, {} tokens each, all contrast and hue rules satisfied.'.format(
        len(THEMES), len(TOKEN_ORDER)))
