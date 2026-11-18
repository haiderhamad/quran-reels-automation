"""Fixed Reel geometry shared with the dashboard through reel-layout.json."""
import json
import math
from pathlib import Path

LAYOUT = json.loads((Path(__file__).resolve().parents[1] / 'apps' / 'dashboard' / 'reel-layout.json').read_text())
REEL_WIDTH = LAYOUT['width']
REEL_HEIGHT = LAYOUT['height']
TEXT_SAFE_ZONE = LAYOUT['safeZone']
SURA_HEADER_OFFSET = LAYOUT['suraHeaderOffset']


def safe_rectangle(width=REEL_WIDTH, height=REEL_HEIGHT):
    left, top = TEXT_SAFE_ZONE['left'], TEXT_SAFE_ZONE['top']
    right, bottom = width - TEXT_SAFE_ZONE['right'], height - TEXT_SAFE_ZONE['bottom']
    return dict(x=left, y=top, right=right, bottom=bottom, width=right-left, height=bottom-top)


def clamp_header_offset(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Sura header offset must be a finite number.')
    rule = SURA_HEADER_OFFSET
    bounded = min(rule['max'], max(rule['min'], value))
    return int(math.floor(bounded / rule['step'] + .5) * rule['step'])


def sura_header_top(offset=SURA_HEADER_OFFSET['default']):
    return TEXT_SAFE_ZONE['top'] + clamp_header_offset(offset)
