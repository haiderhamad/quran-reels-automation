# Pinned worker typography

These four unmodified binaries are the same fonts already required by the worker.
They are ordinary Git objects so a cold CI checkout can install them without Git
LFS or an upstream release download. `scripts/fetch_fonts.py --offline` validates
the entire bundle against `PINNED_FONTS` before changing the runtime cache.

| File | Version | SHA-256 |
| --- | --- | --- |
| AmiriQuran.ttf | 1.000 | 8dcb435b14cb706d79024df4b994221e18474d4be1ec18b04d8120eeaf1844f5 |
| Amiri-Regular.ttf | 1.000 | b092096eb992aebe59084b0cb5cf0015ae82c7e76b0aac5e7d60ae5113af5f54 |
| Amiri-Bold.ttf | 1.000 | c20c5fda0b648bfd7672fd3ded17488daa3a4810673b9b012fbb4a0af6a26aab |
| NotoSans-Regular.ttf | 2.013 | b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5 |

Amiri provenance: the unmodified `Amiri-1.000` members of the official
[release asset 86889246](https://api.github.com/repos/aliftype/amiri/releases/assets/86889246).
Copyright 2010-2022 The Amiri Project Authors
(https://github.com/aliftype/amiri). Full [OFL-Amiri.txt](OFL-Amiri.txt) was copied
from the upstream 1.000 source commit
[45969ca9131d4c4200be89b1b71b9ec5d897264a](https://raw.githubusercontent.com/aliftype/amiri/45969ca9131d4c4200be89b1b71b9ec5d897264a/OFL.txt).

Noto provenance: the existing pinned
[Noto Sans Regular binary](https://raw.githubusercontent.com/googlefonts/noto-fonts/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf).
Its embedded copyright is **Copyright 2015-2021 Google LLC. All Rights Reserved.**
The full upstream [OFL-Noto.txt](OFL-Noto.txt), including the additional
Copyright 2018 The Noto Project Authors notice, was copied from
[the official project license](https://raw.githubusercontent.com/notofonts/noto-fonts/main/LICENSE).
Both binary name tables explicitly identify SIL Open Font License 1.1.

All font software remains under its respective OFL-1.1 license, separately from
the repository's Apache-2.0 code license. Keep both full license files and these
copyright notices with any distribution. No font names or binary bytes were
modified. Upstream URL contents may change; the checked-in hashes are mandatory.
The upstream license texts are byte-for-byte copies; a single scoped whitespace
attribute preserves the Amiri license's original trailing whitespace.
