# Licence and copyright audit

**Question:** can the open player be published on GitHub, and what must the
published release contain?

**Short answer: yes for the open code, under GPL-3.0-or-later.** The decisive
dependency is `mutagen`, which is GPL-2.0-or-later and is imported into the
app's own Python process. That makes the distributed whole a combined work,
and its "or later" clause permits the GPL-3. The `LICENSE` file is the full
GPL-3 text; `NOTICE` carries the copyright line and the AI-assistance
statement.

The open release machine-checkable gate is about *what is shipped*, not about
the licence of the app's own source:

1. the GitHub `.deb` must contain **no optional import module** and no cached
   byte-code — a sourceless module bundled in-process would be a GPL problem.

Two content risks were also identified and cleaned up: the repository's two
full-lyric screenshots showed real copyrighted lyric lines, and older commits
and release notes still named the optional downloader explicitly. Both were
removed from the tree, the history and the public release notes. See §6.

This is an engineering audit, not legal advice. If money or a company is
involved, have a lawyer read §5, §6 and the release checklist in §4.

---

## 1. What is in the repository and in the release

The repository root is the application; there is no nested `application/`
folder any more. The git-tracked tree contains:

- the app's Python code (`lib/`), the window's HTML/CSS/JS
  (`lib/chat/webui/`), the skill descriptions and the tests;
- the installers, the `Dockerfile`, the compose file and `requirements.txt`;
- the packaging files under `packaging/deb/`;
- the launcher icon (`julians-music-app.svg`, PNG renders) and the logo
  (`docs/logo-jmp.svg`, `docs/logo-jmp.png`);
- the documentation under `docs/`, including this audit.

These directories are **git-ignored** and must stay out of every commit and
release asset:

```
download-modules/     a module checkout (its own repository)
.build-modules/       the staging copy used by an image build
lib/modules/          compiled module byte-code
build/  dist/         build output and the release .deb
```

The GitHub release contains only the Debian package and its `.sha256` file.
The package installs the open `lib/`, the launcher, the icon, the man page and
the documentation under `/usr/lib/jmp` and `/usr/share`. It contains **no
music, cover art, lyric database, user profile, secret, token, screenshot or
module payload**.

Verify the built package before it is uploaded:

```bash
dpkg-deb --contents dist/jmp-ai-music-player_<version>-1_all.deb \
  | grep -E '(^|/)lib/modules/|\.py[co]$|(^|/)jmp_download/'
# → no output
```

`publish-release.sh` builds with `JMP_MODULES=` and `--no-modules`, then runs
exactly this check and refuses to upload if anything matches. `build-deb.sh`
repeats the check for an open build before it writes the checksum. This is a
release gate, not a convention: a local module checkout cannot silently enter
the asset.

---

## 2. In-process dependencies (they decide the app's licence)

| Component | Version here | Licence | How it is used |
|---|---|---|---|
| **mutagen** | 1.48.1 | **GPL-2.0-or-later** | `from mutagen import File` in `lib/add_file.py`, `lib/playlist_edit.py`, `lib/chat/tagedit.py`; ID3/MP4/FLAC/Vorbis read and write |
| numpy | 2.5.5 | BSD-3-Clause (plus 0BSD, MIT, Zlib, CC0-1.0) | waveform/spectrum maths, mood and profile analysis |
| textual | 8.2.8 | MIT | the terminal UI |
| rich | 15.0.0 | MIT | terminal rendering used by textual |
| pygments | 2.21.0 | BSD-2-Clause | syntax colouring in textual |
| markdown-it-py / mdurl | current | MIT | Markdown rendering in the TUI |
| platformdirs | current | MIT | per-user paths |
| typing_extensions | current | PSF-2.0 | typing backports |

**The decisive fact:** `mutagen` is not a separate process. It is imported
into the app's Python process, so the app and mutagen form one combined work.
The GPL requires that the combined work be distributed under the GPL. Because
mutagen is `GPL-2.0-or-later`, GPL-2 and GPL-3 are both permitted; JMP
distributes under **GPL-3.0-or-later**, which the "or later" clause allows.
(If a GPL-2-only dependency ever entered the process, the choice would fall
back to GPL-2.0-or-later. Nothing here is GPL-2-only.)

The rest of the in-process dependencies are permissive and add no condition
beyond attribution in the source tree. They do not force a different licence.

> If a permissive licence were a hard requirement, the only route is to remove
> the in-process mutagen import — for example move tagging into a separate
> GPL helper process or replace mutagen — then re-audit. That is a code change,
> not a paperwork change. As shipped today, GPL is the correct answer, and
> GPL-3.0-or-later is the version chosen.

---

## 3. Runtime, ASR and image dependencies (separate programs)

These run as their own processes or are installed into a local virtual
environment. They do not change the app's source licence, but a **distributed
container image** has to carry their notices and, for GPL programs, a way to
obtain the source.

| Component | Licence | Notes |
|---|---|---|
| mpv (Ubuntu binary) | **GPL-3+** | Debian/Ubuntu copyright says the *binary* is GPL-3+ because it links the GPL-3+ `libsmbclient`, while the source is mostly GPL-2+/LGPL-2.1+. It is a separate process; the app talks JSON IPC. |
| ffmpeg (Ubuntu build) | **GPL-2+** | The build config has `--enable-gpl` (libx264, libx265, …), so the binary is GPL-2+; no `--enable-version3`/`--enable-nonfree` in this build. Separate process. |
| WebKitGTK (`gir1.2-webkit2-4.1`) | LGPL-2.1+ / BSD-style parts | renders the native window |
| PyGObject (`python3-gi`) | LGPL-2.1+ | GTK/WebKit bindings |
| GTK 3 / GLib | LGPL-2.1+ | the native window toolkit |
| tini | MIT | PID 1 in the image |
| libgomp1 | GPL-3+ **with the GCC Runtime Library Exception** | OpenMP runtime; the exception permits linking without infecting the app |
| Python base image `python:3.13-slim` | PSF-2.0 (Python) plus mixed Debian licences | the image's base layer |
| faster-whisper | MIT | local lyric/voice transcription |
| ctranslate2 | MIT | inference backend |
| onnxruntime | MIT | VAD runtime |
| av (PyAV) | BSD-3-Clause | audio decoding for the ASR venv; PyAV wheels bundle FFmpeg libraries — check the build flags of the exact wheel and include FFmpeg's notices/source offer if you ship the image |
| huggingface_hub / tokenizers | Apache-2.0 | model download and tokenisation |
| tqdm | **MPL-2.0 AND MIT** | progress bars; a transitive dependency of `faster-whisper` / `huggingface_hub` in the ASR venv |
| certifi | **MPL-2.0** | CA bundle; only when the optional module's fetcher is installed with its default extras — not a dependency of the open player |
| requests / urllib3 / websockets / brotli / pycryptodomex / secretstorage | Apache-2.0 / MIT / BSD-3 / MIT / BSD-2 / BSD-3 | transitive dependencies of a fetcher-based module, if installed |
| Whisper model weights | MIT (OpenAI Whisper release) | downloaded on first use; check the model card of the exact model you fetch |
| Clementine (optional build arg) | GPL-2+ | only when `WITH_CLEMENTINE=true`; separate GUI player |
| SongRec (optional, repository `tools/`) | **GPL-3.0+** | a separate Rust binary used by helper reports; **not** in the repository root and not in the release |

### MPL-2.0 note

MPL-2.0 is file-level copyleft, not a viral whole-work licence. Using an
unmodified MPL library in a separate process or virtual environment does not
relicense the app. If the library is distributed in an image, ship its licence
text and notices and make the source of the MPL-covered files available (the
upstream release is normally enough). This audit lists `tqdm` and `certifi`
because they are the MPL components that can appear transitively; neither is
part of the open `.deb`.

### What a container release owes

If you publish only the source, the GPL obligations are simple: the `LICENSE`,
the source, and the notices above. If you publish a **built image** that
contains mpv, ffmpeg and the other GPL/LGPL binaries, then:

1. include the licence texts and copyright notices of those packages (the
   Debian/Ubuntu `/usr/share/doc/<pkg>/copyright` files are the canonical
   source);
2. provide the corresponding source, or a written offer valid for three years,
   for the GPL/LGPL binaries in the image (pointing at the distribution's
   source package or the upstream release is the usual route);
3. keep the LGPL components relinkable — shipping the distro's unmodified
   shared libraries, as this image does, satisfies that in practice.

The easiest honest route is to publish the **source and the build scripts**,
let people build the image themselves, and state in the README that the image
bundles distribution packages under their own licences. This repository
already behaves that way: `install-desktop.sh` builds the image locally from
`apt`.

---

## 4. Obligations checklist for an open GitHub release

- [x] `LICENSE` with the full GPL-3.0 text.
- [x] `NOTICE` with the copyright line and the AI-assistance statement.
- [x] A "Licence" section in `README.md` saying **GPL-3.0-or-later** and
      pointing at `LICENSE`, `NOTICE` and this audit.
- [x] Keep `requirements.txt` and the `Dockerfile` visible; they are the
      dependency bill of materials.
- [x] Build the uploaded `.deb` with `JMP_MODULES=` and `--no-modules`, and
      verify that it contains no `lib/modules/`, `jmp_download/` or `.pyc`.
      `publish-release.sh` enforces this; `build-deb.sh` repeats it.
- [x] Remove `docs/screenshots/lyrics.png` and
      `docs/screenshots/lyrics-playlist.png` from the tree **and from the git
      history**: both showed real lyric lines (see §6). Review any new
      screenshot before publishing it.
- [ ] Do not commit the music folder, `LibraryData/`, `library-work/`,
      playlists that name real files, tokens, `secrets.json`, or the caches.
      The root `.gitignore` already excludes byte code, venvs, modules, the
      library state and the release output.
- [ ] If the repository ever includes `tools/SongRec`, give it its own GPL-3
      notice and do not relicense it. It is a separate program; the app calls
      it as a subprocess and does not import it.
- [ ] If you distribute a built image, add the notices/source offer from §3.
- [ ] Do a quick trademark check before using "JMP" commercially; copyright
      does not cover the name, trademark might.
- [ ] If you ever distribute a build **with** an import module, satisfy §5
      first.

---

## 5. The optional import module and the GPL

Audio import is not part of the open repository and not part of the published
package. It lives in a separate module checkout. `lib/chat/downloads.py` is the
facade: when a module is present it imports the provider package **into the
app's own process**; when it is absent the player compiles and runs without the
feature.

That design is fine for a local, unpublished build and is a **GPL problem for a
distributed build**:

- the app already forms a combined work with GPL-2.0-or-later `mutagen`;
- the module is imported into the same process and shares the app's data
  structures and call surface;
- `build-modules.py` deliberately deletes the module's `.py` files and ships
  only `.pyc` byte-code.

A GPL-3 combined work must be conveyed with its **complete corresponding
source**, under GPL-compatible terms. Sourceless byte-code is not source, and a
proprietary module cannot be made GPL-compatible just by placing it in a
separate folder. Therefore:

**Do not upload, publish or hand out a `.deb`, image, tarball or archive that
contains the module.** The release gate in §1 exists for this reason. The
earlier statement in this audit that a download module is "not a licence
problem" was wrong; it is a licence problem as soon as the module is
distributed in-process.

If a module build must ever be distributed, the realistic options are:

1. publish the module's source under a GPL-3-compatible licence; or
2. re-architect it as a genuinely separate program with arm's-length
   communication only (no shared in-process objects or data structures), and
   have a lawyer confirm that the separation is respected; or
3. remove the GPL in-process dependency that makes the whole work GPL (see
   §2), which is a substantial code change.

The module's own dependencies need a separate audit. The reference module uses
an external fetcher under the Unlicense; installing it with default extras can
add `certifi` (MPL-2.0) and permissive HTTP/TLS libraries. Those notices belong
with the module, not with the open player, and are irrelevant to the published
`.deb` because it contains neither.

`docs/modules.md` documents the provider contract for private builds. It is
also shipped in the `.deb`, so it must stay a description of the interface, not
a licence to redistribute a closed module.

---

## 6. Copyright in the *content*, not the code

The app is a player and an indexer; it ships no content. But the workflow can
create content that is not yours to publish:

- **Music files and cover art.** Commercial recordings and their artwork stay
  in the user's library and must never be committed, attached to an issue or
  included in a demo. The repository must contain an empty/example library or
  a synthetic test fixture, which is what `tests/chat-tests.py` already does
  (generated silence and fake inventory rows).
- **Lyrics.** Local transcription produces text derived from the recording.
  Publishing a database of transcribed lyrics can infringe the underlying
  lyrics copyright even though the transcription is machine-made. Keep
  `lyrics.db` and any `.lrc` export out of the release and out of public
  screenshots. The two tracked files that showed real lyric lines
  (`docs/screenshots/lyrics.png` and `docs/screenshots/lyrics-playlist.png`)
  were removed from the tree and from the rewritten history; do not reintroduce
  them. Any future screenshot that shows transcribed words needs a synthetic
  or public-domain example.
- **Audio imports (optional module).** The open repository has no import
  feature. A build-time module may add one; downloading can violate a source
  site's terms and the rightsholder's copyright depending on the jurisdiction
  and the use. This is a **non-licence risk of that module, not of the open
  code licence**. State it plainly in the module's own docs, keep the feature
  opt-in, and do not ship a pre-filled queue or a bundled media sample.
- **Third-party metadata and artwork lookups.** MusicBrainz, Cover Art
  Archive, iTunes and Deezer have their own terms; the app queries them only
  when the user runs the artwork tool. Do not publish the fetched cache.
- **The product name and logo.** The logo and icons are original SVG/PNG made
  for this project. "JulAIn" is a pun on the author's name; do a trademark
  search if the project becomes commercial. Copyright in the drawings is the
  author's.

---

## 7. Privacy and personal data in the release

There is no account, no telemetry and no analytics. Still, the app's working
folders contain personal data and must stay out of git:

```
library-work/app/chat/profile.json      taste, notes, learned phrases
library-work/app/chat/session.json      what you last played and where
library-work/app/chat/secrets.json      the planner API key (mode 0600)
library-work/app/lyrics/lyrics.db       transcribed words
library-work/app/psycho/features.db     measured sound profile
LibraryData/*.jsonl                     the daily export of the above
```

Publishing a repository that contains these would publish the author's
listening history, notes and possibly an API key. The `.gitignore` and the
release checklist above are the safeguard. The architecture page
(`docs/architecture.html`) documents the local-only design for users.

---

## 8. Straight answers, one more time

| Question | Answer |
|---|---|
| Is there a copyright reason **not** to release the open code? | **No.** The code is the author's (written with AI assistance, see §9); the dependencies are free software. |
| What licence does the release use? | **GPL-3.0-or-later**, because `mutagen` is GPL-2.0-or-later and is imported in-process; its "or later" clause permits the GPL-3. |
| Can the repository stay public with the music on the same disk? | Yes, as long as the music is **not committed**. The root repository is the release; the library is data. |
| Any problem with the logo or name? | No copyright problem: the drawings are original SVG made by the agent for this project. A trademark search is prudent before commercial use. |
| Any problem with the optional import module? | **Yes, if it is distributed.** Bundled in-process as sourceless byte-code it is part of a GPL combined work and needs complete source under GPL-compatible terms. The published `.deb` therefore excludes it and the release script enforces that. A private local build is not a distribution. |
| Any problem with the current screenshots? | The two full-lyric screenshots were removed from the tree and the rewritten history (§6). Keep real lyric text out of future screenshots. |
| If I publish a built image? | Add the notices and the source offer for mpv (GPL-3+), ffmpeg (GPL-2+), PyAV's bundled FFmpeg and the LGPL libraries. |
| If I include SongRec? | It is GPL-3.0+ and must keep that licence; keep it a separate program and do not bundle it into the app tree. |

The practical release recipe is therefore: publish the root repository under
GPL-3.0-or-later with `LICENSE`, `NOTICE`, `README.md`, this audit and the
architecture page; upload only a module-free `.deb` built by
`publish-release.sh`; never commit the library, the journals, the databases or
the secrets; replace the lyric screenshots; and ship the image as a build
recipe rather than a binary blob, or add the notices if you do ship it.

---

## 9. Authorship and AI assistance

The project was built in a pair-programming loop with a large language model
acting as a coding agent (OpenCode driving a chat model). The human author —
Julian M. Kunkel, named in `NOTICE` — set the requirements, chose every
behaviour, ran and read the tests, reviewed the diffs and accepted or rejected
each change. The model wrote most of the lines, the tests, the documentation
and the hand-written SVG mascot, but it did not decide what the program should
do and it is not a legal person.

Why this is written down rather than hidden:

- **Honesty.** A reader evaluating the code deserves to know how it came to
  be, and "written by a model in a few days" changes how much trust to place
  in it. The GPL's warranty disclaimer is not a formality here.
- **Copyright.** In the US and several other jurisdictions, material generated
  by a model without human authorship cannot itself be copyrighted
  (*Thaler v. Perlmutter*, and the US Copyright Office's 2023–2025 guidance).
  The human author therefore claims copyright in the human contribution —
  selection, arrangement, direction, review, and the parts written or rewritten
  by hand — and releases that under the GPL above. Passages that a court would
  find to have no human authorship are, in effect, public domain; they are not
  made GPL-only by being in this file. This is the conservative reading and it
  does not restrict anyone's use of the project.
- **No third-party code was reproduced from memory.** The dependencies are
  used as libraries or as separate processes (see §2 and §3), not vendored.
  Some patterns are necessarily idiomatic (an HTTP handler, a JSONL journal)
  and a model may emit code similar to public examples; if a rights-holder
  believes a specific passage is theirs, the ordinary GPL/notice route applies
  and the author will relicense or replace it.
- **Accountability.** A model cannot be sued, maintain the project or answer an
  issue. The human author does, and the `NOTICE` says so.

For downstream users: keep `NOTICE` and `LICENSE` when redistributing, and do
not describe the code as entirely human-written. The music library is the
user's data and is not part of the licensed work.
