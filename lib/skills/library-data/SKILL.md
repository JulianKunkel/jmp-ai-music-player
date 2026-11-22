---
name: library-data
description: Back up, export or import the app's own library data — the measured sound profile and the timed lyrics — as plain text, matched by a fingerprint of the audio.
example: back up the library data
triggers: back up the library data, backup, export the library data, import the library data, fingerprinted backup, bibliotheksdaten sichern, datensicherung, sicherung der profildaten
---
# Library data

The two SQLite files the app measures for itself are the expensive part of a
library, and no music is in them:

    library-work/app/psycho/features.db    measured sound properties, axes
    library-work/app/lyrics/lyrics.db      timed lyric lines

This skill keeps a **plain-text** copy of both in the library itself:

    <root>/LibraryData/library-data-YYYY-MM-DD.jsonl

One JSON object per line — a `meta` line, one `track` line per song (its
measurements, profile axes, profile sets and lyric lines together) and one
line per sound family. `grep`, an editor or `jq` is enough to read a backup.

The link between a record and a music file is a **fingerprint of the audio
payload**, never the file name:

    ffmpeg -i FILE -map 0:a:0 -c copy -f streamhash -hash sha256 -

`-c copy` hashes the coded audio packets, so ID3/MP4/Ogg tags, the cover art
and the file name are not part of the hash. Rewriting the tags or renaming
the file keeps the fingerprint, which is what makes an import survive a
reorganised library. `.sid` files and a few odd rips that ffmpeg cannot
demux get no fingerprint; they are matched by path on import, with a warning.

A backup **only reads audio**. It writes the fingerprint index
(`library-work/app/library-data/fingerprints.db`), the `audio_fp` columns of
both databases, today's export and the journal
(`library-work/journal/library-data.jsonl`). An import copies both databases
to `library-work/app/library-data/db-backup/` before writing into them (four
copies kept), keeps rows that already exist unless `overwrite` is set, and
removes an old `LibraryData/` export only on the retention rule.

The app runs this itself once a day on start (`library_backup`, default on),
prunes to `library_backup_keep` exports (7 by default), and shows the job in
the footer chip and in **Settings → Library data**, where an export can also
be checked and imported.

Arguments: `action` — `status` (the numbers and the last job), `backup` (also
`export`, `save`) to run one now, `preview` (also `check`) to report what a
file would fill in without writing, `import` with `file`, and `pause` /
`resume` for a running backup.
