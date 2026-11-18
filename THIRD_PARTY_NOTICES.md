# Third-party notices

[Apache License 2.0](LICENSE) covers eligible original software. Other authors and content providers retain their respective rights. Each installation must establish permissions for its intended use and publication.

| Material | Source and attribution |
| --- | --- |
| Editorial ID selection | The ordered list adapts *QSAC: Quran Semantic Annotation Corpus* by Ahmad Bilal. The selection filters the upstream material to 2,211 canonical verse IDs and preserves source checksums. The original dataset and ontology are credited under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). [QSAC source revision](https://github.com/dev-ahmadbilal/quran-semantic-annotation-corpus/tree/cb3852b127bfdda6668c5eec9e5c1d9cdcde3810) |
| Fonts | The four unmodified Amiri 1.000 and Noto Sans 2.013 files retain their copyright notices and SIL Open Font License 1.1. [Exact files, hashes and upstream provenance](assets/fonts/README.md), [Amiri license](assets/fonts/OFL-Amiri.txt), [Noto license](assets/fonts/OFL-Noto.txt) |
| Arabic text | The worker requests authentic Uthmani text through the operator's authorized Quran Foundation Content API account. Review current source, caching, attribution, output and redistribution terms. [Quran Foundation Developer Terms](https://api-docs.quran.com/legal/developer-terms/) |
| English and German translations | Rowwad Translation Center translations are provided through QuranEnc. Obtain lawful source access and observe preservation, attribution, version and update requirements for the intended use. [QuranEnc API and conditions](https://quranenc.com/en/home/api/) |
| Recitation and timing | Obtain the required permissions for the chosen MP3Quran recording, platform distribution and any commercial use. [MP3Quran](https://www.mp3quran.net/) |
| Nature footage | Each operator acquires and verifies the particular Pexels or Pixabay asset they use, including applicable rights, restrictions and source provenance. [Pexels license](https://www.pexels.com/license/) and [Pixabay content license summary](https://pixabay.com/service/license-summary/) |
| Computer-vision models | The downloader pins each upstream model's SHA-256. The manifest identifies YuNet under MIT and MediaPipe person detection and YOLOX under Apache-2.0. The models are downloaded separately. [Model manifest](scripts/fetch_cv_models.py) and [upstream revision](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7) |
| Other dependencies | Python packages, Node packages and system tools retain their respective licenses. The Python manifest and dashboard lockfile identify the direct and resolved Node dependencies. [Python requirements](requirements.txt) and [dashboard lockfile](apps/dashboard/package-lock.json) |

The source distribution omits raw Quran datasets, translations, source-text alignment manifests, recitations and stock video. Synthetic fixtures and typography previews are for development. Rights to the source repositories, software dependencies and individual provider content require review before redistribution or broadcasting.
