"""Native libass line geometry without splitting or changing canonical Arabic text.

Two native renders identify naturally wrapped lines by their displacement when
extra line leading is applied. Ink bounds include every diacritic; per-line ASS
clips then translate intact natural lines to a consistent optical gap.
"""
from __future__ import annotations

import ctypes as C
import ctypes.util
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import os
from pathlib import Path


class ArabicLayoutError(ValueError):
    pass


@dataclass(frozen=True)
class ArabicLine:
    left: int
    top: int
    right: int
    bottom: int
    shift: int
    clip_top: int
    clip_bottom: int


@dataclass(frozen=True)
class ArabicGeometry:
    lines: tuple[ArabicLine, ...]
    gap: int


class _Image(C.Structure):
    pass


_Image._fields_ = [
    ('w', C.c_int), ('h', C.c_int), ('stride', C.c_int),
    ('bitmap', C.POINTER(C.c_ubyte)), ('color', C.c_uint32),
    ('x', C.c_int), ('y', C.c_int), ('next', C.POINTER(_Image)), ('type', C.c_int),
]


@lru_cache(maxsize=1)
def _library():
    path = ctypes.util.find_library('ass')
    if not path:
        path = next((str(p) for p in (Path('/opt/homebrew/opt/libass/lib/libass.dylib'), Path('/usr/local/opt/libass/lib/libass.dylib')) if p.is_file()), None)
    if not path:
        raise ArabicLayoutError('Native libass is required to measure Arabic lines safely.')
    lib = C.CDLL(path)
    p, i, s = C.c_void_p, C.c_int, C.c_char_p
    signatures = {
        'ass_library_init': (p, []), 'ass_library_done': (None, [p]),
        'ass_renderer_init': (p, [p]), 'ass_renderer_done': (None, [p]),
        'ass_set_fonts_dir': (None, [p, s]),
        'ass_set_fonts': (None, [p, s, s, i, s, i]),
        'ass_set_frame_size': (None, [p, i, i]),
        'ass_set_storage_size': (None, [p, i, i]),
        'ass_read_memory': (p, [p, s, C.c_size_t, s]),
        'ass_free_track': (None, [p]),
        'ass_set_line_spacing': (None, [p, C.c_double]),
        'ass_render_frame': (C.POINTER(_Image), [p, p, C.c_longlong, C.POINTER(i)]),
    }
    for name, (result, args) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, args
    return lib


@lru_cache(maxsize=2048)
def measure_arabic_geometry(probe: str, font_size: int, width: int, height: int,
                            fonts_dir: str) -> ArabicGeometry:
    """Return exact natural line bounds and offsets in probe coordinates.

    Native extra leading is used only to identify lines, never in final output.
    We verify identical pixels and horizontal positions between probes. Ambiguous
    segmentation or overlapping original ink fails closed instead of clipping.
    """
    from PIL import Image
    lib = _library()
    library = lib.ass_library_init()
    renderer = track = None
    if not library:
        raise ArabicLayoutError('Cannot initialize native libass.')
    try:
        lib.ass_set_fonts_dir(library, os.fsencode(os.path.abspath(fonts_dir)))
        renderer = lib.ass_renderer_init(library)
        if not renderer:
            raise ArabicLayoutError('Cannot initialize native Arabic renderer.')
        # Expand only the measurement height so long diagnostic lines cannot crop.
        measure_height = max(height, 16384)
        lib.ass_set_frame_size(renderer, width, measure_height)
        lib.ass_set_storage_size(renderer, width, measure_height)
        lib.ass_set_fonts(renderer, None, None, 1, None, 1)
        data = probe.replace(f'PlayResY: {height}', f'PlayResY: {measure_height}').encode()
        track = lib.ass_read_memory(library, data, len(data), None)
        if not track:
            raise ArabicLayoutError('Cannot parse native Arabic probe.')

        def render(spacing):
            lib.ass_set_line_spacing(renderer, spacing)
            pointer = lib.ass_render_frame(renderer, track, 0, C.byref(C.c_int()))
            result = []
            while pointer:
                item = pointer.contents
                if item.w and item.h:
                    if item.type != 0:
                        raise ArabicLayoutError('Arabic measurement must have no effects.')
                    raw = b''.join(C.string_at(C.addressof(item.bitmap.contents) + y * item.stride,
                                              item.w) for y in range(item.h))
                    mask = Image.frombytes('L', (item.w, item.h), raw)
                    ink = mask.point(lambda value: 255 if value > 16 else 0).getbbox()
                    full = mask.getbbox()
                    if ink and full:
                        result.append((hashlib.sha256(raw).digest(), item.x, item.y,
                                       tuple((item.x if n % 2 == 0 else item.y) + v
                                             for n, v in enumerate(ink)),
                                       (item.y + full[1], item.y + full[3])))
                pointer = item.next
            return result

        original, spaced = render(0), render(256)
        if not original or len(original) != len(spaced):
            raise ArabicLayoutError('Ambiguous natural Arabic line geometry.')
        grouped = {}
        for before, after in zip(original, spaced):
            delta = after[2] - before[2]
            if before[:2] != after[:2] or delta < 0 or delta % 256:
                raise ArabicLayoutError('Native Arabic wrapping changed between probes.')
            grouped.setdefault(delta // 256, []).append(before)
        if sorted(grouped) != list(range(len(grouped))):
            raise ArabicLayoutError('Incomplete natural Arabic line geometry.')
        bands = []
        for index in sorted(grouped):
            parts = grouped[index]
            bands.append((min(p[3][0] for p in parts), min(p[3][1] for p in parts),
                          max(p[3][2] for p in parts), max(p[3][3] for p in parts),
                          min(p[4][0] for p in parts), max(p[4][1] for p in parts)))
        if any(a[5] > b[4] for a, b in zip(bands, bands[1:])):
            raise ArabicLayoutError('Natural Arabic ink overlaps; refusing to clip diacritics.')
        boundaries = [0] + [(a[5] + b[4]) // 2 for a, b in zip(bands, bands[1:])] + [measure_height]
        gap = max(1, round(font_size * .15))
        lines = []
        next_top = bands[0][1]
        for index, (left, top, right, bottom, _, _) in enumerate(bands):
            shift = next_top - top
            lines.append(ArabicLine(left, top, right, bottom, shift,
                                    boundaries[index], boundaries[index + 1]))
            next_top += bottom - top + gap
        return ArabicGeometry(tuple(lines), gap)
    finally:
        if track:
            lib.ass_free_track(track)
        if renderer:
            lib.ass_renderer_done(renderer)
        lib.ass_library_done(library)
