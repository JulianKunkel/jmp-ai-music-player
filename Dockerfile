# syntax=docker/dockerfile:1
#
# JulAIn Music Player — the container image.
#
# Build from *this* folder (it is the whole build context):
#   docker build -t julians-music-app:latest application/
#   application/build-image.sh
#   application/install-desktop.sh          # builds it and wires the launcher
#
# The image contains the app and its runtime.  It does NOT contain any music:
# the library is mounted at /library when the container runs, so the same
# image works for any music folder and never copies the library.

FROM python:3.13-slim

ARG WITH_CLEMENTINE=false
ARG USER_UID=1000
ARG USER_GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LIBRARY=/library \
    APP_HOME=/opt/music/app \
    PYTHON=/opt/music/venv/bin/python \
    MPV_SOCKET=/tmp/mpv.sock \
    MPV_LOG=/tmp/mpv.log \
    PATH=/opt/music/venv/bin:$PATH

# ffmpeg  : decoding, probing, mood analysis, audio conversion
# mpv     : the headless player driven over its JSON IPC socket
# tini    : reaps the mpv daemon when the container stops
# alsa-utils / pulseaudio-utils : aplay / pactl for audio diagnostics
# libgomp1 : OpenMP runtime that ctranslate2 (the local Whisper backend) links
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg mpv tini ca-certificates alsa-utils pulseaudio-utils libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Optional: the GUI player, for importing the canonical M3U8 playlists.
# It needs an X11 socket and an audio server on the host (see README).
RUN if [ "$WITH_CLEMENTINE" = "true" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends clementine dbus-x11 \
      && rm -rf /var/lib/apt/lists/*; \
    fi

# A venv of the image's own (the repository's scripts/venv is built for the
# host interpreter).  The runtime requirements are the installer's file, so
# host and container can never drift apart.
COPY requirements.txt /opt/music/requirements.txt
RUN python -m venv /opt/music/venv \
 && /opt/music/venv/bin/pip install --upgrade pip \
 && /opt/music/venv/bin/pip install -r /opt/music/requirements.txt

# Local song transcription (faster-whisper).  Kept out of requirements.txt
# because the host has no wheels for every Python it may run: scripts/setup_asr.sh
# builds scripts/asr-venv on the host, and this install is the container's copy.
# `av<17` is pinned — faster-whisper 1.2 passes `metadata_errors` to av, which
# 17+ removed.  Models are downloaded on first use into the *mounted* library
# (`library-work/app/lyrics/models`), so they survive a container rebuild.
# No GPU runtime here (docker reports only `runc`), so this path is CPU-only.
RUN /opt/music/venv/bin/pip install "faster-whisper>=1.2,<2" "av<17"

# Unprivileged user; compose runs with the host uid/gid so files in the
# mounted library keep their owner.
RUN groupadd -g "${USER_GID}" music 2>/dev/null || groupadd music \
 && useradd -m -u "${USER_UID}" -g "${USER_GID}" -s /bin/bash music

# the app itself: code only, no library, no venv, no caches
COPY lib /opt/music/app
# the release number, read by chat/version.py from the folder above the code
COPY VERSION /opt/music/VERSION

# Optional import modules.  `.build-modules` is empty in the open checkout;
# build-image.sh / install.sh stage a module checkout there and pass
# WITH_MODULES=1.  A stale staging folder must never leak into an image by
# accident, so the build refuses it unless the opt-in is explicit.
ARG WITH_MODULES=0
COPY build-modules.py /opt/music/build-modules.py
COPY .build-modules /build/modules
RUN if [ "$WITH_MODULES" != "1" ] \
       && [ -n "$(find /build/modules -mindepth 1 ! -name .keep -print -quit)" ]; then \
      echo "error: .build-modules is not empty; pass --build-arg WITH_MODULES=1 for an intentional module build" >&2; \
      exit 1; \
    fi
RUN /opt/music/venv/bin/python /opt/music/build-modules.py \
      --modules /build/modules \
      --dest /opt/music/app/modules \
      --python /opt/music/venv/bin/python \
      --clean --requirements \
 && rm -rf /build

COPY entrypoint.sh /usr/local/bin/music
RUN chmod 0755 /usr/local/bin/music

WORKDIR /library
USER music

ENTRYPOINT ["/usr/local/bin/music"]
