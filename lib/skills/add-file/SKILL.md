---
name: add-file
description: File a local audio file into the library — tag it, place it, index it, all journalled.
example: add the file /home/julian/Music/song.mp3
triggers: add file, add this file, import file, file this, nimm die datei, füge die datei hinzu
---
# Add a local file

Use when the listener points at a file on disk:

- "add ~/Downloads/song.flac"
- "file this under Portishead, album Dummy, track 4"
- "import the folder ~/Recordings"

`add_file.py` **copies** the source (it never deletes it unless `--move` is
explicitly requested), reads whatever tags exist, fills the gaps from the
request, writes them through `tag_fields.py`, moves the file to
`Artists/<Artist>/<Album>/NN_Title.<ext>` and re-indexes the library.

A file already inside the library is refused: it is already indexed, and moving
it again would split an album.

Arguments: `path`, `artist`, `album`, `genre`.
