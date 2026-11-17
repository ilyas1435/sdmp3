"""Download the self-hosted Arabic font subsets used by sdmp3_fonts.py.

Developer tool, not part of the app: run it once after adding a font to the
registry, and commit the resulting files in ``static/fonts/``.

    python fetch_fonts.py            # download missing/stale subsets
    python fetch_fonts.py --force    # re-download everything

It asks Google Fonts for the variable 400..700 cut of every family that has no
``weights`` key, and for the explicit weights of every family that does (IBM
Plex Sans Arabic is only published as static weights). Only the `arabic` and
`latin` subsets are kept (the two the UI needs), their unicode ranges are
verified against the constants in ``sdmp3_fonts.py``, and the files are written
with the names ``sdmp3_fonts.font_assets()`` expects:

    static/fonts/<font-id>-arabic.woff2            (variable family)
    static/fonts/<font-id>-<weight>-arabic.woff2   (static-weight family)

Nothing else in the app depends on this script at runtime.
"""

import argparse
import os
import re
import sys
import urllib.request

import sdmp3_fonts as F

CSS_URL = ('https://fonts.googleapis.com/css2?family={family}:wght@{axis}'
           '&display=swap')
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, F.FONT_DIR)

FACE_RE = re.compile(r'/\*\s*([\w-]+)\s*\*/\s*@font-face\s*\{(.*?)\}', re.S)


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return data if binary else data.decode('utf-8')


def css_url(font):
    """The Google CSS URL for a family: one weight range or the explicit list."""
    family = font['family'].replace(' ', '+')
    weights = font.get('weights')
    axis = ';'.join(str(int(w)) for w in weights) if weights else '400..700'
    return CSS_URL.format(family=family, axis=axis)


def parse_faces(css):
    """[{'subset','weight','url','range'}] for every labelled @font-face block.

    Google labels each block with a `/* arabic */` comment and states the weight
    inside it (`font-weight: 600;` for a static file, `font-weight: 400 700;`
    for a variable one), so the weight must be read from the body — a family
    with four weights repeats the same `/* arabic */` label four times.
    """
    faces = []
    for label, body in FACE_RE.findall(css):
        url = re.search(r'url\((https://[^)]+)\)', body)
        rng = re.search(r'unicode-range:\s*([^;]+);', body)
        weight = re.search(r'font-weight:\s*([^;]+);', body)
        if url and rng and weight:
            faces.append({
                'subset': label,
                'weight': ' '.join(weight.group(1).split()),
                'url': url.group(1).strip(),
                'range': ' '.join(rng.group(1).split()),
            })
    return faces


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true',
                        help='re-download files that already exist')
    parser.add_argument('--font', action='append', default=None,
                        help='only this font id (repeatable)')
    args = parser.parse_args(argv)

    os.makedirs(OUT_DIR, exist_ok=True)
    fonts = [f for f in F.FONTS if not args.font or f['id'] in args.font]
    if not fonts:
        raise SystemExit('no matching font id: {}'.format(args.font))

    expected = {'arabic': F.ARABIC_RANGE, 'latin': F.LATIN_RANGE}
    total = 0
    for font in fonts:
        css = fetch(css_url(font))
        faces = parse_faces(css)
        by_key = {(face['subset'], face['weight']): face for face in faces}
        for weight, subset, name in F.font_assets(font):
            want = '400 700' if weight is None else str(int(weight))
            face = by_key.get((subset, want))
            if not face:
                raise SystemExit('{}: no {} {} subset in the Google CSS'.format(
                    font['id'], want, subset))
            if ' '.join(face['range'].split()) != ' '.join(expected[subset].split()):
                raise SystemExit('{}: {} unicode-range drifted from sdmp3_fonts.py'
                                 '\n  google: {}\n  module: {}'.format(
                                     font['id'], subset, face['range'],
                                     expected[subset]))
            path = os.path.join(OUT_DIR, name)
            if os.path.isfile(path) and not args.force:
                total += os.path.getsize(path)
                print('  {:<38} {} (kept)'.format(name, os.path.getsize(path)))
                continue
            data = fetch(face['url'], binary=True)
            if len(data) < 1000:
                raise SystemExit('{}: download looks empty'.format(name))
            with open(path, 'wb') as fh:
                fh.write(data)
            total += len(data)
            print('  {:<38} {} (downloaded)'.format(name, len(data)))

    print('\n{} fonts, {} bytes in {}'.format(len(fonts), total, F.FONT_DIR))
    missing = F.missing_files(ROOT)
    if missing:
        raise SystemExit('still missing:\n  ' + '\n  '.join(missing))
    print('sdmp3_fonts: all files present')
    return 0


if __name__ == '__main__':
    sys.exit(main())
