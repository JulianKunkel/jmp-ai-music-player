# Licence and copyright audit

**Question:** is there any copyright reason not to release this on GitHub
openly?

**Short answer: no blocker. The release is
GPL-3.0-or-later.** The reason is one dependency, not the app's own code:
`mutagen` is GPL-2.0-or-later and is imported in-process by three modules, so
the distributed whole is a derivative work and has to be GPL-compatible. The
"or later" clause lets that whole be distributed under the GPL-3, which is what
this project chose. The `LICENSE` file in this folder is the full GPL-3 text;
`NOTICE` carries the copyright line and the AI-assistance statement. Nothing in
the repository prevents an open release; the rest of this page lists the
obligations and the non-copyright risks, which are a download-terms question
rather than a licensing one.

This is an engineering audit, not legal advice. If money or a company is
involved, have a lawyer read §7 and §8.

---

## 1. What is actually in the release

`application/` is the whole deliverable and the git-tracked folder (91 files
before this audit). It contains:

- the app's Python code (`lib/`), the window's HTML/CSS/JS (`lib/chat/webui/`),
  the skill descriptions and the tests;
- the installers, the Dockerfile, the compose file and requirements;
- the launcher icon (`julians-music-app.svg`, three PNG renders) and the logo
  (`docs/logo-jmp.svg`, `docs/logo-jmp.png`).

It contains **no music, no cover art, no lyric text, no user database, no
token, no API key** and no bundled third-party source tree. Verified:

```bash
find application -type f \( -iname '*.mp3' -o -iname '*.wav' -o \
     -iname '*.flac' -o -iname '*.ogg' -o -iname '*.opus' -o \
     -iname '*.m4a' \)          # → no output
find application -type f \( -iname '*.woff*' -o -iname '*.ttf' -o \
     -iname '*.otf' \)          # → no output (no bundled fonts)
```

The mascot and the launcher icon were drawn for this project as SVG. The
image-model attempt failed, so **no third-party generated image is part of the
release**; there is nothing to attribute to an image model.

The music library lives next to `application/` in the author's checkout and is
**not** part of the git repository. Keep it that way; see §6.

---

## 2. In-process dependencies (they decide the app's licence)

| Component | Version here | Licence | How it is used |
|---|---|---|---|
| **mutagen** | 1.48.1 | **GPL-2.0-or-later** | `from mutagen import File` in `lib/add_file.py`, `lib/playlist_edit.py`, `lib/chat/tagedit.py`; ID3/MP4/FLAC/Vorbis read and write |
| numpy | 2.5.5 | BSD-3-Clause (plus 0BSD, MIT, Zlib, CC0-1.0) | waveform/spectrum maths, mood and profile analysis |
| textual | 8.2.8 | MIT | the terminal UI |
| rich | 15.0.0 | MIT | terminal rendering used by textual |
| pygments | 2.21.0 | BSD-2-Clause | syntax colouring in textual |
| the import fetcher *(optional module only)* | 2026.10.26 | Unlicense (public-domain style) | search and audio import in a build that includes a download module; **not** a dependency of the open player |

**The decisive fact:** `mutagen` is not a separate process. It is imported into
the app's Python process, so the app and mutagen form one combined work. The
GPL requires that the combined work be distributed under the GPL. Because
mutagen is `GPL-2.0-or-later`, GPL-2 and GPL-3 are both permitted; JMP
distributes under **GPL-3.0-or-later**, which the "or later" clause of mutagen
allows. (If a GPL-2-only dependency ever entered the process, the choice would
fall back to GPL-2.0-or-later. Nothing here is GPL-2-only.)

The rest of the in-process dependencies are permissive and add no condition
beyond attribution in the source tree. They do not force a different licence.

> If a permissive licence were a hard requirement, the only route is to remove
> the in-process mutagen import (for example move tagging into a separate
> GPL helper process or replace mutagen), then re-audit. That is a code change,
> not a paperwork change. As shipped today, GPL is the correct answer, and
> GPL-3.0-or-later is the version chosen.

---

## 3. Runtime and image dependencies (separate programs)

These run as their own processes or are linked by the desktop stack. They do
not change the app's source licence, but a **distributed container image** has
to carry their notices and, for GPL programs, a way to obtain the source.

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
| av (PyAV) | BSD-3-Clause | audio decoding for the ASR venv |
| huggingface_hub / tokenizers | Apache-2.0 | model download and tokenisation |
| Whisper model weights | MIT (OpenAI Whisper release) | downloaded on first use; check the model card of the exact model you fetch |
| Clementine (optional build arg) | GPL-2+ | only when `WITH_CLEMENTINE=true`; separate GUI player |
| SongRec (optional, repository `tools/`) | **GPL-3.0+** | a separate Rust binary used by helper reports; **not** in `application/`, not in the release |

### What a container release owes

If you publish only the source folder, the GPL obligations are simple: the
`LICENSE`, the source, and the notices above. If you publish a **built image**
that contains mpv, ffmpeg and the other GPL/LGPL binaries, then:

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
bundles distribution packages under their own licences. This repository already
behaves that way: `install-desktop.sh` builds the image locally from `apt`.

---

## 4. Obligations checklist for an open GitHub release

- [x] `LICENSE` with the full GPL-3.0 text in `application/`.
- [x] `NOTICE` with the copyright line.
- [x] A “Licence” section in `README.md` saying **GPL-3.0-or-later** and
      pointing at `LICENSE`, `NOTICE` and this audit.
- [x] The AI-assistance statement, in `NOTICE`, `README.md` and §8 below.
- [ ] Keep `requirements.txt` and the Dockerfile visible; they are the
      dependency bill of materials.
- [ ] Do not commit the music folder, `LibraryData/`, `library-work/`,
      playlists that name real files, tokens, `secrets.json`, or the caches.
      `application/.gitignore` already excludes byte code, venvs and logs;
      add the library and app-state folders at the repository root if the
      repository ever contains more than `application/`.
- [ ] If the repository grows to include `scripts/` and `tools/SongRec`, give
      SongRec its own GPL-3 notice and do not relicense it. It is a separate
      program; the app calls it as a subprocess and does not import it.
- [ ] If you distribute a built image, add the notices/source offer from §3.
- [x] Keep the copyright line in a `NOTICE` file with the year and the
      author's name.
- [ ] Do a quick trademark check before using “JMP” commercially; copyright
      does not cover the name, trademark might.

---

## 5. Copyright in the *content*, not the code

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
  screenshots.
- **Audio downloads (optional module).** The open repository has no download
  feature. A build-time module may add one; the reference module uses
  `the import fetcher`, which is legal software with a permissive licence, but downloading
  can violate a site's terms and the rightsholder's copyright depending on the
  jurisdiction and the use. This is a **non-licence risk of that module, not of
  the code licence**. State it plainly in the module's own docs, keep the
  feature opt-in, and do not ship a pre-filled download queue or a bundled
  media sample.
- **Third-party metadata and artwork lookups.** MusicBrainz, Cover Art
  Archive, iTunes and Deezer have their own terms; the app queries them only
  when the user runs the artwork tool. Do not publish the fetched cache.
- **The product name and logo.** The logo and icons are original SVG/PNG made
  for this project. “JulAIn” is a pun on the author's name; do a trademark
  search if the project becomes commercial. Copyright in the drawings is the
  author's.

---

## 6. Privacy and personal data in the release

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

## 7. Straight answer, one more time

| Question | Answer |
|---|---|
| Is there a copyright reason **not** to release the code openly? | **No.** The code is the author's (written with AI assistance, see §8); the dependencies are free software. |
| What licence does the release use? | **GPL-3.0-or-later**, because `mutagen` is GPL-2.0-or-later and is imported in-process; its “or later” clause permits the GPL-3. |
| Can the repository stay public with the music on the same disk? | Yes, as long as the music is **not committed**. `application/` is the release; the library is data. |
| Any problem with the logo or name? | No copyright problem: the drawings are original SVG made by the agent for this project. A trademark search is prudent before commercial use. |
| Any problem with a download module? | Not a licence problem, but a terms/copyright risk of that module's feature. Document it there; keep it opt-in. The open player contains no downloader. |
| If I publish a built image? | Add the notices and the source offer for mpv (GPL-3+), ffmpeg (GPL-2+) and the LGPL libraries. |
| If I include SongRec? | It is GPL-3.0+ and must keep that licence; keep it a separate program and do not bundle it into `application/`. |

The practical release recipe is therefore: publish `application/` under
GPL-3.0-or-later with `LICENSE`, `NOTICE`, `README.md`, this audit and the
architecture page; never commit the library, the journals, the databases or the
secrets; ship the image as a build recipe rather than a binary blob, or add the
notices if you do ship it.

---

## 8. Authorship and AI assistance

The project was built in a pair-programming loop with a large language model
acting as a coding agent (OpenCode driving a chat model). The human author —
Julian M. Kunkel, named in `NOTICE` — set the requirements, chose every
behaviour, ran and read the tests, reviewed the diffs and accepted or rejected
each change. The model wrote most of the lines, the tests, the documentation
and the hand-written SVG mascot, but it did not decide what the program should
do and it is not a legal person.

Why this is written down rather than hidden:

- **Honesty.** A reader evaluating the code deserves to know how it came to
  be, and “written by a model in a few days” changes how much trust to place
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
