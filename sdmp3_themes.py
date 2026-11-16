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
    '--radius',
)


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
        '--radius': defn.get('radius', '16px'),
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

_THEME_DEFS = [
    {
        'id': 'coffee-blues',
        'name': 'قهوة ورمال',
        'desc': 'الأصلي: رمال دافئة على سليت بارد',
        'dark': True,
        'bg': '#17222e', 'card': '#1f2c3a', 'card_2': '#293847',
        'field': '#141d27', 'border': '#2b3e52',
        'text': '#ebe1d6', 'text_muted': '#aca19a',
        'accent': '#39918c', 'accent_hover': '#2e7a75',
        'accent_fill': '#2e7a75', 'accent_fill_hover': '#276a66',
        'accent_text': '#7fc4bf', 'on_accent': '#f7f4f0',
        'on_fill': '#151e28', 'accent_ink': '#151e28',
        'success': '#7fbf93', 'success_fill': '#7fbf93',
        'danger': '#f43f5e', 'danger_fill': '#f43f5e', 'danger_ink': '#fc8b9b',
        'warning': '#f59e0b',
        'sand_fill': '#d0b49f', 'sand_fill_hover': '#c4a88f',
        'kind_internal': '#d0b49f', 'kind_internal_ink': '#d0b49f',
        'kind_sdcard': '#d98b63', 'kind_sdcard_ink': '#dda88c',
        'kind_usb': '#8fa9c4', 'kind_usb_ink': '#a8bfd6',
        'kind_card': '#a3b18a', 'kind_card_ink': '#b7c39f',
        'scrim': 'rgba(8,12,16,0.88)', 'sheet': 'rgba(24,33,44,0.97)',
        'shadow': '#000000',
    },
    {
        'id': 'midnight-teal',
        'name': 'ليل فيروزي',
        'desc': 'أخضر مزرق أعمق، تباين أعلى',
        'dark': True,
        'bg': '#0b1a1c', 'card': '#12282b', 'card_2': '#193438',
        'field': '#071315', 'border': '#21454a',
        'text': '#e6f4f1', 'text_muted': '#95aeaa',
        'accent': '#45b3a8', 'accent_hover': '#369389',
        'accent_fill': '#257a70', 'accent_fill_hover': '#1f6a61',
        'accent_text': '#7fd4ca', 'on_accent': '#f2fffd',
        'on_fill': '#08191b', 'accent_ink': '#08191b',
        'success': '#74c79b', 'success_fill': '#74c79b',
        'danger': '#f2607c', 'danger_fill': '#f2607c', 'danger_ink': '#ffa1b2',
        'warning': '#e9b44c',
        'sand_fill': '#d8c3a5', 'sand_fill_hover': '#c9b192',
        'kind_internal': '#d8c3a5', 'kind_internal_ink': '#d8c3a5',
        'kind_sdcard': '#e2926a', 'kind_sdcard_ink': '#f0b28c',
        'kind_usb': '#86b0d8', 'kind_usb_ink': '#a8c8ea',
        'kind_card': '#a4b981', 'kind_card_ink': '#bfd29c',
        'scrim': 'rgba(4,12,13,0.90)', 'sheet': 'rgba(16,36,38,0.97)',
        'shadow': '#000000',
    },
    {
        'id': 'slate-sand',
        'name': 'سليت ورمل',
        'desc': 'سليت رمادي هادئ ولمسة رملية',
        'dark': True,
        'bg': '#1a2431', 'card': '#243040', 'card_2': '#2e3c4e',
        'field': '#151e29', 'border': '#36465c',
        'text': '#ede8e1', 'text_muted': '#a9a59f',
        'accent': '#d0b49f', 'accent_hover': '#c0a189',
        'accent_fill': '#c9a98f', 'accent_fill_hover': '#b8967c',
        'accent_text': '#e6cbb4', 'on_accent': '#1a222c',
        'on_fill': '#1a222c', 'accent_ink': '#1a222c',
        'success': '#8ccfa6', 'success_fill': '#8ccfa6',
        'danger': '#ef6b7f', 'danger_fill': '#ef6b7f', 'danger_ink': '#ffa4b2',
        'warning': '#e8b45a',
        'sand_fill': '#d0b49f', 'sand_fill_hover': '#c4a88f',
        'kind_internal': '#d0b49f', 'kind_internal_ink': '#d6bfae',
        'kind_sdcard': '#dfa070', 'kind_sdcard_ink': '#f0c09a',
        'kind_usb': '#88b0d8', 'kind_usb_ink': '#b3cfe9',
        'kind_card': '#a8bb8c', 'kind_card_ink': '#c3d3aa',
        'scrim': 'rgba(10,16,24,0.88)', 'sheet': 'rgba(27,36,48,0.97)',
        'shadow': '#05080d',
    },
    {
        'id': 'olive-grove',
        'name': 'بستان زيتوني',
        'desc': 'أخضر زيتوني ترابي',
        'dark': True,
        'bg': '#161b13', 'card': '#20271c', 'card_2': '#293324',
        'field': '#11150e', 'border': '#37412c',
        'text': '#eceae0', 'text_muted': '#a9a896',
        'accent': '#9bb06a', 'accent_hover': '#899d59',
        'accent_fill': '#77904a', 'accent_fill_hover': '#677d40',
        'accent_text': '#bcd08c', 'on_accent': '#0f150a',
        'on_fill': '#0f150a', 'accent_ink': '#0f150a',
        'success': '#8fca8f', 'success_fill': '#8fca8f',
        'danger': '#e8705f', 'danger_fill': '#e8705f', 'danger_ink': '#ffa99a',
        'warning': '#dcae4a',
        'sand_fill': '#cdb992', 'sand_fill_hover': '#bfa87f',
        'kind_internal': '#cdb992', 'kind_internal_ink': '#cdb992',
        'kind_sdcard': '#da9569', 'kind_sdcard_ink': '#eeb48e',
        'kind_usb': '#87aec9', 'kind_usb_ink': '#aacbe4',
        'kind_card': '#a0b87c', 'kind_card_ink': '#bdd49a',
        'scrim': 'rgba(7,10,5,0.90)', 'sheet': 'rgba(27,34,23,0.97)',
        'shadow': '#000000',
    },
    {
        'id': 'clay-rose',
        'name': 'طين وورد',
        'desc': 'طيني دافئ بلمسة وردية',
        'dark': True,
        'bg': '#211a1a', 'card': '#2c2222', 'card_2': '#382b2b',
        'field': '#191313', 'border': '#463430',
        'text': '#f1e7e1', 'text_muted': '#b5a49d',
        'accent': '#d98b7a', 'accent_hover': '#c47a6a',
        'accent_fill': '#bd7a65', 'accent_fill_hover': '#a96955',
        'accent_text': '#eda795', 'on_accent': '#1d1210',
        'on_fill': '#1d1210', 'accent_ink': '#1d1210',
        'success': '#8fc795', 'success_fill': '#8fc795',
        'danger': '#f2687c', 'danger_fill': '#f2687c', 'danger_ink': '#ffa6b3',
        'warning': '#e6b05a',
        'sand_fill': '#d5b394', 'sand_fill_hover': '#c6a382',
        'kind_internal': '#d5b394', 'kind_internal_ink': '#d5b394',
        'kind_sdcard': '#de9066', 'kind_sdcard_ink': '#f2ad86',
        'kind_usb': '#8caac6', 'kind_usb_ink': '#b0c8de',
        'kind_card': '#a7b383', 'kind_card_ink': '#c4cfa2',
        'scrim': 'rgba(14,9,9,0.89)', 'sheet': 'rgba(38,29,29,0.97)',
        'shadow': '#0a0505',
    },
    {
        'id': 'graphite',
        'name': 'جرافيت',
        'desc': 'رمادي محايد وكهرماني واحد',
        'dark': True,
        'bg': '#16181a', 'card': '#1f2225', 'card_2': '#282c30',
        'field': '#111315', 'border': '#343a40',
        'text': '#eaeae7', 'text_muted': '#a5a7a6',
        'accent': '#e0a34b', 'accent_hover': '#cd8f38',
        'accent_fill': '#b47a24', 'accent_fill_hover': '#9c6919',
        'accent_text': '#f0c07f', 'on_accent': '#16181a',
        'on_fill': '#16181a', 'accent_ink': '#16181a',
        'success': '#8ccfa6', 'success_fill': '#8ccfa6',
        'danger': '#ef6b7f', 'danger_fill': '#ef6b7f', 'danger_ink': '#ffa4b2',
        'warning': '#e8b45a',
        'sand_fill': '#d8cbb8', 'sand_fill_hover': '#c8b9a4',
        'kind_internal': '#d8cbb8', 'kind_internal_ink': '#d8cbb8',
        'kind_sdcard': '#e0a06a', 'kind_sdcard_ink': '#f3bd8c',
        'kind_usb': '#8fb4cf', 'kind_usb_ink': '#b3d0e6',
        'kind_card': '#aab98c', 'kind_card_ink': '#c6d4a8',
        'scrim': 'rgba(6,7,8,0.90)', 'sheet': 'rgba(26,29,32,0.97)',
        'shadow': '#000000',
    },
    {
        'id': 'paper',
        'name': 'ورق',
        'desc': 'فاتح: ورق دافئ وحبر فيروزي',
        'dark': False,
        'bg': '#f4efe6', 'card': '#fffdf8', 'card_2': '#f0eae0',
        'field': '#fbf8f2', 'border': '#ddd4c4',
        'text': '#222831', 'text_muted': '#5c6673',
        'accent': '#2e7a75', 'accent_hover': '#276862',
        'accent_fill': '#2e7a75', 'accent_fill_hover': '#276862',
        'accent_text': '#1f625d', 'on_accent': '#ffffff',
        'on_fill': '#ffffff', 'accent_ink': '#222831',
        'success': '#2f7d4f', 'success_fill': '#2f7d4f',
        'danger': '#c8334b', 'danger_fill': '#c8334b', 'danger_ink': '#b0243a',
        'warning': '#8a5a06',
        'sand_fill': '#96603a', 'sand_fill_hover': '#84522f',
        'kind_internal': '#8a6a48', 'kind_internal_ink': '#7c5c3c',
        'kind_sdcard': '#b3542a', 'kind_sdcard_ink': '#9c4520',
        'kind_usb': '#3f6b96', 'kind_usb_ink': '#35597e',
        'kind_card': '#5f7540', 'kind_card_ink': '#546838',
        'scrim': 'rgba(34,30,25,0.55)', 'sheet': 'rgba(255,253,248,0.97)',
        'shadow': '#7a6a55',
    },
    {
        'id': 'sandstone',
        'name': 'حجر رملي',
        'desc': 'فاتح: رملي وحبر طيني',
        'dark': False,
        'bg': '#f2eade', 'card': '#fbf5ec', 'card_2': '#ece2d3',
        'field': '#f8f1e6', 'border': '#d9cab3',
        'text': '#2b2320', 'text_muted': '#6a5f55',
        'accent': '#a15a3c', 'accent_hover': '#8c4c31',
        'accent_fill': '#9a5738', 'accent_fill_hover': '#874a2f',
        'accent_text': '#8a4f38', 'on_accent': '#fff8f3',
        'on_fill': '#fff8f3', 'accent_ink': '#2b2320',
        'success': '#2f7d4f', 'success_fill': '#2f7d4f',
        'danger': '#b83349', 'danger_fill': '#b83349', 'danger_ink': '#a12539',
        'warning': '#7f5305',
        'sand_fill': '#8f6135', 'sand_fill_hover': '#7e552d',
        'kind_internal': '#8a6a48', 'kind_internal_ink': '#7a5b3a',
        'kind_sdcard': '#a8491f', 'kind_sdcard_ink': '#92401a',
        'kind_usb': '#3b6289', 'kind_usb_ink': '#325271',
        'kind_card': '#5b7038', 'kind_card_ink': '#516431',
        'scrim': 'rgba(38,30,22,0.55)', 'sheet': 'rgba(251,245,236,0.97)',
        'shadow': '#8a7458',
    },
    {
        'id': 'night-oxford',
        'name': 'نايت أوكسفورد',
        'desc': 'داكن: أزرق ليلي عميق ولمسة عنبرية',
        'dark': True,
        'bg': '#0f172a', 'card': '#1e293b', 'card_2': '#2d3748',
        'field': '#0b1120', 'border': '#334155',
        'text': '#f8fafc', 'text_muted': '#cbd5e1',
        'accent': '#f59e0b', 'accent_hover': '#d97706',
        'accent_fill': '#78350f', 'accent_fill_hover': '#451a03',
        'accent_text': '#fcd34d', 'on_accent': '#ffffff',
        'on_fill': '#0f172a', 'accent_ink': '#0f172a',
        'success': '#34d399', 'success_fill': '#34d399',
        'danger': '#f87171', 'danger_fill': '#f87171', 'danger_ink': '#fca5a5',
        'warning': '#fbbf24',
        'sand_fill': '#fbbf24', 'sand_fill_hover': '#d97706',
        'kind_internal': '#fbbf24', 'kind_internal_ink': '#fbbf24',
        'kind_sdcard': '#f59e0b', 'kind_sdcard_ink': '#fdba74',
        'kind_usb': '#60a5fa', 'kind_usb_ink': '#93c5fd',
        'kind_card': '#a3e635', 'kind_card_ink': '#ccff7d',
        'scrim': 'rgba(8,12,20,0.90)', 'sheet': 'rgba(24,35,50,0.97)',
        'shadow': '#000000',
    },
    {
        'id': 'deep-charcoal',
        'name': 'فحم عميق',
        'desc': 'داكن: فحم محايد مع لهجة فيروزية',
        'dark': True,
        'bg': '#121212', 'card': '#1d1d1d', 'card_2': '#292929',
        'field': '#0d0d0d', 'border': '#3a3a3a',
        'text': '#eaeaea', 'text_muted': '#a0a0a0',
        'accent': '#2dd4bf', 'accent_hover': '#14b8a6',
        'accent_fill': '#064e3b', 'accent_fill_hover': '#115e59',
        'accent_text': '#67e8f9', 'on_accent': '#ffffff',
        'on_fill': '#121212', 'accent_ink': '#121212',
        'success': '#4ade80', 'success_fill': '#4ade80',
        'danger': '#fb7185', 'danger_fill': '#fb7185', 'danger_ink': '#fda4af',
        'warning': '#fbbf24',
        'sand_fill': '#2dd4bf', 'sand_fill_hover': '#14b8a6',
        'kind_internal': '#0f766e', 'kind_internal_ink': '#eaeaea',
        'kind_sdcard': '#2dd4bf', 'kind_sdcard_ink': '#eaeaea',
        'kind_usb': '#60a5fa', 'kind_usb_ink': '#eaeaea',
        'kind_card': '#4ade80', 'kind_card_ink': '#eaeaea',
        'scrim': 'rgba(4,4,4,0.90)', 'sheet': 'rgba(24,24,24,0.97)',
        'shadow': '#000000',
    },
    {
        'id': 'clear-dawn',
        'name': 'فجر صافٍ',
        'desc': 'فاتح: كريم دافئ وحبر أزرق داكن',
        'dark': False,
        'bg': '#fdfbf7', 'card': '#ffffff', 'card_2': '#f5f1e8',
        'field': '#faf7f1', 'border': '#e7dcc9',
        'text': '#3f3f3f', 'text_muted': '#6e6658',
        'accent': '#2563eb', 'accent_hover': '#1d4ed8',
        'accent_fill': '#1d4ed8', 'accent_fill_hover': '#1e40af',
        'accent_text': '#1e3a8a', 'on_accent': '#ffffff',
        'on_fill': '#0f172a', 'accent_ink': '#3f3f3f',
        'success': '#14532d', 'success_fill': '#bbf7d0',
        'danger': '#7f1d1d', 'danger_fill': '#fee2e2', 'danger_ink': '#7f1d1d',
        'warning': '#b45309',
        'sand_fill': '#fef3c7', 'sand_fill_hover': '#d97706',
        'kind_internal': '#d97706', 'kind_internal_ink': '#92400e',
        'kind_sdcard': '#c2410c', 'kind_sdcard_ink': '#b45309',
        'kind_usb': '#1d4ed8', 'kind_usb_ink': '#1d4ed8',
        'kind_card': '#14532d', 'kind_card_ink': '#14532d',
        'scrim': 'rgba(63,63,63,0.50)', 'sheet': 'rgba(253,251,247,0.97)',
        'shadow': '#a8a292',
    },
    {
        'id': 'pearl-breath',
        'name': 'زفير اللؤلؤ',
        'desc': 'فاتح: رمادي أزرق فاتح وحبر فيروزي خافت',
        'dark': False,
        'bg': '#f4f6f8', 'card': '#ffffff', 'card_2': '#e9edf1',
        'field': '#fbfcfd', 'border': '#d7dde2',
        'text': '#22303c', 'text_muted': '#5b6b78',
        'accent': '#0e7490', 'accent_hover': '#0b5f76',
        'accent_fill': '#0b5f76', 'accent_fill_hover': '#094c5e',
        'accent_text': '#0d5a72', 'on_accent': '#ffffff',
        'on_fill': '#0f172a', 'accent_ink': '#22303c',
        'success': '#14532d', 'success_fill': '#bbf7d0',
        'danger': '#7f1d1d', 'danger_fill': '#fee2e2', 'danger_ink': '#7f1d1d',
        'warning': '#7f5305',
        'sand_fill': '#fef3c7', 'sand_fill_hover': '#8f6135',
        'kind_internal': '#8f6135', 'kind_internal_ink': '#7e552d',
        'kind_sdcard': '#0e7490', 'kind_sdcard_ink': '#0e7490',
        'kind_usb': '#2f6f9f', 'kind_usb_ink': '#2f6f9f',
        'kind_card': '#14532d', 'kind_card_ink': '#14532d',
        'scrim': 'rgba(34,48,60,0.50)', 'sheet': 'rgba(244,246,248,0.97)',
        'shadow': '#b9c4ce',
    },
]

THEMES = {}
_THEME_FAILURES = []
for _defn in _THEME_DEFS:
    _tokens = build_tokens(_defn)
    for _fail in check_contrast(_defn):
        _THEME_FAILURES.append('{}: {}={:.2f} (<{})'.format(_defn['id'], *_fail))
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
    raise ValueError('theme contrast failures: ' + '; '.join(_THEME_FAILURES))


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
    print('{:<14} {:<12} {:>7}  {}'.format('theme', 'text/card', 'min', 'checks'))
    for theme_id in THEMES:
        defn = next(d for d in _THEME_DEFS if d['id'] == theme_id)
        ratios = [(label, contrast_ratio(defn[f], defn[b]))
                  for label, f, b, _floor in CONTRAST_RULES]
        worst_label, worst = min(ratios, key=lambda kv: kv[1])
        print('{:<14} {:<12} {:>7.2f}  {}'.format(
            theme_id,
            '{:.2f}'.format(contrast_ratio(defn['text'], defn['card'])),
            worst, worst_label))
    print('\n{} themes, {} tokens each, all contrast rules satisfied.'.format(
        len(THEMES), len(TOKEN_ORDER)))
