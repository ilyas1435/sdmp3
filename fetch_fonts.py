"""Download the self-hosted Arabic font subsets used by sdmp3_fonts.py.

Developer tool, not part of the app: run it once after adding a font to the
registry, and commit the resulting files in ``static/fonts/``.

    python fetch_fonts.py            # download missing/stale subsets
    python fetch_fonts.py --force    # re-download everything

It asks Google Fonts for the *variable* 400..700 cut, keeps only the `arabic`
and `latin` subsets (the two the UI needs), verifies their unicode ranges match
the constants in ``sdmp3_fonts.py``, and writes:

    static/fonts/<font-id>-arabic.woff2
    static/fonts/<font-id>-latin.woff2

Nothing else in the app depends on this script at runtime.
"""

import argparse
import os
import re
import sys
import urllib.request

import sdmp3_fonts as F

CSS_URL = ('https://fonts.googleapis.com/css2?family={family}:wght@400..700'
           '&display=swap')
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, F.FONT_DIR)


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return data if binary else data.decode('utf-8')


def parse_subsets(css):
    """{subset: (woff2 url, unicode-range)} for the labelled @font-face blocks."""
    found = {}
    for name, body in re.findall(r'/\*\s*([\w-]+)\s*\*/\s*@font-face\s*\{(.*?)\}',
                                 css, re.S):
        if name not in ('arabic', 'latin'):
            continue
        url = re.search(r'url\((https://[^)]+)\)', body)
        rng = re.search(r'unicode-range:\s*([^;]+);', body)
        if url and rng:
            found[name] = (url.group(1).strip(), ' '.join(rng.group(1).split()))
    return found


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
        family = font['family'].replace(' ', '+')
        css = fetch(CSS_URL.format(family=family))
        subsets = parse_subsets(css)
        for subset in ('arabic', 'latin'):
            if subset not in subsets:
                raise SystemExit('{}: no {} subset in the Google CSS'.format(
                    font['id'], subset))
            url, rng = subsets[subset]
            if ' '.join(rng.split()) != ' '.join(expected[subset].split()):
                raise SystemExit('{}: {} unicode-range drifted from sdmp3_fonts.py'
                                 '\n  google: {}\n  module: {}'.format(
                                     font['id'], subset, rng, expected[subset]))
            name = F.font_file(font['id'], subset)
            path = os.path.join(OUT_DIR, name)
            if os.path.isfile(path) and not args.force:
                total += os.path.getsize(path)
                print('  {:<28} {} (kept)'.format(name, os.path.getsize(path)))
                continue
            data = fetch(url, binary=True)
            if len(data) < 1000:
                raise SystemExit('{}: download looks empty'.format(name))
            with open(path, 'wb') as fh:
                fh.write(data)
            total += len(data)
            print('  {:<28} {} (downloaded)'.format(name, len(data)))

    print('\n{} fonts, {} bytes in {}'.format(len(fonts), total, F.FONT_DIR))
    missing = F.missing_files(ROOT)
    if missing:
        raise SystemExit('still missing:\n  ' + '\n  '.join(missing))
    print('sdmp3_fonts: all files present')
    return 0


if __name__ == '__main__':
    sys.exit(main())
