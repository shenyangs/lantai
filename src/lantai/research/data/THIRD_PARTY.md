# Citation components

Citation formatting uses unmodified **citeproc-js 2.4.63**, copyright ©
2009–2019 Frank Bennett, under the Common Public Attribution License 1.0.
The source is bundled in `citeproc.js`; the upstream notice and complete CPAL
are bundled in `LICENSE-citeproc.txt` and `LICENSE-CPAL-1.0.txt`.
Upstream: <https://github.com/Juris-M/citeproc-js>.

APA 7, IEEE numeric, GB/T 7714-2015 numeric styles and CSL locale files come
from the Citation Style Language project under CC BY-SA 3.0, unmodified.
Their respective authors and attribution are retained in the CSL XML.
The complete license is bundled in `LICENSE-CC-BY-SA-3.0.txt`.
Upstream: <https://github.com/citation-style-language/styles> and
<https://github.com/citation-style-language/locales>.

`manifest.json` pins the upstream commits, URLs and SHA-256 hashes.
The citation engine performs no network access. The research library shows
attribution next to its formatter, and exported provenance retains it.

DOCX and PDF embed **Lantai Research Sans**, derived from Google Fonts' Noto
Sans SC variable font with weight fixed at 400 and the family renamed. Full
character coverage is retained. The font permits embedding (OS/2 fsType=0),
and its SIL Open Font License 1.1 is bundled in `LICENSE-NotoSansSC-OFL-1.1.txt`.
The manifest records the upstream and derived SHA-256 hashes. The derivation
uses fontTools 4.60.2; fontTools is not a runtime dependency.
Upstream: <https://github.com/google/fonts/tree/main/ofl/notosanssc>.
