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
  'name': 'ليل فيروزي',
  'desc': 'أخضر مزرق أعمق، تباين أعلى',
  'dark': True,
  'bg': '#0a1b18',
  'card': '#112925',
  'card_2': '#173430',
  'field': '#061312',
  'border': '#1f4741',
  'text': '#e7f4ee',
  'text_muted': '#96aea4',
  'accent': '#46b68d',
  'accent_hover': '#379471',
  'accent_fill': '#257c5a',
  'accent_fill_hover': '#1f6c4e',
  'accent_text': '#83d5b6',
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
  'name': 'سليت ورمل',
   'desc': 'سليت بارد وتفاصيل فيروزية هادئة',
  'dark': True,
   'bg': '#1b2230',
   'card': '#252e3e',
   'card_2': '#303b4d',
   'field': '#161d29',
   'border': '#3b4b60',
   'text': '#eef2f5',
   'text_muted': '#aab8c5',
   'accent': '#83b5c7',
   'accent_hover': '#69a1b5',
   'accent_fill': '#477a8c',
   'accent_fill_hover': '#386474',
   'accent_text': '#a7d0dd',
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
  'name': 'بستان زيتوني',
   'desc': 'زيتوني عميق بلمسات خضراء متوازنة',
  'dark': True,
   'bg': '#1c2118',
   'card': '#282f22',
   'card_2': '#35402c',
   'field': '#171c13',
   'border': '#46543b',
   'text': '#f1f2e8',
   'text_muted': '#b0b9a5',
   'accent': '#bdc77b',
   'accent_hover': '#a8b56a',
   'accent_fill': '#647341',
   'accent_fill_hover': '#546334',
   'accent_text': '#d1da9b',
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
  'name': 'طين وورد',
   'desc': 'طين دافئ ولمسات مرجانية ناعمة',
  'dark': True,
   'bg': '#241b1b',
   'card': '#302423',
   'card_2': '#3e302f',
   'field': '#1b1414',
   'border': '#51413e',
   'text': '#f3ece8',
   'text_muted': '#bda9a4',
   'accent': '#e4a28e',
   'accent_hover': '#d58c77',
   'accent_fill': '#a45e50',
   'accent_fill_hover': '#8e4e42',
   'accent_text': '#f0baac',
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
  'name': 'جرافيت',
   'desc': 'فحم محايد مع تفاصيل كهرمانية دافئة',
  'dark': True,
   'bg': '#191d22',
   'card': '#232a30',
   'card_2': '#303942',
   'field': '#13171b',
   'border': '#3b464f',
   'text': '#f0f3f5',
   'text_muted': '#aeb8bf',
   'accent': '#dbad72',
   'accent_hover': '#c89658',
   'accent_fill': '#8f6334',
   'accent_fill_hover': '#775229',
   'accent_text': '#e9c18f',
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
  'name': 'ورق',
   'desc': 'ورق عاجي نظيف وحبر فيروزي هادئ',
  'dark': False,
   'bg': '#f4f0e7',
   'card': '#fffdf8',
   'card_2': '#ebe5d8',
   'field': '#faf7f0',
   'border': '#ddd4c2',
   'text': '#282a31',
   'text_muted': '#625f58',
   'accent': '#4c8791',
   'accent_hover': '#3d737d',
   'accent_fill': '#356a75',
   'accent_fill_hover': '#2d5b65',
   'accent_text': '#285a64',
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
  'name': 'حجر رملي',
   'desc': 'رملي فاتح ولمسات طينية متزنة',
  'dark': False,
   'bg': '#f5eee9',
   'card': '#fffaf6',
   'card_2': '#eedfd4',
   'field': '#faf3ed',
   'border': '#e0cabe',
   'text': '#322724',
   'text_muted': '#705d56',
   'accent': '#b97961',
   'accent_hover': '#a7654f',
   'accent_fill': '#995d48',
   'accent_fill_hover': '#804d3d',
   'accent_text': '#844936',
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
  'name': 'نايت أوكسفورد',
   'desc': 'أزرق ليلي هادئ ولمسات نيليّة',
  'dark': True,
   'bg': '#151b2b',
   'card': '#202a3b',
   'card_2': '#2c394d',
   'field': '#0f1522',
   'border': '#394b63',
   'text': '#edf1f6',
   'text_muted': '#adb9ca',
   'accent': '#8ba6e8',
   'accent_hover': '#7693d7',
   'accent_fill': '#4c639a',
   'accent_fill_hover': '#3c507e',
   'accent_text': '#b3c6f3',
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
  'name': 'فحم عميق',
   'desc': 'فحم هادئ مع لمسات خضراء مزرقة',
  'dark': True,
   'bg': '#171b1d',
   'card': '#22292b',
   'card_2': '#2f3839',
   'field': '#121617',
   'border': '#414d4e',
   'text': '#edf3f2',
   'text_muted': '#aab9b9',
   'accent': '#7fc5b7',
   'accent_hover': '#69ac9f',
   'accent_fill': '#397d73',
   'accent_fill_hover': '#2e6b62',
   'accent_text': '#a4ddd3',
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
  'name': 'فجر صافٍ',
   'desc': 'ضوء بارد ودرجات أزرق هادئة',
  'dark': False,
   'bg': '#f0f4f8',
   'card': '#ffffff',
   'card_2': '#e4ebf2',
   'field': '#f8fafc',
   'border': '#d4dee7',
   'text': '#222b36',
   'text_muted': '#5c6976',
   'accent': '#7189bd',
   'accent_hover': '#5e76aa',
   'accent_fill': '#4f699e',
   'accent_fill_hover': '#405783',
   'accent_text': '#3e5a92',
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
  'name': 'زفير اللؤلؤ',
   'desc': 'لؤلؤ بارد مع فيروزي واضح',
  'dark': False,
   'bg': '#eff5f5',
   'card': '#fcfefe',
   'card_2': '#e2ecec',
   'field': '#f6fafa',
   'border': '#cddcda',
   'text': '#203136',
   'text_muted': '#587075',
   'accent': '#4f9293',
   'accent_hover': '#3d7c7e',
   'accent_fill': '#347477',
   'accent_fill_hover': '#286164',
   'accent_text': '#286367',
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
  'name': 'GitHub داكن',
   'desc': 'فحم بارد مع أزرق رابط واضح',
  'dark': True,
   'bg': '#0f141b',
   'card': '#171e27',
   'card_2': '#202a35',
   'field': '#0b1016',
   'border': '#303d4a',
   'text': '#e8edf3',
   'text_muted': '#a3afbc',
   'accent': '#78a9ef',
   'accent_hover': '#6597df',
   'accent_fill': '#356da8',
   'accent_fill_hover': '#295b91',
   'accent_text': '#9fc5f7',
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
  'name': 'GitHub فاتح',
   'desc': 'فاتح محايد مع أزرق موثوق',
  'dark': False,
   'bg': '#f5f7f9',
   'card': '#ffffff',
   'card_2': '#e9eef2',
   'field': '#f7f9fb',
   'border': '#d5dde5',
   'text': '#1f2328',
   'text_muted': '#59636e',
   'accent': '#4b7bb8',
   'accent_hover': '#38689f',
   'accent_fill': '#2864a6',
   'accent_fill_hover': '#20538d',
   'accent_text': '#24568d',
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
  'name': 'ضباب الغابة',
   'desc': 'أخضر غابي فاتح وحبر صنوبرى',
  'dark': False,
   'bg': '#eef4ef',
   'card': '#f9fcf9',
   'card_2': '#e1ece2',
   'field': '#f5f9f5',
   'border': '#c9d8cb',
   'text': '#1f3024',
   'text_muted': '#526451',
   'accent': '#6f9877',
   'accent_hover': '#5b8465',
   'accent_fill': '#456d52',
   'accent_fill_hover': '#395a45',
   'accent_text': '#355d42',
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
  'name': 'غروب الخوخ',
   'desc': 'بنفسجي مسائي ولمسات لافندر ناعمة',
  'dark': True,
   'bg': '#201b2b',
   'card': '#2a2437',
   'card_2': '#383047',
   'field': '#17131f',
   'border': '#4a3d5d',
   'text': '#f0ebf6',
   'text_muted': '#b8adc7',
   'accent': '#c29bd7',
   'accent_hover': '#ac82c4',
   'accent_fill': '#78558f',
   'accent_fill_hover': '#644477',
   'accent_text': '#ddc2ea',
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
            _defn['kind_sdcard'], _defn['text'],
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
