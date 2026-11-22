# Optional download modules

JulAIn Music Player (JMP) itself **never downloads audio and ships without a
download feature**.  That feature lives in a separate *download module* that a
build can add; this page is the contract between the open player and any such
module.

The open app is complete without a module: playback, search, playlists,
lyrics, the sound profile and every background job work unchanged, and the
window simply shows no import button.

## How a module reaches the app

```
download-modules/            a module checkout (usually its own repository)
├── module.json              the manifest (see below)
├── requirements.txt         the module's own Python dependencies (optional)
└── jmp_download/            the package named by module.json
    ├── __init__.py          the provider API
    └── importer.py          the import subprocess entry point

        │  build-modules.py --modules download-modules --dest lib/modules
        ▼

lib/modules/
├── modules.json             what was compiled
└── jmp_download/            sourceless byte-code (no .py files)
    ├── __init__.pyc
    └── importer.pyc
```

`build-modules.py` copies the package named in the manifest, runs
`compileall` (`legacy=True`, so the `.pyc` sits at the package path instead of
under `__pycache__`), deletes the `.py` sources and installs
`requirements.txt` into the build's Python environment when asked.  The result
is that `lib/modules` contains compiled code only.

```bash
# preview (writes nothing)
python3 build-modules.py --modules download-modules --dest lib/modules --dry-run

# actual build
python3 build-modules.py --modules download-modules --dest lib/modules \
    --clean --requirements
```

`install.sh` and `Dockerfile` call exactly this when a build is pointed at a
module checkout (`install.sh --modules DIR` or `$JMP_MODULES`); with no
checkout the `lib/modules` directory is created empty and the build has no
download feature.

## Discovery at runtime

`lib/chat/downloads.py` is the only download surface of the open app and the
facade every host call site uses.  It
loads the provider package named by the `JMP_DOWNLOAD_MODULE` environment
variable (default `jmp_download`), searching these directories in order:

1. every path in `$JMP_MODULES` and `$JMP_DOWNLOAD_PATH` (an `os.pathsep`
   separated list; handy for tests and custom builds),
2. `lib/modules` next to the running `lib/chat/` package.

If the package cannot be imported, the app behaves as if no module existed:

| facade call | without a module |
|---|---|
| `installed()` / `available(root)` | `False` |
| `search(...)` and every queue write | raises `DownloadUnavailable` |
| `jobs(...)` / `latest(...)` / `attention(...)` | empty / `None` |
| `status_line(...)` | `"unavailable"` |
| `importer_entry()` | `""` (the CLI exits 1 with a readable message) |

Nothing about a module is needed at import time, so the open player compiles
and starts either way.

## `module.json`

```json
{
  "name": "jmp_download",
  "version": "1.0",
  "package": "jmp_download",
  "provides": "downloads",
  "importer": "jmp_download.importer",
  "requirements": "requirements.txt",
  "description": "What this module adds."
}
```

* **`package`** (required) — the directory copied and compiled.
* **`name`**, **`version`**, **`description`** — shown by the build and kept in
  `modules.json`.
* **`provides`** — the facade it fills; `downloads` is the one the app knows.
* **`importer`** — the module used by `music-cli import <link>` (below).
* **`requirements`** — informational; the build installs `requirements.txt`
  from the module checkout if it exists.

## The provider API (`__init__.py`)

The package must expose the following.  Only `available`, `search`, `start`
and the queue reads are strictly required; a missing write raises
`DownloadUnavailable` from the facade, so implement what your build offers.

Constants:

| name | meaning |
|---|---|
| `MODULE_NAME` | the package name (`jmp_download`) |
| `MODULE_VERSION` | a version string |
| `IMPORTER` | the `-m` entry point for the CLI importer |
| `ATTENTION` | statuses that need the listener's attention (`failed`, `partial`, `unknown`) |
| `FINISHED` | terminal statuses (`done`, `partial`, `failed`, `duplicate`, `unknown`, `cancelled`) |
| `EDITABLE` | metadata fields the queue may edit (`title`, `artist`, `album`, `genre`, `track`) |

Functions:

| function | purpose |
|---|---|
| `available(root) -> bool` | can this library file an import (e.g. its tagging scripts exist)? |
| `search(query, count=6, timeout=90) -> list[dict]` | read-only search; each hit has `url`, `title`, `channel`, optional `duration_s`, `views` |
| `probe(url, timeout=60) -> dict` | resolve one link before queueing (title, channel, duration) |
| `guess_metadata(candidate) -> (artist, title)` | derive artist/title from a hit |
| `start(root, candidates, **options) -> list[dict]` | queue imports; one runs at a time, the rest wait |
| `jobs(root) -> list[dict]` | every record, newest first |
| `latest(root)` / `attention(root, records=None)` | the newest record / the newest failure that needs attention |
| `_queued(root, records=None) -> list[dict]` | waiting records in order |
| `status_line(record) -> str` / `_state(root, record) -> dict` | human status and the state the footer draws |
| `log_tail(root, record, lines=400) -> list[str]` | the tail of a run log |
| `pause` / `resume` / `cancel` / `move` / `retry` / `dismiss` / `forget(root, job_id=…)` | queue control |
| `update_tags(root, job_id, fields) -> dict` | edit metadata of a queued or filed import |

A record is a plain `dict`; the fields the app reads are `id`, `url`,
`display`, `title`, `artist`, `album`, `genre`, `track`, `status`, `pid`,
`channel`, `summary`, `filed`, `duplicate`, `order`, `attempts`, `created`,
`dismissed` and `log`.  Exact field names are the module's choice only where
the facade does not read them; the table above is what the window and chat use.

`available()` is the gate the app uses before offering anything, so a module
that is installed but cannot work in a given library should return `False`
there rather than fail later.

## The importer entry point

`music-cli import <link>` calls `downloads.run_importer(root, argv)`, which runs

```bash
python -m <IMPORTER> <argv…>
```

with `MUSIC_LIBRARY` set to the library root and the module's parent directory
on `PYTHONPATH`, so the importer can import its own package even from the
compiled `lib/modules`.  The importer is where the actual download, tagging,
filing and journal write happen; the reference module keeps that in
`jmp_download/importer.py`.

## Building your own module

1. Create the package and a `module.json` as above.
2. Put any third-party dependencies in `requirements.txt`.
3. Point a build at the checkout:
   ```bash
   ./install.sh --docker --modules /path/to/your/module
   # or
   docker build -f Dockerfile -t jmp:local .
   ```
   (the Dockerfile and `install.sh` stage and compile the module into
   `lib/modules`).
4. Start the app and confirm the import button appears and
   `GET /api/state` reports `downloads.available = true`.

## Trust and privacy

A module runs **in the app's process** (the provider API) and as a child
process (the importer), so it can do anything the app can.  Only install a
module you trust, the same as any other code.  What leaves the machine is
whatever the module sends; the open player itself sends nothing unless you
configure a remote planner or ask a library tool to fetch artwork or a model.
The reference module talks to a video site and is documented, with its
terms/copyright caveat, in its own repository.
