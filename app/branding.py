"""Brand accent colour: validated, contrast-checked, and turned into CSS variables."""
import re

DEFAULT_ACCENT = '#1d5bf0'
PRESETS = [('Blue', '#1d5bf0'), ('Indigo', '#4f46e5'), ('Violet', '#7c3aed'), ('Teal', '#0f766e'),
           ('Emerald', '#047857'), ('Rose', '#be123c'), ('Amber', '#b45309'), ('Slate', '#334155')]
_HEX = re.compile(r'^#[0-9a-fA-F]{6}$')
MIN_CONTRAST = 4.5          # WCAG AA for normal text: white text on the accent (buttons, active items)


def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def contrast_with_white(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    lum = 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
    return 1.05 / (lum + 0.05)


def normalize_accent(value):
    """(hex, error). Empty means the default. Rejects colours too light for white text."""
    value = (value or '').strip()
    if not value:
        return DEFAULT_ACCENT, None
    if not value.startswith('#'):
        value = '#' + value
    if not _HEX.match(value):
        return None, 'Use a colour like #1d5bf0 (six hexadecimal digits).'
    value = value.lower()
    ratio = contrast_with_white(value)
    if ratio < MIN_CONTRAST:
        return None, f'That colour is too light: white text on it would be hard to read (contrast {ratio:.1f}, needs {MIN_CONTRAST}).'
    return value, None


def brand_css(stored):
    """CSS overriding the accent variables, or '' when the default is in use. Re-validates what it was given."""
    color, err = normalize_accent(stored)
    if err or color == DEFAULT_ACCENT:
        return ''
    return (f':root{{--accent:{color};--accent-2:color-mix(in srgb,{color} 80%,#000)}}'
            f'[data-theme=dark]{{--accent:color-mix(in srgb,{color} 62%,#fff);--accent-2:color-mix(in srgb,{color} 45%,#fff)}}')


# ───────────────────────── Bolga Communications identity ─────────────────────────
BOLGA = {'name': 'Bolga Communications', 'tagline': 'Service Beyond Expectations', 'accent': '#1a5a9d'}
# The "b" with its round counter, plus the ring split into a short top-right arc and a large C-shaped arc.
# Drawn on a -300 -300 520 600 canvas; filled with currentColor so it works on any background.
BOLGA_MARK_PATHS = (
    '<path fill-rule="evenodd" d="M-145,-252 L-81,-280 L-75,-124 A145,145 0 1 1 -145,0 Z '
    'M72,0 A72,72 0 1 0 -72,0 A72,72 0 1 0 72,0 Z"/>'
    '<path d="M0,-292 A292,292 0 0 1 206.5,-206.5 L152,-152 A215,215 0 0 0 0,-215 Z"/>'
    '<path d="M-223.7,-187.7 A292,292 0 0 0 206.5,206.5 L152,152 A215,215 0 0 1 -164.7,-138.2 Z"/>')

LOGO_MAX_BYTES = 1_000_000
_SVG_BAD = re.compile(r'<script|javascript:|<foreignobject|<!entity|data:text/html|\son[a-z]+\s*=|href\s*=\s*["\']\s*https?:', re.I)


def validate_logo(data):
    """(mime, error). Only real PNG / JPEG / WebP / SVG images, up to 1 MB. SVGs with scripts or remote links are refused."""
    if not data:
        return None, 'Choose an image file first.'
    if len(data) > LOGO_MAX_BYTES:
        return None, 'That image is larger than 1 MB. Export a smaller version (a logo rarely needs more than 600 px).'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', None
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', None
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp', None
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return None, 'Use a PNG, JPG, WebP or SVG file.'
    if '<svg' in text.lower():
        if _SVG_BAD.search(text):
            return None, 'That SVG contains scripts or links to other sites, so it was refused. Export a plain SVG.'
        return 'image/svg+xml', None
    return None, 'Use a PNG, JPG, WebP or SVG file.'
