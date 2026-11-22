/* JulAIn Music Player — window UI.
 *
 * The page is a thin shell over the same Engine the terminal UI uses: every
 * turn goes through POST /api/chat, so journals, playlists and learning are
 * shared and nothing is duplicated here.  All text from the engine is escaped
 * and rendered server-side (md_to_html); this file only builds the frame.
 */

(() => {
  "use strict";

  const TOKEN = (document.querySelector('meta[name="chat-token"]') || {}).content || "";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const els = {
    transcript: $("#transcript"),
    scrollBottom: $("#scroll-bottom"),
    prompt: $("#prompt"),
    composer: $("#composer"),
    send: $("#send"),
    btnVoice: $("#btn-voice"),
    chips: $("#chips"),
    stats: $("#stats"),
    indexChip: $("#index-chip"),
    playlistList: $("#playlist-list"),
    brandRoot: $("#brand-root"),
    npTitle: $("#np-title"),
    npArtist: $("#np-artist"),
    npBar: $("#np-bar"),
    npTime: $("#np-time"),
    npTotal: $("#np-total"),
    npCover: $("#np-cover"),
    npDot: $("#np-dot"),
    npWave: $("#np-wave"),
    npFacts: $("#np-facts"),
    npTags: $("#np-tags"),
    radioToggle: $("#radio-toggle"),
    endlessToggle: $("#endless-toggle"),
    shuffleToggle: $("#shuffle-toggle"),
    queueToggle: $("#queue-toggle"),
    queuePanel: $("#panel-queue"),
    queueList: $("#queue-list"),
    queueMeta: $("#queue-meta"),
    queueRefresh: $("#queue-refresh"),
    queueOwn: $("#queue-own"),
    queueOwnName: $("#queue-own-name"),
    queueOwnForm: $("#queue-own-form"),
    queueOwnInput: $("#queue-own-input"),
    queueOwnKeep: $("#queue-own-keep"),
    queueOwnSave: $("#queue-own-save"),
    queueOwnDiscard: $("#queue-own-discard"),
    resumeRow: $("#np-resume"),
    resumePlay: $("#np-resume-play"),
    resumeForget: $("#np-resume-forget"),
    resumeWhat: $("#np-resume-what"),
    lyricsToggle: $("#lyrics-toggle"),
    lyricsPanel: $("#panel-lyrics"),
    lyricsLines: $("#lyrics-lines"),
    lyricsMeta: $("#lyrics-meta"),
    lyricsTranscribe: $("#lyrics-transcribe"),
    lyricsEarlier: $("#lyrics-earlier"),
    lyricsLater: $("#lyrics-later"),
    lyricsExport: $("#lyrics-export"),
    lyricsNext: $("#lyrics-next"),
    npToggle: $("#np-toggle"),
    vizToggle: $("#viz-toggle"),
    vizStyle: $("#viz-style"),
    volume: $("#volume"),
    volumeLabel: $("#volume-label"),
    statusLine: $("#status-line"),
    statusMeta: $("#status-meta"),
    work: $("#work"),
    connBanner: $("#conn-banner"),
    plannerToggle: $("#planner-toggle"),
    plannerLabel: $("#planner-label"),
    modal: $("#modal"),
    modalTitle: $("#modal-title"),
    modalBody: $("#modal-body"),
    toasts: $("#toasts"),
    topbarSub: $("#topbar-sub"),
  };

  const state = {
    ready: false,
    busy: false,
    status: null,
    meta: null,
    lastTracks: null,
    vizEnabled: true,
    vizStyle: "wave",
    vizStyles: null,
    spectrum: null,
    spectrumRel: "",
    axes: null,
    axesRel: "",
    fftHold: [],
    vizRel: "",
    waveform: null,
    statusAt: 0,
    vizFrame: null,
    lyricsOn: false,
    lyricsRel: "",
    lyricsLoadedRel: "",      // the track whose lines are really on screen
    lyricsLines: [],
    lyricsIndex: -1,
    lyricsPolling: null,
    lyricsTimer: null,
    queueOn: false,
    queueKey: "",
    queueBusy: false,
    resume: { available: false, label: "" },
    index: null,
    indexTimer: null,
    indexWasRunning: false,
    work: [],
    workBusy: false,
    workTimer: null,
    downloads: false,
    youtubeSeen: new Set(),
    youtubeReady: false,
    quitting: false,
    clientErrors: [],
    voice: { available: false, enabled: false, autosend: true, ready: false,
             recording: false, sending: false, recorder: null, chunks: [],
             stream: null, audio: null, source: null, processor: null,
             samples: [], size: 0, startedAt: 0, timer: null },
  };

  const icon = (name, cls = "") =>
    `<svg class="${cls}"><use href="#i-${name}"/></svg>`;

  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

  // --------------------------------------------------------------- requests
  //
  // "Load failed" is not an app message: it is what WebKit says when a fetch
  // never got an answer at all (the server closed the connection, was being
  // restarted, or the request outlived its timeout).  Every request therefore
  // carries a timeout and a name, idempotent GETs are retried, and a failure
  // says which endpoint it was instead of repeating the browser's wording.

  const API_TIMEOUTS = {
    "/api/chat": 240,       // the planner may wait on the language model
    "/api/spectrum": 120,   // first call decodes + FFTs the whole track
    "/api/waveform": 90,
    "/api/psycho": 90,
    "/api/import": 60,
    "/api/activity": 30,
    "/api/diagnostics": 20,
    "/api/voice": 300,      // a clip is transcribed locally on the CPU
    "/api/settings": 90,    // the panel asks the model server for its list
  };

  function apiTimeout(path) {
    let seconds = 25;
    Object.keys(API_TIMEOUTS).forEach((prefix) => {
      if (path.startsWith(prefix)) seconds = Math.max(seconds, API_TIMEOUTS[prefix]);
    });
    return seconds;
  }

  async function rawFetch(path, body, seconds) {
    const opts = { headers: { "X-Chat-Token": TOKEN }, cache: "no-store" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    // AbortController also covers the case where the connection opened but
    // the answer never came — without it that hangs forever and looks frozen
    const controller = new AbortController();
    opts.signal = controller.signal;
    const timer = setTimeout(() => controller.abort(), seconds * 1000);
    try {
      return await fetch(path, opts);
    } finally {
      clearTimeout(timer);
    }
  }

  async function api(path, body, options = {}) {
    const method = body === undefined ? "GET" : "POST";
    const seconds = options.timeout || apiTimeout(path);
    const retries = options.retries !== undefined
      ? options.retries : (method === "GET" ? 2 : 0);
    let last = null;
    for (let attempt = 0; attempt <= retries; attempt += 1) {
      try {
        const res = await rawFetch(path, body, seconds);
        let data = null;
        try { data = await res.json(); } catch (_) { data = null; }
        if (!res.ok) {
          // the server answered: that is a real error, not a network failure
          const detail = data && typeof data === "object" ? data : {};
          const message = detail.error || `${res.status} ${res.statusText}`;
          const where = detail.where || path;
          const kind = detail.kind === "internal" ? "server error" : String(res.status);
          const error = new Error(`${message} (${kind} · ${method} ${where})`);
          error.status = res.status;
          error.detail = detail;
          noteClientError(error.message, path, method);
          throw error;
        }
        noteServerUp();
        return data || {};
      } catch (err) {
        if (err && err.status) throw err;        // answered — do not retry
        last = err;
        const aborted = err && err.name === "AbortError";
        if (!aborted && !(err instanceof TypeError)) throw err;
        markServerDown(err, method, path, seconds);
        if (attempt >= retries) break;
        await sleep(350 * Math.pow(attempt + 1, 2));
      }
    }
    const message = networkMessage(last, method, path, seconds);
    noteClientError(message, path, method);
    throw new Error(message);
  }

  function networkMessage(err, method, path, seconds) {
    if (err && err.name === "AbortError") {
      return `no answer after ${seconds} s — ${method} ${path}`;
    }
    const raw = String((err && err.message) || "no answer").trim();
    return `${raw} — ${method} ${path} (server not reachable)`;
  }

  // ------------------------------------------------------ connection state
  //
  // Repeated network failures are folded into one banner and one toast per
  // outage instead of a toast per two-second poll.

  const conn = { down: false, since: 0, lastToast: 0, timer: null };

  function noteClientError(message, path, method) {
    const at = new Date().toISOString().slice(11, 19);
    state.clientErrors.push({ at, message, path, method });
    if (state.clientErrors.length > 12) state.clientErrors.shift();
  }

  function setConnBanner(text) {
    if (!els.connBanner) return;
    els.connBanner.hidden = !text;
    els.connBanner.textContent = text || "";
  }

  function noteServerUp() {
    if (!conn.down) return;
    conn.down = false;
    setConnBanner(null);
    if (conn.timer) { clearInterval(conn.timer); conn.timer = null; }
    toast("The app is answering again.", "good");
    pollStatus();
  }

  function markServerDown(err, method, path) {
    if (state.quitting) return;      // we stopped it ourselves
    const now = Date.now();
    if (!conn.down) { conn.down = true; conn.since = now; }
    const raw = String((err && err.message) || "no answer");
    setConnBanner(`No answer from the app server (${path}) — it may have been ` +
                  `restarted. Retrying…`);
    if (now - conn.lastToast > 20000) {
      conn.lastToast = now;
      toast(`${raw} · ${method} ${path}`, "error");
    }
    if (!conn.timer) {
      // ask the cheapest endpoint whether the server is back, until it is
      conn.timer = setInterval(async () => {
        if (!conn.down) {
          clearInterval(conn.timer); conn.timer = null;
          return;
        }
        try {
          const res = await rawFetch("/api/ping", undefined, 5);
          if (res.ok) {
            conn.down = false;
            clearInterval(conn.timer); conn.timer = null;
            setConnBanner(null);
            toast("The app is answering again.", "good");
            pollStatus();
          }
        } catch (_) { /* still down */ }
      }, 2000);
    }
  }

  // ------------------------------------------------------------------ toast

  function toast(text, kind = "") {
    const node = document.createElement("div");
    node.className = "toast" + (kind ? " " + kind : "");
    node.textContent = text;
    els.toasts.appendChild(node);
    setTimeout(() => {
      node.style.transition = "opacity .2s, transform .2s";
      node.style.opacity = "0";
      node.style.transform = "translateY(6px)";
      setTimeout(() => node.remove(), 220);
    }, kind === "error" ? 6000 : 3600);
  }

  function plainText(html) {
    const box = document.createElement("div");
    box.innerHTML = html || "";
    return (box.textContent || "").trim();
  }

  // ------------------------------------------------------------- transcript

  function nearBottom() {
    const t = els.transcript;
    return t.scrollHeight - t.scrollTop - t.clientHeight < 140;
  }

  function scrollToBottom(smooth = true) {
    els.transcript.scrollTo({
      top: els.transcript.scrollHeight,
      behavior: smooth ? "smooth" : "auto",
    });
    els.scrollBottom.hidden = true;
  }

  function avatar(role) {
    const node = document.createElement("div");
    node.className = "avatar";
    if (role === "user") {
      node.textContent = "you";
    } else {
      node.innerHTML = icon("sparkle");
    }
    return node;
  }

  function messageNode(role, { html = "", text = "", error = false }) {
    const article = document.createElement("article");
    article.className = `msg ${role}` + (error ? " error" : "");
    article.appendChild(avatar(role));
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (role === "user") {
      bubble.textContent = text;
    } else {
      const md = document.createElement("div");
      md.className = "md";
      md.innerHTML = html;
      bubble.appendChild(md);
    }
    article.appendChild(bubble);
    return article;
  }

  function append(node) {
    const stick = nearBottom();
    els.transcript.appendChild(node);
    if (stick) scrollToBottom();
    else els.scrollBottom.hidden = false;
  }

  function addAssistant(html, { error = false } = {}) {
    const node = messageNode("assistant", { html, error });
    append(node);
    return node;
  }

  function addUser(text) {
    append(messageNode("user", { text }));
  }

  function thinkingNode() {
    const article = messageNode("assistant", { html: "" });
    const md = $(".md", article);
    md.innerHTML =
      '<div class="thinking"><span class="dots"><i></i><i></i><i></i></span>' +
      "<span>thinking…</span></div>";
    return article;
  }

  /** Path lines the engine indents under a track become clickable rows. */
  function wireTrackRows(scope) {
    $$("li", scope).forEach((li) => {
      const meta = $(".meta", li);
      if (!meta) return;
      const rel = (meta.textContent || "").trim().replace(/^`|`$/g, "");
      if (!rel || !/\.[a-z0-9]{2,5}$/i.test(rel)) return;
      meta.classList.add("track-path");
      meta.dataset.play = rel;
      meta.title = "Click to play this track";
      meta.style.cursor = "pointer";
    });
  }

  function renderTracks(tracks) {
    if (!tracks || !tracks.length) return null;
    const box = document.createElement("details");
    box.className = "tracks";
    const summary = document.createElement("summary");
    summary.textContent = `${tracks.length} track${tracks.length === 1 ? "" : "s"} — click to play`;
    box.appendChild(summary);
    const actions = document.createElement("div");
    actions.className = "tracks-actions";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "tracks-save";
    save.textContent = "Save as playlist";
    save.title = "Keep this selection as a custom playlist";
    save.onclick = (event) => {
      event.preventDefault();
      event.stopPropagation();
      send("save that playlist");
    };
    actions.appendChild(save);
    box.appendChild(actions);
    const list = document.createElement("ol");
    tracks.forEach((track) => {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.className = "track-btn";
      button.dataset.play = track.rel;
      button.title = track.rel;
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = track.artist || "unknown";
      const what = document.createElement("span");
      what.className = "what";
      what.textContent = track.title || track.rel.split("/").pop();
      const len = document.createElement("span");
      len.className = "len";
      len.textContent = track.duration || "";
      button.append(who, what, len);
      li.appendChild(button);
      list.appendChild(li);
    });
    box.appendChild(list);
    return box;
  }

  function addReply(reply) {
    if (reply.clear) {
      clearConversation(false);
      return;
    }
    const stick = nearBottom();
    const node = addAssistant(reply.html, { error: reply.error });
    wireTrackRows($(".md", node));
    const tracks = renderTracks(reply.tracks);
    if (tracks) {
      $(".bubble", node).appendChild(tracks);
      state.lastTracks = reply.tracks;
      if (stick) scrollToBottom(false);
    }
    if (reply.quit) {
      toast("The server is stopping — you can close this window.");
    }
  }

  function openSkills() {
    const skills = (state.meta && state.meta.skills) || [];
    openModal("Skills", (close) => {
      const wrap = document.createElement("div");
      const intro = document.createElement("p");
      intro.className = "note";
      intro.style.marginTop = "0";
      intro.textContent =
        "These are the things the chat can do. Click a phrase to try it, or " +
        "just say what you want in your own words.";
      wrap.appendChild(intro);
      skills.forEach((skill) => {
        const card = document.createElement("div");
        card.className = "skill-card";
        const title = document.createElement("div");
        title.className = "skill-card-title";
        title.innerHTML = icon("bolt");
        const name = document.createElement("strong");
        name.textContent = skill.title;
        title.appendChild(name);
        card.appendChild(title);
        if (skill.description) {
          const body = document.createElement("p");
          body.className = "skill-card-desc";
          body.textContent = skill.description;
          card.appendChild(body);
        }
        if (skill.example) {
          const chips = document.createElement("div");
          chips.className = "chips inline";
          const chip = document.createElement("button");
          chip.className = "chip try";
          chip.textContent = `“${skill.example}”`;
          chip.onclick = () => {
            close();
            send(skill.example);
          };
          chips.appendChild(chip);
          card.appendChild(chips);
        }
        wrap.appendChild(card);
      });
      return wrap;
    });
  }

  // ------------------------------------------------------------- activity

  function activitySection(title, rows, empty) {
    const section = document.createElement("div");
    section.className = "activity-section";
    const heading = document.createElement("h3");
    heading.textContent = title;
    section.appendChild(heading);
    if (!rows.length) {
      const note = document.createElement("p");
      note.className = "note";
      note.style.margin = "0";
      note.textContent = empty;
      section.appendChild(note);
      return section;
    }
    rows.forEach((row) => section.appendChild(row));
    return section;
  }

  function activityRow(kind, what, when, onOpen) {
    const row = document.createElement(onOpen ? "button" : "div");
    row.className = "activity-row";
    if (onOpen) {
      row.type = "button";
      row.style.width = "100%";
      row.style.textAlign = "left";
      row.onclick = onOpen;
    }
    const tag = document.createElement("span");
    tag.className = "kind";
    tag.textContent = kind;
    const text = document.createElement("span");
    text.className = "what";
    text.textContent = what;
    const stamp = document.createElement("span");
    stamp.className = "when";
    stamp.textContent = when || "";
    row.append(tag, text, stamp);
    return row;
  }

  function activityView(data, close) {
    const wrap = document.createElement("div");

    const changeRows = (data.changes || []).map((row) => activityRow(
      row.undoable ? "undo" : row.label || "change",
      `${row.title}${row.detail ? " · " + row.detail : ""}`,
      row.ago || row.ts || "",
      row.undoable ? async () => {
        close();
        try {
          const res = await api("/api/recent", { ts: row.ts });
          toast(res.message || (res.ok ? "undone" : "not undone"),
                res.ok ? "ok" : "error");
          if (res.ok) refreshMeta();
        } catch (err) {
          toast(err.message, "error");
        }
        openActivity();
      } : null));
    wrap.appendChild(activitySection("Recently changed", changeRows,
                                     "Nothing changed yet."));

    const playlistRows = (data.playlists || []).map((row) => {
      const detail = [`${row.count} track${row.count === 1 ? "" : "s"}`];
      if (row.request) detail.push(`“${row.request}”`);
      return activityRow("playlist", `${row.name} · ${detail.join(" · ")}`,
                         row.ago, () => {
        close();
        send(`show ${row.name}`);
      });
    });
    wrap.appendChild(activitySection("Playlists made", playlistRows,
                                     "No playlists saved yet."));

    const downloadRows = (data.downloads || []).map((row) => {
      const detail = [row.state || row.status || "unknown"];
      if (row.artist) detail.unshift(row.artist);
      if (row.summary) detail.push(row.summary);
      return activityRow("download", `${row.label} · ${detail.join(" · ")}`,
                         row.ago, () => { close(); send("download status"); });
    });
    wrap.appendChild(activitySection("Downloads", downloadRows,
                                     "No imports yet."));

    const eventRows = (data.events || []).map((event) => {
      const detail = event.source ? `${event.ago} · ${event.source}` : event.ago;
      return activityRow(event.kind || "event", event.label, detail);
    });
    wrap.appendChild(activitySection("Events", eventRows,
                                     "Nothing liked, skipped or noted yet."));

    const recentRows = (data.recent || []).map((row) => {
      const heard = Math.round((Number(row.fraction) || 0) * 100);
      return activityRow("played", `${row.label} · ${heard}%`, row.ago, () => {
        close();
        play(row.rel);
      });
    });
    wrap.appendChild(activitySection("Recently played", recentRows,
                                     "Nothing played yet."));

    const agent = data.agent || {};
    const agentRows = [];
    if (agent.requests) {
      agentRows.push(activityRow(
        "agent", `${agent.requests} request${agent.requests === 1 ? "" : "s"} waiting or recorded`,
        "", () => { close(); send("/agent"); }));
    }
    (agent.learned || []).forEach((name) => {
      agentRows.push(activityRow("learned", name, ""));
    });
    wrap.appendChild(activitySection("Skill agent", agentRows,
                                     "No unrecognised requests — good."));

    const profile = data.profile || {};
    const learned = document.createElement("p");
    learned.className = "note";
    learned.style.margin = "6px 0 0";
    learned.textContent =
      `Learned taste: ${profile.artists || 0} artists · ` +
      `${profile.genres || 0} genres · ${profile.moods || 0} moods · ` +
      `${profile.notes || 0} notes · ${profile.mappings || 0} phrases.`;
    wrap.appendChild(learned);
    return wrap;
  }

  function renderStats(box, data) {
    box.innerHTML = "";
    const list = (pairs, empty) => (pairs && pairs.length
      ? pairs.map(([name, count]) => `${name} ${Number(count).toLocaleString()}`)
        .join(" · ")
      : empty);
    const covers = data.coverage || {};
    const pct = (n) => `${Number(n || 0).toFixed(1)} %`;
    const lists = data.playlists || {};
    const added = data.added || {};
    const lines = [
      ["Tracks", `${(data.tracks || 0).toLocaleString()} · `
        + `${Number(data.hours || 0).toLocaleString()} h · `
        + `${Number(data.size_gb || 0).toLocaleString()} GB`],
      ["Taggable", `${(data.taggable || 0).toLocaleString()} · `
        + `${(data.chiptune || 0).toLocaleString()} chiptunes`],
      ["Artists", `${(data.artists?.unique || 0).toLocaleString()} · `
        + `${(data.albums?.unique || 0).toLocaleString()} albums`],
      ["Genres", list(data.genres, "none tagged yet")],
      ["Decades", list(data.decades, "no dated tracks")],
      ["Formats", list((data.formats || []).slice(0, 6), "—")],
      ["Quality", `${Number(data.average_bitrate || 0).toLocaleString()} kbit/s`
        + ` · ${(data.lossless || 0).toLocaleString()} lossless`],
      ["Added", `${(added.week || 0).toLocaleString()} this week · `
        + `${(added.month || 0).toLocaleString()} this month`],
      ["Playlists", `${(lists.total || 0).toLocaleString()} · `
        + `${(lists.mood || 0)} mood · ${(lists.focus || 0)} focus · `
        + `${(lists.custom || 0)} of mine`],
      ["Coverage", `title ${pct(covers.title)} · artist ${pct(covers.artist)} · `
        + `genre ${pct(covers.genre)} · art ${pct(covers.artwork)}`],
    ];
    lines.forEach(([key, value]) => {
      const row = document.createElement("div");
      row.className = "health-row";
      const label = document.createElement("span");
      label.className = "health-key";
      label.textContent = key;
      const text = document.createElement("span");
      text.textContent = value;
      row.append(label, text);
      box.appendChild(row);
    });
    const when = document.createElement("p");
    when.className = "note";
    when.style.margin = "6px 0 0";
    when.textContent = `Read ${new Date().toLocaleTimeString()} · the full `
      + "detail is in the inventory reports and the numbers the app measures.";
    box.appendChild(when);
  }

  function renderHealth(box, data) {
    box.innerHTML = "";
    const tags = data.tags || {};
    const art = data.artwork || {};
    const decode = data.decode || {};
    const audit = data.inventory || {};
    const lists = data.playlists || {};
    const profile = data.profile || {};
    const lyrics = data.lyrics || {};
    const lines = [
      ["Tracks", `${(data.tracks || 0).toLocaleString()} · `
        + `${Number(data.hours || 0).toLocaleString()} h · `
        + `${Number(data.size_gb || 0).toLocaleString()} GB`],
      ["Tags", `${(tags.taggable || 0).toLocaleString()} taggable · `
        + `${tags.no_title || 0} no title · ${tags.no_artist || 0} no artist `
        + `· ${tags.no_genre || 0} no genre · ${tags.no_album || 0} no album`],
      ["Artwork", `${(art.with_embedded || 0).toLocaleString()} embedded · `
        + `${(art.with_folder || 0).toLocaleString()} folder-only · `
        + `${(art.none || 0).toLocaleString()} none`],
      ["Decode", `${(decode.ok || 0).toLocaleString()} clean · `
        + `${decode.failed || 0} with an error · `
        + `${decode.unknown || 0} unknown`],
      ["Index", `${(audit.rows || 0).toLocaleString()} rows · `
        + `${audit.missing || 0} missing · ${audit.changed || 0} changed`],
      ["Playlists", `${(lists.lists || 0).toLocaleString()} lists · `
        + `${(lists.entries || 0).toLocaleString()} entries`],
      ["Sound profile", `${(profile.with_data || 0).toLocaleString()} of `
        + `${(profile.total || 0).toLocaleString()} measured`],
      ["Lyrics", `${(lyrics.with_data || 0).toLocaleString()} of `
        + `${(lyrics.total || 0).toLocaleString()} transcribed`],
    ];
    lines.forEach(([key, value]) => {
      const row = document.createElement("div");
      row.className = "health-row";
      const label = document.createElement("span");
      label.className = "health-key";
      label.textContent = key;
      const text = document.createElement("span");
      text.textContent = value;
      row.append(label, text);
      box.appendChild(row);
    });
    (data.notes || []).forEach((note) => {
      const line = document.createElement("p");
      line.className = "note";
      line.style.margin = "2px 0";
      line.textContent = note;
      box.appendChild(line);
    });
    const when = document.createElement("p");
    when.className = "note";
    when.style.margin = "6px 0 0";
    when.textContent = `Checked ${new Date().toLocaleTimeString()} · the `
      + "full detail is in library-work/reports/.";
    box.appendChild(when);
  }

  function dupClock(seconds) {
    const value = Math.max(0, Math.round(Number(seconds) || 0));
    return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
  }

  function renderDuplicates(box, data) {
    box.innerHTML = "";
    const groups = data.items || [];
    if (!groups.length) {
      const none = document.createElement("p");
      none.className = "note";
      none.textContent = "No duplicate group is open: every copy left in the "
        + "tree is a different recording or a different master.";
      box.appendChild(none);
      return;
    }
    const head = document.createElement("p");
    head.className = "note";
    head.textContent = `${(data.groups || 0).toLocaleString()} groups · `
      + `${(data.files || 0).toLocaleString()} files · `
      + `${(data.present || 0).toLocaleString()} on disk.  The same recording `
      + "on two albums — both copies are wanted, and nothing is deleted here.";
    box.appendChild(head);
    groups.slice(0, 40).forEach((group, index) => {
      const first = (group.tracks || [])[0] || {};
      const wrap = document.createElement("div");
      wrap.className = "dup-group";
      const title = document.createElement("div");
      title.className = "dup-title";
      title.textContent = `${index + 1}. ${first.artist || "unknown artist"} — `
        + `${first.title || group.key}`;
      wrap.appendChild(title);
      (group.tracks || []).forEach((track) => {
        const row = document.createElement("div");
        row.className = "dup-copy";
        const text = document.createElement("span");
        const kept = track.rel === group.keep;
        text.textContent = `${track.album || "no album"} · `
          + `${track.duration_s ? dupClock(track.duration_s) : "—"}`
          + (track.bitrate ? ` · ${track.bitrate} kbit/s` : "")
          + (track.size_mb ? ` · ${track.size_mb} MB` : "")
          + (kept ? " · kept" : "")
          + (track.exists ? "" : " · not on disk");
        row.appendChild(text);
        if (track.exists) {
          const button = document.createElement("button");
          button.className = "btn tiny";
          button.type = "button";
          button.textContent = "Play";
          button.title = `Play ${track.rel}`;
          button.onclick = () => play(track.rel);
          row.appendChild(button);
        }
        wrap.appendChild(row);
      });
      if (group.length_gap) {
        const gap = document.createElement("p");
        gap.className = "note";
        gap.style.margin = "2px 0 0";
        gap.textContent = `${group.length_gap.toFixed(1)} s apart — `
          + (group.length_gap > 2 ? "a different master"
                                  : "within the same-take rule");
        wrap.appendChild(gap);
      }
      box.appendChild(wrap);
    });
    if (groups.length > 40) {
      const more = document.createElement("p");
      more.className = "note";
      more.textContent = `… and ${groups.length - 40} more groups; the chat `
        + "lists them all with “find duplicate songs”.";
      box.appendChild(more);
    }
  }

  function renderArrivals(box, data) {
    box.innerHTML = "";
    const head = document.createElement("p");
    head.className = "note";
    head.textContent = `${(data.total || 0).toLocaleString()} tracks · changed `
      + `today ${(data.today || 0).toLocaleString()} · 7 days `
      + `${(data.week || 0).toLocaleString()} · 31 days `
      + `${(data.month || 0).toLocaleString()}.  “Added” is the file's own `
      + "time, so a tag write or a rename moves a track up here too.";
    box.appendChild(head);
    const items = data.items || [];
    if (!items.length) {
      const none = document.createElement("p");
      none.className = "note";
      none.textContent = "No file is newer than the chosen window.";
      box.appendChild(none);
      return;
    }
    items.forEach((entry, index) => {
      const group = document.createElement("div");
      group.className = "dup-group";
      const title = document.createElement("div");
      title.className = "dup-title";
      title.textContent = `${index + 1}. ${entry.artist || "unknown artist"} — `
        + `${entry.title || entry.rel.split("/").pop()}`;
      group.appendChild(title);
      const row = document.createElement("div");
      row.className = "dup-copy";
      const text = document.createElement("span");
      const album = entry.album || entry.rel.split("/").slice(-2)[0] || "";
      text.textContent = [album, entry.genre, entry.age || ""]
        .filter(Boolean).join(" · ");
      row.appendChild(text);
      const button = document.createElement("button");
      button.className = "btn tiny";
      button.type = "button";
      button.textContent = "Play";
      button.title = `Play ${entry.rel}`;
      button.onclick = () => play(entry.rel);
      row.appendChild(button);
      group.appendChild(row);
      box.appendChild(group);
    });
  }

  function openActivity() {
    openModal("Activity", () => {
      const wrap = document.createElement("div");
      const head = document.createElement("div");
      head.className = "modal-actions";
      head.style.margin = "0 0 10px";
      head.style.justifyContent = "space-between";
      const hint = document.createElement("span");
      hint.className = "note";
      hint.style.margin = "0";
      hint.textContent = "What the app remembers — changes, playlists, "
        + "events and plays.  A playlist change can be taken back.";
      const refresh = document.createElement("button");
      refresh.className = "btn";
      refresh.textContent = "Refresh";
      head.append(hint, refresh);
      const body = document.createElement("div");
      wrap.append(head, body);

      const load = async () => {
        body.innerHTML = "";
        const loading = document.createElement("p");
        loading.className = "note";
        loading.textContent = "Loading…";
        body.appendChild(loading);
        try {
          const data = await api("/api/activity?limit=60");
          body.innerHTML = "";
          body.appendChild(activityView(data, closeModal));
        } catch (err) {
          body.innerHTML = "";
          const failed = document.createElement("p");
          failed.className = "note";
          failed.textContent = err.message;
          body.appendChild(failed);
        }
      };
      refresh.onclick = load;
      load();
      return wrap;
    });
  }

  // -------------------------------------------------------------- the turn

  async function send(text) {
    const message = (text || "").trim();
    if (!message || state.busy) return;
    state.busy = true;
    els.send.disabled = true;
    els.prompt.value = "";
    autoGrow();
    addUser(message);
    const thinking = thinkingNode();
    append(thinking);
    try {
      const reply = await api("/api/chat", { message });
      thinking.remove();
      addReply(reply);
      const tool = String(reply.tool || "");
      // a turn that changed the library also moves the stats and the jobs
      const touched = ["make-playlist", "edit-playlist", "save-playlist",
                       "import-audio", "add-file"].includes(tool)
        || tool.startsWith("radio")
        || tool.startsWith("settings");
      // "index the library" and "measure the library" start a background job:
      // read the state so its chip and progress appear without a reload
      const startedJob = tool === "library-overview · index"
        || tool === "psycho · update";
      if (!tool || ["make-playlist", "edit-playlist", "save-playlist",
                    "import-audio", "add-file", "play-music"].includes(tool)
          || tool.startsWith("settings") || tool.startsWith("radio")
          || startedJob) {
        setTimeout(pollStatus, 250);
        if (touched || startedJob) refreshMeta();
      }
    } catch (err) {
      thinking.remove();
      addAssistant(`<p><code>${escapeHtml(err.message)}</code></p>`,
                   { error: true });
      toast(err.message, "error");
    } finally {
      state.busy = false;
      els.send.disabled = false;
      els.prompt.focus();
    }
  }

  // --------------------------------------------------------------- voice
  //
  // Local speech input: the window records a clip, the server transcribes it
  // with the library's own Whisper backend (`scripts/transcribe_lyrics.py
  // --text`) and returns the words, which then steer the app exactly like a
  // typed sentence.  Nothing is uploaded and no clip is kept — the temp file
  // is deleted by the server the moment the transcriber exits.

  const VOICE_MIMES = [
    "audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus",
    "audio/ogg", "audio/mp4",
  ];
  const VOICE_MAX_SECONDS = 60;

  function voiceSupported() {
    return Boolean(navigator.mediaDevices &&
                   navigator.mediaDevices.getUserMedia);
  }

  function setVoiceUi() {
    const v = state.voice;
    const btn = els.btnVoice;
    if (!btn) return;
    btn.hidden = !v.available;
    btn.classList.toggle("recording", Boolean(v.recording));
    btn.classList.toggle("busy", Boolean(v.sending));
    btn.disabled = Boolean(v.sending || (!v.recording && (!v.ready || state.busy)));
    if (v.recording) {
      btn.title = "Stop recording and use what I said";
      btn.setAttribute("aria-label", "Stop recording");
    } else if (!v.ready) {
      btn.title = v.note || "No local transcriber is installed";
      btn.setAttribute("aria-label", "Voice input unavailable");
    } else if (v.sending) {
      btn.title = "Transcribing locally…";
      btn.setAttribute("aria-label", "Transcribing");
    } else {
      btn.title = "Speak a request (Ctrl+Shift+V) — transcribed locally";
      btn.setAttribute("aria-label", "Voice input");
    }
  }

  function applyVoice(voice) {
    if (!voice || typeof voice !== "object") return;
    const v = state.voice;
    if (typeof voice.available === "boolean") v.available = voice.available;
    if (typeof voice.enabled === "boolean") v.enabled = voice.enabled;
    if (typeof voice.autosend === "boolean") v.autosend = voice.autosend;
    if (typeof voice.ready === "boolean") v.ready = voice.ready;
    if (typeof voice.note === "string") v.note = voice.note;
    if (!v.enabled || !voiceSupported()) v.available = false;
    setVoiceUi();
  }

  function startVoiceTimer() {
    const v = state.voice;
    v.startedAt = Date.now();
    stopVoiceTimer();
    v.timer = setInterval(() => {
      const seconds = (Date.now() - v.startedAt) / 1000;
      if (seconds >= VOICE_MAX_SECONDS) {
        stopRecording();
        return;
      }
      if (els.btnVoice) {
        els.btnVoice.title = `Recording ${Math.floor(seconds)} s — click to stop`;
      }
    }, 500);
  }

  function stopVoiceTimer() {
    if (state.voice.timer) {
      clearInterval(state.voice.timer);
      state.voice.timer = null;
    }
  }

  function releaseVoiceStream() {
    const v = state.voice;
    if (v.processor) {
      try { v.processor.disconnect(); } catch (_) { /* already gone */ }
      v.processor.onaudioprocess = null;
      v.processor = null;
    }
    if (v.source) {
      try { v.source.disconnect(); } catch (_) { /* already gone */ }
      v.source = null;
    }
    if (v.audio) {
      try { v.audio.close(); } catch (_) { /* already gone */ }
      v.audio = null;
    }
    if (v.stream) {
      v.stream.getTracks().forEach((track) => {
        try { track.stop(); } catch (_) { /* already gone */ }
      });
      v.stream = null;
    }
    if (v.recorder) {
      try { v.recorder.ondataavailable = null; } catch (_) { /* gone */ }
      v.recorder = null;
    }
  }

  async function startRecording() {
    const v = state.voice;
    if (v.recording || v.sending || state.busy) return;
    if (!v.enabled) {
      toast("Voice input is switched off — turn it on in Settings.", "error");
      return;
    }
    if (!v.available) {
      toast(v.note || "This window cannot reach a microphone.", "error");
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true,
                 noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      const name = String((err && err.name) || "");
      const why = String((err && err.message) || err || "");
      const hints = {
        NotAllowedError: "the window refused microphone access",
        PermissionDeniedError: "the window refused microphone access",
        NotFoundError: "no microphone is connected",
        DevicesNotFoundError: "no microphone is connected",
        NotReadableError: "the microphone is used by another program",
        TrackStartError: "the microphone is used by another program",
        OverconstrainedError: "the microphone does not support this format",
        SecurityError: "the page is not allowed to record here",
      };
      const hint = hints[name]
        || (why ? why : "the window has no microphone access");
      toast(`The microphone is not available — ${hint}.`, "error");
      return;
    }
    v.stream = stream;
    v.chunks = [];
    v.samples = [];
    v.sampleRate = 0;
    v.recording = true;
    setVoiceUi();
    startVoiceTimer();
    // preferred: the browser's own encoder; Firefox/Safari/WebKit vary, so a
    // plain WebAudio capture is the fallback rather than a dead microphone
    if (window.MediaRecorder) {
      const mime = VOICE_MIMES.find(
        (type) => MediaRecorder.isTypeSupported(type)) || "";
      try {
        v.recorder = new MediaRecorder(
          stream, mime ? { mimeType: mime } : undefined);
        v.recorder.ondataavailable = (event) => {
          if (event.data && event.data.size) v.chunks.push(event.data);
        };
        v.recorder.onstop = () => finishRecording();
        v.recorder.onerror = () => finishRecording();
        v.recorder.start();
        return;
      } catch (_) {
        v.recorder = null;
      }
    }
    startWavCapture(stream);
  }

  function startWavCapture(stream) {
    const v = state.voice;
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    try {
      v.audio = new AudioCtx();
      v.sampleRate = v.audio.sampleRate;
      v.source = v.audio.createMediaStreamSource(stream);
      v.processor = v.audio.createScriptProcessor(4096, 1, 1);
      v.processor.onaudioprocess = (event) => {
        if (!v.recording) return;
        const data = event.inputBuffer.getChannelData(0);
        for (let i = 0; i < data.length; i += 1) v.samples.push(data[i]);
      };
      v.source.connect(v.processor);
      // a muted sink keeps the graph running without echoing the microphone
      const mute = v.audio.createGain();
      mute.gain.value = 0;
      v.processor.connect(mute);
      mute.connect(v.audio.destination);
    } catch (err) {
      v.recording = false;
      stopVoiceTimer();
      releaseVoiceStream();
      setVoiceUi();
      toast(`This window cannot record audio: ${err.message}`, "error");
    }
  }

  function stopRecording() {
    const v = state.voice;
    if (!v.recording) return;
    if (v.recorder && v.recorder.state !== "inactive") {
      try {
        v.recorder.stop();       // onstop continues in finishRecording()
      } catch (_) {
        finishRecording();
      }
      return;
    }
    finishRecording();
  }

  function encodeWav(samples, rate) {
    const length = samples.length;
    const buffer = new ArrayBuffer(44 + length * 2);
    const view = new DataView(buffer);
    const write = (offset, text) => {
      for (let i = 0; i < text.length; i += 1) {
        view.setUint8(offset + i, text.charCodeAt(i));
      }
    };
    write(0, "RIFF");
    view.setUint32(4, 36 + length * 2, true);
    write(8, "WAVE");
    write(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);           // PCM
    view.setUint16(22, 1, true);           // mono
    view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    write(36, "data");
    view.setUint32(40, length * 2, true);
    let offset = 44;
    for (let i = 0; i < length; i += 1) {
      const sample = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff,
                    true);
      offset += 2;
    }
    return view;
  }

  async function finishRecording() {
    const v = state.voice;
    if (!v.recording) return;
    v.recording = false;
    stopVoiceTimer();
    let blob = null;
    if (v.recorder && v.chunks.length) {
      const type = String(v.recorder.mimeType || "audio/webm").split(";")[0];
      blob = new Blob(v.chunks, { type });
    } else if (v.samples.length) {
      blob = new Blob([encodeWav(v.samples, v.sampleRate || 44100)],
                      { type: "audio/wav" });
    }
    v.chunks = [];
    v.samples = [];
    releaseVoiceStream();
    if (!blob || !blob.size) {
      setVoiceUi();
      toast("Nothing was recorded — hold the microphone a moment longer.");
      return;
    }
    await uploadClip(blob);
  }

  async function postBinary(path, blob, seconds) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), seconds * 1000);
    try {
      return await fetch(path, {
        method: "POST",
        headers: { "X-Chat-Token": TOKEN,
                   "Content-Type": blob.type || "application/octet-stream" },
        body: blob,
        signal: controller.signal,
        cache: "no-store",
      });
    } finally {
      clearTimeout(timer);
    }
  }

  async function uploadClip(blob) {
    const v = state.voice;
    v.sending = true;
    setVoiceUi();
    toast("Transcribing locally…");
    try {
      const res = await postBinary("/api/voice", blob, apiTimeout("/api/voice"));
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!res.ok || !data) {
        const message = (data && data.error)
          || `the transcriber did not answer (${res.status})`;
        throw new Error(message);
      }
      if (!data.ok) throw new Error(data.error || "the clip was not understood");
      handleVoiceText(data.text);
    } catch (err) {
      const text = err && err.name === "AbortError"
        ? `no transcription after ${apiTimeout("/api/voice")} s`
        : String((err && err.message) || err);
      toast(`Voice input: ${text}`, "error");
      noteClientError(`voice: ${text}`, "/api/voice", "POST");
    } finally {
      v.sending = false;
      setVoiceUi();
    }
  }

  function handleVoiceText(text) {
    const clean = String(text || "").trim();
    if (!clean) {
      toast("I did not catch that — try again.", "error");
      return;
    }
    if (state.voice.autosend) {
      send(clean);
      return;
    }
    els.prompt.value = clean;
    autoGrow();
    els.prompt.focus();
    toast("Transcribed — correct it if needed, then press Enter.");
  }

  function toggleRecording() {
    if (state.voice.recording) stopRecording();
    else startRecording();
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  // ---------------------------------------------------------------- status

  function renderStatus(status) {
    state.status = status;
    const playing = Boolean(status.playing);
    const paused = Boolean(status.paused);

    if (playing) {
      els.npTitle.textContent = status.title || "playing";
      els.npArtist.textContent = status.artist || "";
      els.npTime.textContent = status.time || "0:00";
      els.npTotal.textContent = status.total || "0:00";
      els.npBar.style.width = `${Math.min(100, (status.progress || 0) * 100)}%`;
      els.npDot.className = "dot " + (paused ? "paused" : "live");
      els.npCover.classList.add("live");
      els.npToggle.innerHTML = paused ? icon("play") : icon("pause");
      els.statusLine.textContent = status.line || "";
      if (els.npFacts) els.npFacts.disabled = !status.rel;
      if (els.npTags) els.npTags.disabled = !status.rel;
    } else {
      els.npTitle.textContent = "Nothing yet";
      els.npArtist.textContent = "Ask me for something to play";
      els.npTime.textContent = "0:00";
      els.npTotal.textContent = "0:00";
      els.npBar.style.width = "0%";
      els.npDot.className = "dot";
      els.npCover.classList.remove("live");
      els.npToggle.innerHTML = icon("play");
      if (els.npFacts) els.npFacts.disabled = true;
      if (els.npTags) els.npTags.disabled = true;
      els.statusLine.textContent = "idle — ask for something to play";
    }
    if (status.error) els.statusLine.textContent = status.error;
    followLyrics(status);
    applyResume(state.resume);
    followQueue(status);
  }

  // the Up-next panel is a window on the player's queue, so it is refreshed
  // when the queue really moved (a new queue, a jump, a removal) instead of on
  // every two-second poll
  function followQueue(status) {
    if (!queueVisible()) return;
    const count = status.playing ? Number(status.playlist_count || 0) : 0;
    const pos = status.playing ? Number(status.playlist_pos || 0) : 0;
    if (`${count}:${pos}` !== state.queueKey) refreshQueue();
  }

  // the lyrics panel follows the playhead: load the song's lines once, then
  // only move the highlight
  function followLyrics(status) {
    if (!lyricsVisible()) return;
    const rel = status && status.rel ? status.rel : "";
    if (!rel) {
      if (state.lyricsRel || state.lyricsLoadedRel) {
        state.lyricsRel = "";
        state.lyricsLoadedRel = "";
        state.lyricsLines = [];
        state.lyricsIndex = -1;
        lyricsMeta("");
        lyricsNote("Nothing is playing — start a song and its lyrics follow it.");
      }
      return;
    }
    // a song with no lines must not be asked for again on every status poll
    if (rel !== state.lyricsLoadedRel) loadLyrics(rel);
    else highlightLyric();
  }

  async function pollStatus() {
    if (document.hidden || conn.down) return;
    try {
      // polling must not retry: the next poll is two seconds away anyway, and
      // the banner already says the server is not answering
      const status = await api("/api/status", undefined,
                               { retries: 0, timeout: 10 });
      state.statusAt = performance.now();
      renderStatus(status);
      maybeLoadWaveform(status);
      if (state.vizEnabled && status.playing && !status.paused) startVizLoop();
      if (state.status && state.status.volume) {
        const volume = Math.round(state.status.volume);
        if (document.activeElement !== els.volume) {
          els.volume.value = String(volume);
          els.volumeLabel.textContent = String(volume);
        }
      }
    } catch (err) {
      // keep the last known state; a network failure is already on the banner
      if (err && err.status) {
        els.statusLine.textContent = err.message;
      }
    }
  }

  // ------------------------------------------------------------ visualizer

  function applyViz() {
    const enabled = Boolean(state.vizEnabled);
    const now = $("#now");
    if (now) now.classList.toggle("viz-off", !enabled);
    if (els.vizToggle) {
      els.vizToggle.classList.toggle("on", enabled);
      els.vizToggle.title = enabled
        ? "Visualizer on — click to hide the waveform"
        : "Visualizer off — click to show the waveform";
    }
    if (els.vizStyle) {
      els.vizStyle.classList.toggle("on", enabled);
      els.vizStyle.title = `Visualizer style: ${VIZ_LABELS[vizStyleName()]}`
        + " — click to cycle";
    }
    if (!enabled) {
      stopVizLoop();
      return;
    }
    drawWaveform();
    if (state.status && state.status.playing) startVizLoop();
  }

  async function toggleVisualizer() {
    state.vizEnabled = !state.vizEnabled;
    applyViz();
    try {
      await api("/api/settings", { visualizer: state.vizEnabled });
    } catch (err) {
      toast(err.message, "error");
    }
    if (state.vizEnabled) {
      if (state.status && state.status.rel) loadWaveform(state.status.rel);
      else drawWaveform();
    }
  }

  function maybeLoadWaveform(status) {
    const rel = status && status.rel ? status.rel : "";
    if (!rel) {
      if (state.vizRel) {
        state.vizRel = "";
        state.waveform = null;
        drawWaveform();
      }
      return;
    }
    if (rel !== state.vizRel) loadWaveform(rel);
  }

  async function loadWaveform(rel) {
    state.vizRel = rel;
    state.waveform = null;
    if (!state.vizEnabled || !rel) {
      drawWaveform();
      return;
    }
    try {
      const data = await api(`/api/waveform?rel=${encodeURIComponent(rel)}`);
      if (state.vizRel !== rel) return;
      state.waveform = data && data.peaks && data.peaks.length ? data : null;
    } catch (_) {
      state.waveform = null;
    }
    drawWaveform();
    if (state.vizEnabled && state.status && state.status.playing) startVizLoop();
    loadVizData(rel);
  }

  // the spectrum and the profile are only fetched for the styles that draw
  // them, so the usual strip costs exactly one cached request per track
  async function loadVizData(rel, style = vizStyleName()) {
    if (!rel) return;
    if (style === "spectrum" && state.spectrumRel !== rel) {
      state.spectrum = null;
      state.spectrumRel = rel;
      state.fftHold = [];
      drawWaveform();
      try {
        const data = await api(`/api/spectrum?rel=${encodeURIComponent(rel)}`);
        if (state.spectrumRel === rel) {
          state.spectrum = data && (data.frames || []).length ? data : null;
        }
      } catch (_) {
        state.spectrum = null;
      }
      drawWaveform();
      return;
    }
    if (style === "axes" && state.axesRel !== rel) {
      state.axes = null;
      state.axesRel = rel;
      drawWaveform();
      try {
        const data = await api(`/api/psycho?rel=${encodeURIComponent(rel)}`);
        if (state.axesRel === rel) {
          state.axes = data && (data.axes || []).length ? data : null;
        }
      } catch (_) {
        state.axes = null;
      }
      drawWaveform();
    }
  }

  function vizProgress() {
    const status = state.status;
    if (!status || !status.duration) return 0;
    let position = Number(status.position) || 0;
    if (status.playing && !status.paused && state.statusAt) {
      position += (performance.now() - state.statusAt) / 1000;
    }
    return Math.max(0, Math.min(1, position / status.duration));
  }

  // five ways to draw the same cached peak envelope; the style is remembered
  // in profile.json so the strip looks the same after a restart
  const VIZ_STYLES = ["wave", "eq", "leds", "scope", "vu", "spectrum", "axes"];
  const VIZ_LABELS = { wave: "bars", eq: "equalizer", leds: "LED columns",
                       scope: "oscilloscope", vu: "VU meters",
                       spectrum: "live spectrum", axes: "sound properties" };

  // the server owns the list (viz.STYLES); the copy here is only a fallback
  function vizStyleList() {
    return Array.isArray(state.vizStyles) && state.vizStyles.length
      ? state.vizStyles
      : VIZ_STYLES;
  }

  function vizStyleName() {
    const list = vizStyleList();
    return list.includes(state.vizStyle) ? state.vizStyle : list[0] || "wave";
  }

  function clamp01(value) {
    const number = Number(value) || 0;
    return Math.max(0.02, Math.min(1, number));
  }

  function vizPhase() {
    const status = state.status;
    const live = status && status.playing && !status.paused;
    return live ? performance.now() / 1000 : 0;
  }

  function vizSample(peaks, position) {
    const n = peaks.length;
    if (!n) return 0;
    const x = Math.max(0, Math.min(1, position)) * (n - 1);
    const index = Math.floor(x);
    const t = x - index;
    const a = Number(peaks[index]) || 0;
    const b = Number(peaks[Math.min(n - 1, index + 1)]) || 0;
    return clamp01(a * (1 - t) + b * t);
  }

  function drawWaveform() {
    const canvas = els.npWave;
    if (!canvas || !state.vizEnabled) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = window.devicePixelRatio || 1;
    const width = Math.floor(rect.width);
    const height = Math.floor(rect.height);
    if (canvas.width !== Math.floor(width * dpr)
        || canvas.height !== Math.floor(height * dpr)) {
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const peaks = (state.waveform && state.waveform.peaks) || [];
    const wanted = vizStyleName();
    if (!peaks.length && wanted !== "spectrum" && wanted !== "axes") {
      const mid = height * 0.52;
      ctx.strokeStyle = "rgba(167, 139, 250, .18)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(8, mid);
      ctx.lineTo(width - 8, mid);
      ctx.stroke();
      return;
    }
    const progress = vizProgress();
    const style = vizStyleName();
    if (style === "eq") drawEq(ctx, width, height, peaks, progress);
    else if (style === "leds") drawLeds(ctx, width, height, peaks, progress);
    else if (style === "scope") {
      drawScope(ctx, width, height, peaks, progress, vizPhase());
    } else if (style === "vu") drawVu(ctx, width, height, peaks, progress);
    else if (style === "spectrum") drawSpectrum(ctx, width, height, progress);
    else if (style === "axes") drawAxes(ctx, width, height);
    else drawBars(ctx, width, height, peaks, progress);
    if (style !== "vu" && style !== "spectrum" && style !== "axes") {
      vizPlayhead(ctx, width, height, progress);
    }
  }

  // a small note where the drawing would be (no data yet, or a broken file)
  function vizNotice(ctx, width, height, text) {
    ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "rgba(233, 234, 240, .38)";
    ctx.fillText(text, width / 2, height / 2);
    ctx.textAlign = "left";
  }

  function spectrumWindow() {
    // the bands of the last second before the playhead, averaged
    const data = state.spectrum;
    const bands = data.bands;
    const hop = Number(data.hop) || 0.25;
    const seconds = Number(data.seconds) || 0;
    const duration = Number((state.status || {}).duration) || seconds;
    const live = vizProgress() * (duration || seconds);
    const at = Math.max(0, Math.min(seconds || live, live));
    const last = Math.min(data.frames.length - 1, Math.floor(at / hop));
    const first = Math.max(0, Math.floor((at - 1) / hop));
    const out = new Array(bands).fill(0);
    let count = 0;
    for (let i = first; i <= last; i += 1) {
      const row = data.frames[i];
      if (!row) continue;
      count += 1;
      for (let b = 0; b < bands; b += 1) out[b] += Number(row[b]) || 0;
    }
    return count ? out.map((value) => value / count) : out;
  }

  function drawSpectrum(ctx, width, height, progress) {
    const data = state.spectrum;
    if (!data) {
      vizNotice(ctx, width, height,
        state.spectrumRel ? "measuring the spectrum…" : "no spectrum yet");
      return;
    }
    const values = spectrumWindow();
    const bands = values.length;
    if (!bands) {
      vizNotice(ctx, width, height, "no spectrum for this file");
      return;
    }
    const playing = state.status && state.status.playing && !state.status.paused;
    const hold = (state.fftHold || []).slice(0, bands);
    while (hold.length < bands) hold.push(0);
    const gap = 1.6;
    const bar = Math.max(1, width / bands - gap);
    const base = height - 2;
    const room = height - 6;
    for (let i = 0; i < bands; i += 1) {
      const value = Math.max(0, Math.min(100, values[i])) / 100;
      // bars fall away rather than jump; the hold line keeps the peak visible
      hold[i] = playing ? Math.max(value, hold[i] * 0.90) : value;
      const x = i * (bar + gap);
      const h = Math.max(1.5, value * room);
      const hue = 190 + (i / Math.max(1, bands - 1)) * 100;
      ctx.fillStyle = `hsla(${hue}, 88%, 68%, .92)`;
      ctx.fillRect(x, base - h, bar, h);
      const peakY = base - Math.max(h, hold[i] * room);
      ctx.fillStyle = `hsla(${hue}, 70%, 82%, .55)`;
      ctx.fillRect(x, peakY, bar, 1);
    }
    state.fftHold = hold;
    // the band width is logarithmic, so mark the octaves for orientation
    ctx.fillStyle = "rgba(233, 234, 240, .22)";
    ctx.font = "8px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textBaseline = "top";
    const edges = data.edges || [];
    ["100", "1k", "10k"].forEach((label) => {
      const hz = label === "100" ? 100 : label === "1k" ? 1000 : 10000;
      let index = -1;
      for (let i = 0; i < edges.length - 1; i += 1) {
        if (edges[i] <= hz && hz < edges[i + 1]) { index = i; break; }
      }
      if (index < 0) return;
      ctx.fillText(label, index * (bar + gap) + 1, 1);
    });
    ctx.textBaseline = "middle";
  }

  function drawAxes(ctx, width, height) {
    const data = state.axes;
    if (!data) {
      vizNotice(ctx, width, height,
        state.axesRel ? "reading the profile…" : "no profile yet");
      return;
    }
    const values = data.values || {};
    const axes = (data.axes || []).filter((axis) => axis.key in values);
    if (!axes.length) {
      vizNotice(ctx, width, height, "not measured yet");
      return;
    }
    const gap = 2.4;
    const cell = Math.max(6, width / axes.length);
    const bar = Math.max(2, cell - gap);
    const base = height - 11;
    const room = height - 14;
    ctx.font = "7.5px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    axes.forEach((axis, index) => {
      const value = Math.max(0, Math.min(1, Number(values[axis.key]) || 0));
      const x = index * cell + gap / 2;
      const h = Math.max(2, value * room);
      ctx.fillStyle = `hsla(${250 - value * 70}, 85%, ${52 + value * 22}%, .92)`;
      ctx.fillRect(x, base - h, bar, h);
      ctx.fillStyle = "rgba(233, 234, 240, .30)";
      ctx.fillRect(x, base + 1, bar, 1);
      ctx.fillStyle = "rgba(233, 234, 240, .55)";
      const label = String(axis.label || axis.key).slice(0, 3);
      ctx.fillText(label, x + bar / 2, base + 3);
    });
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
  }

  function drawBars(ctx, width, height, peaks, progress) {
    const mid = height * 0.52;
    const gap = 1.4;
    const bar = Math.max(1, width / peaks.length - gap);
    for (let i = 0; i < peaks.length; i += 1) {
      const peak = clamp01(peaks[i]);
      const x = i * (bar + gap);
      const h = Math.max(2, peak * height * 0.86);
      const played = (i + 0.5) / peaks.length <= progress;
      const hue = 262 - (i / peaks.length) * 62;
      ctx.fillStyle = played
        ? `hsla(${hue}, 88%, 72%, .95)`
        : `hsla(${hue}, 62%, 58%, .36)`;
      ctx.fillRect(x, mid - h / 2, bar, h);
    }
  }

  function drawEq(ctx, width, height, peaks, progress) {
    const base = height - 3;
    const room = height - 8;
    const gap = 1.6;
    const bar = Math.max(1, width / peaks.length - gap);
    for (let i = 0; i < peaks.length; i += 1) {
      const peak = clamp01(peaks[i]);
      const x = i * (bar + gap);
      const h = Math.max(1.5, peak * room);
      const played = (i + 0.5) / peaks.length <= progress;
      const hue = 272 - (i / peaks.length) * 86;
      ctx.fillStyle = played
        ? `hsla(${hue}, 90%, 68%, .95)`
        : `hsla(${hue}, 60%, 52%, .30)`;
      ctx.fillRect(x, base - h, bar, h);
      ctx.fillStyle = played
        ? `hsla(${hue}, 90%, 68%, .16)`
        : `hsla(${hue}, 60%, 52%, .07)`;
      ctx.fillRect(x, base + 1, bar, Math.min(10, h * 0.32));
    }
  }

  function drawLeds(ctx, width, height, peaks, progress) {
    const segment = 5;
    const gapY = 1.6;
    const gapX = 1.8;
    const rows = Math.max(3, Math.floor((height - 6) / (segment + gapY)));
    const bar = Math.max(1.4, width / peaks.length - gapX);
    const base = height - 2;
    for (let i = 0; i < peaks.length; i += 1) {
      const lit = Math.round(clamp01(peaks[i]) * rows);
      const x = i * (bar + gapX);
      const played = (i + 0.5) / peaks.length <= progress;
      for (let row = 0; row < rows; row += 1) {
        const y = base - (row + 1) * (segment + gapY) + gapY;
        const heat = row / Math.max(1, rows - 1);
        const hue = 148 - heat * 148;
        if (row >= lit) {
          ctx.fillStyle = played
            ? "rgba(255, 255, 255, .07)"
            : "rgba(255, 255, 255, .035)";
        } else {
          ctx.fillStyle = played
            ? `hsla(${hue}, 88%, ${66 - heat * 14}%, .96)`
            : `hsla(${hue}, 62%, ${46 - heat * 8}%, .42)`;
        }
        ctx.fillRect(x, y, bar, segment);
      }
    }
  }

  function drawScope(ctx, width, height, peaks, progress, phase) {
    const mid = height * 0.52;
    const amp = height * 0.4;
    const count = Math.max(24, Math.min(220, Math.floor(width / 2.4)));
    const points = [];
    const drift = phase * 1.9;
    for (let i = 0; i <= count; i += 1) {
      const t = i / count;
      const envelope = vizSample(peaks, t);
      const carrier = Math.sin(drift + t * 31) * 0.58
        + Math.sin(drift * 1.63 + t * 67) * 0.42;
      points.push([t * width, mid - envelope * amp * carrier]);
    }
    ctx.beginPath();
    ctx.moveTo(0, mid);
    points.forEach(([x, y]) => ctx.lineTo(x, y));
    ctx.lineTo(width, mid);
    ctx.closePath();
    const glow = ctx.createLinearGradient(0, 0, width, 0);
    glow.addColorStop(0, "rgba(167, 139, 250, .26)");
    glow.addColorStop(1, "rgba(56, 189, 248, .2)");
    ctx.fillStyle = glow;
    ctx.fill();

    const trace = () => {
      ctx.beginPath();
      points.forEach(([x, y], index) => {
        if (index) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
      });
      ctx.stroke();
    };
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = "rgba(196, 181, 253, .32)";
    trace();
    const cutoff = Math.max(0, Math.min(1, progress));
    const played = points.filter(([x]) => x <= cutoff * width);
    if (played.length > 1) {
      ctx.lineWidth = 1.9;
      ctx.strokeStyle = "rgba(221, 214, 254, .92)";
      ctx.beginPath();
      played.forEach(([x, y], index) => {
        if (index) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
      });
      ctx.stroke();
    }
  }

  function drawVu(ctx, width, height, peaks, progress) {
    const left = 13;
    const right = width - 5;
    const span = Math.max(12, right - left);
    const segments = 26;
    const cell = span / segments;
    const meterH = Math.max(4, Math.min(9, height * 0.17));
    const gap = 2.4;

    const at = Math.round(Math.max(0, Math.min(1, progress)) * (peaks.length - 1));
    const window = Math.max(4, Math.round(peaks.length * 0.03));
    const slice = peaks.slice(Math.max(0, at - window), at + window + 1);
    const rms = Math.sqrt(slice.reduce((sum, peak) => sum + clamp01(peak) ** 2, 0)
      / Math.max(1, slice.length));
    const levels = [Math.min(1, rms * 1.34), Math.min(1, rms * 1.12)];
    const holds = [Math.min(1, (Math.max(0, ...slice) || 0) * 1.12),
                   Math.min(1, (Math.max(0, ...slice) || 0) * 0.95)];

    ctx.font = "9px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textBaseline = "middle";
    levels.forEach((level, channel) => {
      const y = channel === 0 ? height * 0.27 : height * 0.27 + meterH + gap;
      ctx.fillStyle = "rgba(233, 234, 240, .38)";
      ctx.textAlign = "left";
      ctx.fillText(channel === 0 ? "L" : "R", 1, y + meterH / 2);
      for (let i = 0; i < segments; i += 1) {
        const heat = i / (segments - 1);
        const lit = i / segments <= level;
        const held = i / segments <= holds[channel] && i / segments > level;
        const x = left + i * cell;
        const w = Math.max(1, cell - 1.4);
        if (lit) {
          const hue = 146 - heat * 146;
          ctx.fillStyle = `hsla(${hue}, 88%, ${64 - heat * 10}%, .94)`;
          ctx.fillRect(x, y, w, meterH);
        } else {
          ctx.fillStyle = "rgba(255, 255, 255, .06)";
          ctx.fillRect(x, y, w, meterH);
          if (held) {
            ctx.fillStyle = `hsla(${146 - heat * 146}, 82%, 60%, .6)`;
            ctx.fillRect(x, y, w, meterH);
          }
        }
      }
    });
    ctx.fillStyle = "rgba(255, 255, 255, .22)";
    ctx.fillRect(0, height - 1.5, width * Math.max(0, Math.min(1, progress)), 1.5);
  }

  function vizPlayhead(ctx, width, height, progress) {
    if (progress <= 0) return;
    ctx.fillStyle = "rgba(255, 255, 255, .55)";
    ctx.fillRect(Math.min(width - 1, progress * width), 4, 1, height - 8);
  }

  async function cycleVizStyle() {
    const list = vizStyleList();
    const index = list.indexOf(vizStyleName());
    state.vizStyle = list[(index + 1) % list.length];
    state.vizEnabled = true;
    applyViz();
    drawWaveform();
    if (state.status && state.status.rel) {
      loadVizData(state.status.rel, state.vizStyle);
    }
    if (state.status && state.status.playing) startVizLoop();
    toast(`Visualizer: ${VIZ_LABELS[state.vizStyle]}`);
    try {
      await api("/api/settings", { viz_style: state.vizStyle });
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function startVizLoop() {
    if (state.vizFrame) return;
    const step = () => {
      drawWaveform();
      if (state.vizEnabled && state.status && state.status.playing
          && !state.status.paused) {
        state.vizFrame = requestAnimationFrame(step);
      } else {
        state.vizFrame = null;
      }
    };
    state.vizFrame = requestAnimationFrame(step);
  }

  function stopVizLoop() {
    if (state.vizFrame) cancelAnimationFrame(state.vizFrame);
    state.vizFrame = null;
  }

  async function transport(action, value) {
    try {
      const reply = await api("/api/transport", { action, value });
      if (reply.error) toast(plainText(reply.html) || "That did not work.", "error");
      pollStatus();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ---------------------------------------------------------------- meta UI

  function formatNumber(value) {
    return Number(value || 0).toLocaleString("en-US");
  }

  // absolute wall clock of the server start, so "up 8 s" reads against a
  // real time and cannot look like a counter that got stuck
  function formatClock(seconds) {
    const value = Number(seconds) || 0;
    if (!value) return "";
    const when = new Date(value * 1000);
    const time = when.toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    if (when.toDateString() === new Date().toDateString()) return time;
    return `${when.toLocaleDateString([], { day: "2-digit", month: "2-digit" })} ${time}`;
  }

  function formatUptime(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (hours) return `${hours} h ${minutes} min`;
    if (minutes) return `${minutes} min`;
    return `${total} s`;
  }

  // --------------------------------------------------------- library index
  //
  // The index job is the same detached pipeline `music index` runs.  A small
  // chip in the stats line shows it while it works, so nobody has to open a
  // dialog to know whether the library is being re-read.

  function renderIndexChip(job) {
    if (!els.indexChip) return;
    state.index = job || null;
    const active = Boolean(job && (job.active || job.running));
    els.indexChip.hidden = !active;
    if (!active) return;
    els.indexChip.dataset.state = String(job.state || "");
    const percent = Math.round(Number(job.percent) || 0);
    const detail = job.phase === "scan" && job.total
      ? `${formatNumber(job.done)}/${formatNumber(job.total)}`
      : (job.step || "working");
    if (job.state === "paused") {
      els.indexChip.textContent = `index paused · ${percent} %`;
      els.indexChip.title = `Paused at ${detail} — click for the details`;
      return;
    }
    if (job.state === "cancelling") {
      els.indexChip.textContent = "index stopping…";
      els.indexChip.title = "The scan is ending — click for the details";
      return;
    }
    els.indexChip.textContent = `indexing ${percent} % · ${detail}`;
    els.indexChip.title =
      `${job.step || "Indexing the library"} — click for the details`;
  }

  function stopIndexWatch() {
    if (state.indexTimer) clearInterval(state.indexTimer);
    state.indexTimer = null;
  }

  async function pollIndex() {
    let job;
    try {
      job = await api("/api/index/update", undefined,
                      { retries: 0, timeout: 15 });
    } catch (_) {
      return;                             // the next poll is a moment away
    }
    const wasRunning = state.indexWasRunning;
    const active = Boolean(job && (job.active || job.running));
    state.indexWasRunning = active;
    renderIndexChip(job);
    if (active) return;
    stopIndexWatch();
    if (!wasRunning) return;
    const counts = (job && job.counts) || {};
    if (job && job.state === "done") {
      toast(`Library index updated · ${formatNumber(counts.tracks)} tracks`,
            "good");
      await refreshMeta();                // the catalogue really changed
    } else if (job && job.state === "cancelled") {
      toast("Library index cancelled", "");
      await refreshMeta();
    } else if (job && job.state === "failed") {
      toast("The library index stopped — open Settings → Library index "
            + "for the log.", "error");
      await refreshMeta();
    }
  }

  function watchIndex(job) {
    renderIndexChip(job);
    if (job && (job.active || job.running)) {
      state.indexWasRunning = true;
      if (!state.indexTimer) state.indexTimer = setInterval(pollIndex, 2500);
    } else if (state.indexWasRunning) {
      state.indexWasRunning = false;
      pollIndex();                        // pick up the final state once
    }
  }

  function renderStats(stats) {
    els.stats.textContent =
      `${formatNumber(stats.files)} tracks · ` +
      `${formatNumber(Math.round(stats.hours || 0))} h · ` +
      `${formatNumber(stats.artists)} artists`;
    els.stats.title =
      `${formatNumber(stats.features)} analysed · ` +
      `${formatNumber(stats.playlists ? Object.values(stats.playlists)
        .reduce((a, b) => a + b, 0) : 0)} playlists`;
  }

  const KIND_ORDER = ["custom", "own", "smart", "shelf", "psycho", "mood",
                      "focus"];
  const KIND_LABEL = {
    shelf: "Shelves", mood: "Moods", focus: "Focus", custom: "Mine",
    own: "Ad-hoc", psycho: "Sound families", smart: "Smart",
  };

  function renderPlaylists(playlists) {
    els.playlistList.innerHTML = "";
    const groups = {};
    (playlists || []).forEach((pl) => {
      (groups[pl.kind] = groups[pl.kind] || []).push(pl);
    });
    KIND_ORDER.concat(Object.keys(groups).filter((k) => !KIND_ORDER.includes(k)))
      .forEach((kind) => {
        const items = groups[kind];
        if (!items || !items.length) return;
        if (kind === "custom" && !items.length) return;
        const head = document.createElement("div");
        head.className = "pl-group";
        head.textContent = KIND_LABEL[kind] || kind;
        els.playlistList.appendChild(head);
        items.forEach((pl) => {
          const button = document.createElement("button");
          button.className = "pl-item";
          button.dataset.playlist = pl.name;
          button.title = `${pl.name} — ${formatNumber(pl.count)} tracks (${pl.kind})`;
          button.innerHTML = icon("list");
          const name = document.createElement("span");
          name.className = "name";
          name.textContent = pl.name.replace(/_/g, " ");
          const count = document.createElement("span");
          count.className = "count";
          count.textContent = formatNumber(pl.count);
          button.append(name, count);
          els.playlistList.appendChild(button);
        });
      });
    if (!els.playlistList.children.length) {
      const empty = document.createElement("div");
      empty.className = "pl-group";
      empty.textContent = "no playlists yet — ask for one";
      els.playlistList.appendChild(empty);
    }
  }

  function renderChips(suggestions) {
    els.chips.innerHTML = "";
    (suggestions || []).slice(0, 7).forEach((text) => {
      const chip = document.createElement("button");
      chip.className = "chip";
      chip.type = "button";
      chip.dataset.send = text;
      chip.textContent = text;
      els.chips.appendChild(chip);
    });
  }

  function renderPlanner(planner) {
    const enabled = Boolean(planner && planner.enabled);
    const available = Boolean(planner && planner.available);
    els.plannerToggle.classList.toggle("on", enabled);
    els.plannerLabel.textContent = enabled
      ? (available ? "LLM planner" : "LLM (unavailable)")
      : "local planner";
    els.plannerToggle.title = enabled
      ? "The optional language model is on — click for the offline planner only"
      : "The offline planner is on — click to use the language model too";
  }

  function applyRadio(radio) {
    if (!els.radioToggle) return;
    const on = Boolean(radio && radio.on);
    els.radioToggle.classList.toggle("on", on);
    els.radioToggle.setAttribute("aria-pressed", on ? "true" : "false");
    const label = radio && radio.label ? String(radio.label) : "mix";
    els.radioToggle.title = on
      ? `Radio on · ${label} — click to stop adding tracks`
      : "Radio mode: keep playing tracks that sound like this one";
  }

  function applyEndless(info) {
    if (!els.endlessToggle) return;
    const on = Boolean(info && info.on);
    const repeating = Boolean(info && info.repeat);
    els.endlessToggle.classList.toggle("on", on);
    els.endlessToggle.setAttribute("aria-pressed", on ? "true" : "false");
    els.endlessToggle.classList.toggle("warn", on && repeating);
    els.endlessToggle.title = on
      ? (repeating
        ? "Endless mode · repeating the queue (nothing fitting was left) — click to stop"
        : "Endless mode on — it grows from the song that just played; click to stop")
      : "Endless mode: keep adding music that fits the song that just played, and repeat when nothing fits";
  }

  function applyShuffle(value) {
    if (!els.shuffleToggle) return;
    const on = Boolean(value);
    els.shuffleToggle.classList.toggle("on", on);
    els.shuffleToggle.setAttribute("aria-pressed", on ? "true" : "false");
    els.shuffleToggle.title = on
      ? "Shuffle mode on — playlists start in random order; click to turn it off"
      : "Shuffle mode: start playlists in random order";
  }

  function renderMeta(stateData) {
    state.meta = stateData;
    renderStats(stateData.stats);
    renderPlaylists(stateData.playlists);
    renderPlanner(stateData.planner);
    watchIndex(stateData.index);
    if (!state.ready) renderChips(stateData.suggestions);
    els.brandRoot.textContent = stateData.root;
    els.brandRoot.title = stateData.root;
    els.statusMeta.innerHTML = "";
    const bits = [];
    bits.push(stateData.dry_run ? "dry run" : "journalled");
    bits.push(`${formatNumber(stateData.playlists.length)} playlists`);
    bits.push("AI-assisted · GPL-3.0-or-later");
    els.statusMeta.textContent = bits.join(" · ");
    if (stateData.dry_run) {
      els.topbarSub.textContent = "dry run — playlists are planned but never written";
      els.statusLine.textContent = "dry run active";
    }
    const settings = stateData.settings || {};
    if (Object.prototype.hasOwnProperty.call(settings, "visualizer")) {
      state.vizEnabled = Boolean(settings.visualizer);
    }
    if (settings.viz_style) state.vizStyle = String(settings.viz_style);
    if (Array.isArray(settings.viz_styles) && settings.viz_styles.length) {
      state.vizStyles = settings.viz_styles.map(String);
    }
    applyViz();
    if (Object.prototype.hasOwnProperty.call(settings, "lyrics_panel")) {
      applyLyrics(settings.lyrics_panel);
    }
    if (stateData.voice) applyVoice(stateData.voice);
    applyDownloads(stateData.downloads);
    applyResume(stateData.resume);
    applyRadio(stateData.radio);
    applyEndless(stateData.endless);
    applyShuffle(stateData.shuffle_mode);
    state.ready = true;
  }

  function applyDownloads(info) {
    // Download support is an optional build-time module.  Without one the
    // open player has no import button and no queue; nothing else changes.
    const on = Boolean(info && info.available);
    state.downloads = on;
    const button = $("#btn-import");
    if (button) {
      button.hidden = !on;
      button.disabled = !on;
      button.title = on ? "Import audio from the download module"
                        : "No download module is installed in this build";
    }
  }

  async function refreshMeta() {
    try {
      renderMeta(await api("/api/state"));
    } catch (err) {
      toast(`Could not read the library: ${err.message}`, "error");
    }
  }

  // --------------------------------------------------- background work
  //
  // The footer carries the jobs that fill the library while the window is
  // open: the inventory index, the lyric transcription and the sound profile.
  // Each line shows its progress and offers a pause/start and a refresh
  // button, so none of them has to be driven from a shell.

  function workChip(job) {
    const chip = document.createElement("span");
    chip.className = "work-chip";
    chip.dataset.job = String(job.key || "");
    chip.dataset.jobId = String(job.job || "");
    chip.dataset.state = String(job.state || "idle");
    if (job.active) chip.classList.add("active");
    if (job.paused) chip.classList.add("paused");
    if (job.retry) chip.classList.add("attention");
    // the whole downloads chip is the door to the queue list
    const opens = job.key === "downloads";
    if (opens) {
      chip.classList.add("work-chip-link");
      chip.tabIndex = 0;
      chip.setAttribute("role", "button");
    }

    const dot = document.createElement("span");
    dot.className = "work-dot";
    chip.appendChild(dot);

    const label = document.createElement("span");
    label.className = "work-label";
    label.textContent = job.label || job.key || "";
    chip.appendChild(label);

    const value = document.createElement("span");
    value.className = "work-value";
    value.textContent = job.value || "";
    chip.appendChild(value);

    // a job that is ending keeps its place, but there is nothing to press
    if (job.stopping || job.state === "cancelling") {
      value.textContent = "stopping…";
      chip.title = `The ${job.label} job is finishing`;
      return chip;
    }

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "work-btn";
    const action = job.paused ? "resume"
      : (job.active ? "pause" : (job.retry ? "retry" : "start"));
    toggle.dataset.work = action;
    toggle.title = job.paused
      ? `Continue the ${job.label} job`
      : job.active
        ? `Pause the ${job.label} job`
        : job.retry
          ? `Try this download again`
          : `Start the ${job.label} job`;
    toggle.innerHTML = icon(job.active && !job.paused ? "pause" : "play");
    // an empty download queue has nothing to start; it keeps the refresh
    if (job.startable !== false) chip.appendChild(toggle);

    const refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "work-btn";
    refresh.dataset.work = "refresh";
    refresh.title = `Check the ${job.label} job and start it if there is work`;
    refresh.innerHTML = icon("refresh");
    chip.appendChild(refresh);

    // a failed import stays until it is retried or cleared
    if (job.dismissible) {
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "work-btn";
      clear.dataset.work = "dismiss";
      clear.title = "Clear this failed download from the footer";
      clear.innerHTML = icon("close");
      chip.appendChild(clear);
    }

    const bits = [];
    if (opens) bits.push("show the download queue");
    if (job.retry) bits.push("needs attention");
    if (job.detail) bits.push(job.detail);
    if (job.state === "paused") bits.push("paused");
    if (job.total) {
      bits.push(`${formatNumber(job.done)} of ${formatNumber(job.total)}`);
    }
    if (bits.length) chip.title = bits.join(" · ");
    return chip;
  }

  function renderWork(jobs) {
    state.work = Array.isArray(jobs) ? jobs : [];
    if (!els.work) return;
    els.work.innerHTML = "";
    state.work.forEach((job) => els.work.appendChild(workChip(job)));
  }

  async function refreshWork() {
    if (state.workBusy || document.hidden || conn.down) return;
    state.workBusy = true;
    let data = null;
    try {
      data = await api("/api/work", undefined, { retries: 0, timeout: 15 });
    } catch (_) {
      data = null;                        // the next poll is a moment away
    } finally {
      state.workBusy = false;
    }
    if (data) renderWork(data.jobs);
    scheduleWork();
  }

  function scheduleWork() {
    if (state.workTimer) clearTimeout(state.workTimer);
    const active = state.work.some((job) => job.active);
    // a running job is worth watching; an idle library only needs a slow look
    state.workTimer = setTimeout(refreshWork, active ? 2500 : 15000);
  }

  async function workAction(job, action, id) {
    let data;
    try {
      data = await api("/api/work", { job, action, id: id || "" },
                       { timeout: 30 });
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    if (data.jobs) renderWork(data.jobs);
    const status = (data && data.status) || {};
    if (data.error || status.error) {
      toast(data.error || status.error, "error");
    } else if (status.note) {
      toast(status.note, "");
    } else if (action === "pause") {
      toast(`${job} job paused`, "");
    } else if (action === "resume") {
      toast(`${job} job continued`, "ok");
    } else if (action === "retry") {
      toast("Download queued again", "ok");
    } else if (action === "dismiss") {
      toast("Failed download cleared", "");
    } else if (action === "cancel") {
      toast("Download cancelled", "");
    } else if (action === "up" || action === "down") {
      toast("Queue reordered", "");
    } else {
      toast(`${job} job running`, "ok");
    }
    scheduleWork();
    if (action !== "pause") refreshMeta();   // the counts moved
  }

  // ---------------------------------------------------------------- modal

  let modalCloser = null;

  function openModal(title, build, options) {
    if (modalCloser) modalCloser();
    els.modalTitle.textContent = title;
    els.modalBody.innerHTML = "";
    els.modal.classList.toggle("wide", Boolean(options && options.wide));
    els.modal.hidden = false;
    const close = () => {
      modalCloser = null;
      els.modal.hidden = true;
      els.modal.classList.remove("wide");
    };
    modalCloser = close;
    const built = build(close);
    if (built) els.modalBody.appendChild(built);
    const first = $("input, textarea, select", els.modalBody) || $("#modal-close");
    if (first) first.focus();
  }

  function closeModal() {
    if (modalCloser) modalCloser();
  }

  function field(label, input, hint) {
    const wrap = document.createElement("div");
    wrap.className = "field";
    const tag = document.createElement("label");
    tag.textContent = label;
    if (hint) {
      const opt = document.createElement("span");
      opt.className = "opt";
      opt.textContent = " " + hint;
      tag.appendChild(opt);
    }
    wrap.append(tag, input);
    return wrap;
  }

  function input(placeholder, value = "", type = "text") {
    const node = document.createElement("input");
    node.type = type;
    node.placeholder = placeholder;
    node.value = value;
    return node;
  }

  let searchTimer = null;

  //: what a playlist kind is called in the search results
  const PLAYLIST_KINDS = {
    shelf: "shelf list",
    mood: "mood list",
    focus: "focus list",
    custom: "my playlist",
    psycho: "sound family",
  };

  function openSearch(prefill = "") {
    openModal("Search the library", () => {
      const wrap = document.createElement("div");
      const row = document.createElement("div");
      row.className = "search-row";
      const box = input("title, artist, album, genre, shelf, file name or playlist",
                        prefill);
      box.type = "search";
      row.appendChild(box);
      const count = document.createElement("span");
      count.className = "search-count";
      row.appendChild(count);
      wrap.appendChild(row);

      const results = document.createElement("div");
      results.className = "search-results";
      const hint = document.createElement("p");
      hint.className = "note";
      hint.textContent =
        "Type any part of the metadata, a file name or a playlist; click a " +
        "hit to load it into the player.";
      results.appendChild(hint);
      wrap.appendChild(results);

      const group = (label) => {
        const head = document.createElement("div");
        head.className = "search-group";
        head.textContent = label;
        results.appendChild(head);
      };

      // one row shape for both kinds of hit: name, a line of detail, a line
      // of facts; clicking always hands the target to the player
      const hitButton = (title, detail, where) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "search-hit";
        const main = document.createElement("span");
        main.className = "what";
        main.textContent = title;
        const who = document.createElement("span");
        who.className = "who";
        who.textContent = detail;
        const meta = document.createElement("span");
        meta.className = "where";
        meta.textContent = where;
        button.append(main, who, meta);
        return button;
      };

      const render = (data) => {
        results.innerHTML = "";
        const playlists = data.playlists || [];
        const parts = [];
        if (playlists.length) {
          parts.push(`${playlists.length} playlist${playlists.length === 1 ? "" : "s"}`);
        }
        parts.push(`${data.count} hit${data.count === 1 ? "" : "s"}`);
        count.textContent = parts.join(" · ");
        const lyricHits = data.lyrics || [];
        if (lyricHits.length) {
          parts.push(`${lyricHits.length} lyric line${lyricHits.length === 1 ? "" : "s"}`);
        }
        count.textContent = parts.join(" · ");
        if (!data.hits.length && !playlists.length && !lyricHits.length) {
          const empty = document.createElement("p");
          empty.className = "note";
          empty.textContent = `Nothing matches “${data.query}”.`;
          results.appendChild(empty);
          return;
        }
        if (playlists.length) {
          group("Playlists");
          playlists.forEach((item) => {
            const button = hitButton(
              item.name,
              [
                `${item.count.toLocaleString()} track${item.count === 1 ? "" : "s"}`,
                PLAYLIST_KINDS[item.kind] || item.kind,
              ].join(" — "),
              item.path);
            button.title = "Load this playlist into the player";
            button.onclick = () => {
              play(item.path);
              toast(`Loading ${item.name} into the player.`);
            };
            results.appendChild(button);
          });
        }
        if (data.hits.length) {
          if (playlists.length) group("Tracks");
          data.hits.forEach((hit) => {
            const button = hitButton(
              hit.title || hit.rel,
              [hit.artist, hit.album].filter(Boolean).join(" — "),
              [hit.genre, hit.duration, hit.rel].filter(Boolean).join(" · "));
            button.title = "Load this track into the player";
            button.onclick = () => {
              play(hit.rel);
              toast(`Loading ${hit.title || hit.rel} into the player.`);
            };
            const line = document.createElement("div");
            line.className = "search-item";
            const inspect = document.createElement("button");
            inspect.type = "button";
            inspect.className = "icon-btn tiny inspect";
            inspect.title = "Show the sound properties of this track";
            inspect.innerHTML = '<svg><use href="#i-brain"/></svg>';
            inspect.onclick = () => openPsycho(hit.rel);
            const edit = document.createElement("button");
            edit.type = "button";
            edit.className = "icon-btn tiny inspect";
            edit.title = "Edit this track's tags and artwork";
            edit.innerHTML = '<svg><use href="#i-pencil"/></svg>';
            edit.onclick = () => openTags(hit.rel);
            line.append(button, inspect, edit);
            results.appendChild(line);
          });
        }
        if (lyricHits.length) {
          group("Lyrics");
          lyricHits.forEach((hit) => {
            const where = [hit.artist, hit.start ? lyricClock(hit.start) : "",
                           hit.also ? `+${hit.also} more` : ""]
              .filter(Boolean).join(" · ");
            const button = hitButton(hit.title || hit.rel,
                                     `“${hit.line}”`, where);
            button.title = "Play this song from the line it sings";
            button.onclick = async () => {
              await play(hit.rel);
              const at = Number(hit.start) || 0;
              if (at > 0) setTimeout(() => transport("seek_abs", at), 500);
            };
            results.appendChild(button);
          });
        }
      };

      const run = async () => {
        const query = box.value.trim();
        if (!query) {
          count.textContent = "";
          results.innerHTML = "";
          results.appendChild(hint);
          return;
        }
        try {
          render(await api(`/api/search?q=${encodeURIComponent(query)}&limit=60`));
        } catch (err) {
          results.innerHTML = "";
          const failed = document.createElement("p");
          failed.className = "note";
          failed.textContent = err.message;
          results.appendChild(failed);
        }
      };
      box.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(run, 160);
      });
      box.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          clearTimeout(searchTimer);
          run();
        }
      });
      if (prefill) setTimeout(run, 0);
      return wrap;
    });
  }

  // ---------------------------------------------------- psychoacoustic net

  function polar(cx, cy, radius, angle) {
    return [cx + radius * Math.cos(angle), cy + radius * Math.sin(angle)];
  }

  function drawRadar(canvas, axes, base, target, hover) {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = canvas.clientWidth || 340;
    const height = canvas.clientHeight || 340;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const cx = width / 2;
    const cy = height / 2;
    const count = axes.length;
    // many axes leave less room for the labels around the ring
    const radius = Math.min(width, height) / 2 - (count > 10 ? 58 : 44);
    const step = (Math.PI * 2) / count;
    const angleAt = (index) => -Math.PI / 2 + index * step;

    // rings
    for (let ring = 1; ring <= 4; ring += 1) {
      ctx.beginPath();
      for (let i = 0; i < count; i += 1) {
        const [x, y] = polar(cx, cy, (radius * ring) / 4, angleAt(i));
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.strokeStyle = ring === 4 ? "rgba(255,255,255,.16)" : "rgba(255,255,255,.07)";
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    // spokes + labels
    ctx.font = `${count > 10 ? 10 : 10.5}px ui-sans-serif, system-ui, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    axes.forEach((axis, index) => {
      const angle = angleAt(index);
      const [x, y] = polar(cx, cy, radius, angle);
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(x, y);
      ctx.strokeStyle = "rgba(255,255,255,.07)";
      ctx.stroke();
      const [lx, ly] = polar(cx, cy, radius + 20, angle);
      ctx.fillStyle = hover === axis.key ? "#fbbf24" : "rgba(230,232,238,.72)";
      const label = axis.label.split(" ")[0];
      ctx.fillText(label, lx, ly);
    });

    const polygon = (values, stroke, fill, dashed) => {
      ctx.beginPath();
      axes.forEach((axis, index) => {
        const value = Math.max(0, Math.min(1, values[axis.key] ?? 0));
        const [x, y] = polar(cx, cy, radius * value, angleAt(index));
        if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.closePath();
      ctx.setLineDash(dashed ? [5, 4] : []);
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.setLineDash([]);
      axes.forEach((axis, index) => {
        const value = Math.max(0, Math.min(1, values[axis.key] ?? 0));
        const [x, y] = polar(cx, cy, radius * value, angleAt(index));
        ctx.beginPath();
        ctx.arc(x, y, 2.6, 0, Math.PI * 2);
        ctx.fillStyle = stroke;
        ctx.fill();
      });
    };

    polygon(base, "rgba(167,139,250,.95)", "rgba(167,139,250,.16)", false);
    if (target) polygon(target, "rgba(251,191,36,.95)", "rgba(251,191,36,.12)", true);
  }

  // -- the tag and artwork editor ----------------------------------------
  //
  // Every write goes through the server: tags via scripts/tag_fields.py,
  // artwork with the previous picture journalled first.  The editor never
  // talks to the filesystem, so a browser cannot name a path of its own.
  function openTags(rel = "") {
    const target = rel || (state.status && state.status.rel) || "";
    if (!target) {
      toast("Nothing is playing to edit.", "error");
      return;
    }
    openModal("Tags and artwork", (close) => {
      const wrap = document.createElement("div");
      wrap.className = "tagedit";
      let data = null;
      let inputs = {};
      let albumWide = false;

      const reload = async () => {
        try {
          data = await api(`/api/tags?rel=${encodeURIComponent(target)}`,
                           undefined, { timeout: 60 });
        } catch (err) {
          wrap.innerHTML = "";
          const failed = document.createElement("p");
          failed.className = "note";
          failed.textContent = err.message;
          wrap.appendChild(failed);
          return;
        }
        draw();
      };

      const sendTags = async (values, wide) => {
        const res = await api("/api/tags",
                              { rel: data.rel, values, album_wide: wide },
                              { timeout: 300 });
        if (res.ok) {
          toast(res.message || "Tags written.", "good");
          refreshMeta();
          if (data.tags && data.tags.rel) {
            data = data.tags;
            draw();
          } else {
            await reload();
          }
        } else {
          toast(res.message || res.error || "The tags were not written.",
                "error");
        }
        return res;
      };

      const sendArt = async (payload) => {
        const res = await api("/api/tags/artwork",
                              Object.assign({ rel: data.rel }, payload),
                              { timeout: 300 });
        if (res.ok) {
          toast(res.message || "Artwork written.", "good");
          if (res.art) {
            data.art = res.art;
            draw();
          } else {
            await reload();
          }
        } else {
          toast(res.message || res.error || "The artwork was not written.",
                "error");
        }
        return res;
      };

      const draw = () => {
        wrap.innerHTML = "";
        inputs = {};
        if (!data || data.error) {
          const note = document.createElement("p");
          note.className = "note";
          note.textContent = (data && data.error) || "That track was not found.";
          wrap.appendChild(note);
          return;
        }

        const head = document.createElement("div");
        head.className = "tagedit-head";
        const title = document.createElement("div");
        title.className = "tagedit-title";
        title.innerHTML = "<strong></strong><span></span>";
        title.querySelector("strong").textContent =
          data.values.title || data.rel;
        title.querySelector("span").textContent = [data.values.artist,
          data.album.name].filter(Boolean).join(" — ");
        head.appendChild(title);
        const where = document.createElement("div");
        where.className = "note tagedit-path";
        where.textContent = data.rel;
        head.appendChild(where);
        wrap.appendChild(head);

        if (!data.writable) {
          const blocked = document.createElement("p");
          blocked.className = "note";
          blocked.textContent = "This file cannot be tagged: "
            + (data.reason || "the format has no tag field") + ".";
          wrap.appendChild(blocked);
        } else {
          const batch = document.createElement("label");
          batch.className = "tagedit-batch";
          const tick = document.createElement("input");
          tick.type = "checkbox";
          tick.checked = albumWide;
          tick.onchange = () => {
            albumWide = tick.checked;
            draw();
          };
          batch.append(tick, document.createTextNode(
            ` Apply album, album artist, genre and date to all `
            + `${data.album.count} tracks of this album`));
          wrap.appendChild(batch);
        }

        const grid = document.createElement("div");
        grid.className = "tagedit-grid";
        (data.fields || []).forEach((field) => {
          const row = document.createElement("label");
          row.className = "tagedit-field";
          const name = document.createElement("span");
          name.textContent = field.label;
          const key = field.key;
          const box = document.createElement("input");
          box.type = "text";
          box.value = field.value == null ? "" : String(field.value);
          box.dataset.key = key;
          box.disabled = !data.writable
            || (albumWide && !field.album_field);
          if (albumWide && !field.album_field) {
            box.title = "Not an album-wide field — untick the album box "
              + "to edit it";
          }
          inputs[key] = box;
          row.append(name, box);
          grid.appendChild(row);
        });
        wrap.appendChild(grid);

        const actions = document.createElement("div");
        actions.className = "tagedit-actions";
        const save = document.createElement("button");
        save.type = "button";
        save.className = "btn primary";
        save.textContent = albumWide ? "Save to the album" : "Save tags";
        save.disabled = !data.writable;
        save.onclick = async () => {
          const values = {};
          Object.entries(inputs).forEach(([key, box]) => {
            values[key] = box.value;
          });
          save.disabled = true;
          try {
            await sendTags(values, albumWide);
          } catch (err) {
            toast(err.message, "error");
          } finally {
            save.disabled = false;
          }
        };
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "btn";
        cancel.textContent = "Close";
        cancel.onclick = close;
        actions.append(save, cancel);
        wrap.appendChild(actions);

        const hint = document.createElement("p");
        hint.className = "note";
        hint.textContent = "A tag write is recorded in field-writes.jsonl "
          + "first (byte-exact rollback for MP3), and the search index is "
          + "updated with it.";
        wrap.appendChild(hint);

        // -- artwork ------------------------------------------------------
        const art = data.art || {};
        const artBox = document.createElement("div");
        artBox.className = "tagedit-art";
        const artHead = document.createElement("div");
        artHead.className = "tagedit-art-title";
        artHead.textContent = "Cover art";
        artBox.appendChild(artHead);

        const preview = document.createElement("img");
        preview.className = "tagedit-cover";
        preview.alt = "Current cover artwork";
        if (art.preview) {
          preview.src = art.preview;
        } else {
          preview.classList.add("empty");
        }
        const facts = document.createElement("div");
        facts.className = "tagedit-art-facts";
        const bits = [];
        bits.push(art.embedded ? "embedded" : "not embedded");
        if (art.folder) bits.push(`folder: ${art.folder}`);
        if (art.mime) bits.push(art.mime);
        if (art.bytes) bits.push(`${formatNumber(art.bytes)} bytes`);
        facts.textContent = bits.join(" · ");
        artBox.append(preview, facts);

        if (!data.art_writable) {
          const blocked = document.createElement("p");
          blocked.className = "note";
          blocked.textContent = "This format cannot carry a picture.";
          artBox.appendChild(blocked);
        } else {
          const controls = document.createElement("div");
          controls.className = "tagedit-art-controls";
          const file = document.createElement("input");
          file.type = "file";
          file.accept = "image/jpeg,image/png";
          file.title = "A JPEG or PNG, at most "
            + `${formatNumber(data.art_limit || 6291456)} bytes`;
          const whereTo = document.createElement("select");
          [["embed", "inside the file"], ["folder", "cover file next to the album"]]
            .forEach(([value, label]) => {
              const option = document.createElement("option");
              option.value = value;
              option.textContent = label;
              whereTo.appendChild(option);
            });
          const upload = document.createElement("button");
          upload.type = "button";
          upload.className = "btn";
          upload.textContent = "Save picture";
          upload.onclick = () => {
            const chosen = file.files && file.files[0];
            if (!chosen) {
              toast("Choose an image first.", "error");
              return;
            }
            const reader = new FileReader();
            reader.onload = async () => {
              upload.disabled = true;
              try {
                await sendArt({ data: String(reader.result),
                                mime: chosen.type || "",
                                target: whereTo.value });
              } catch (err) {
                toast(err.message, "error");
              } finally {
                upload.disabled = false;
              }
            };
            reader.readAsDataURL(chosen);
          };
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "btn";
          remove.textContent = "Remove";
          let armed = false;
          remove.onclick = async () => {
            if (!armed) {
              armed = true;
              remove.textContent = "Really remove?";
              return;
            }
            remove.disabled = true;
            try {
              await sendArt({ target: whereTo.value, remove: true });
            } catch (err) {
              toast(err.message, "error");
            } finally {
              remove.disabled = false;
              armed = false;
              remove.textContent = "Remove";
            }
          };
          controls.append(file, whereTo, upload, remove);
          artBox.appendChild(controls);
          const hint = document.createElement("p");
          hint.className = "note";
          hint.textContent = "The previous picture is stored in "
            + "artwork-edit.jsonl before it is replaced.";
          artBox.appendChild(hint);
        }
        wrap.appendChild(artBox);
      };

      reload();
      return wrap;
    }, { wide: true });
  }

  function openPsycho(rel = "") {
    openModal("Sound profile", () => {
      const wrap = document.createElement("div");
      wrap.className = "psycho";

      // any song can be inspected, not just the one that happens to play
      const pick = document.createElement("div");
      pick.className = "psycho-pick";
      const find = input("find a song — title, artist, album or file name");
      find.type = "search";
      find.setAttribute("aria-label", "Find a song to show its properties");
      pick.appendChild(find);
      const found = document.createElement("div");
      found.className = "psycho-found";
      found.hidden = true;
      pick.appendChild(found);
      wrap.appendChild(pick);

      // the same measurements, seen through different lenses
      const setBar = document.createElement("div");
      setBar.className = "psycho-sets";
      wrap.appendChild(setBar);
      let setKey = (state.meta && state.meta.settings
        && state.meta.settings.psycho_set) || "psychology";
      const setList = () => {
        const offered = state.meta && state.meta.psycho
          && Array.isArray(state.meta.psycho.sets)
          ? state.meta.psycho.sets : [];
        return offered.length ? offered : [
          { key: "psychology", label: "Psychology", note: "", count: 0 },
          { key: "timbre", label: "Timbre", note: "", count: 0 },
          { key: "rhythm", label: "Rhythm", note: "", count: 0 },
          { key: "dynamics", label: "Dynamics", note: "", count: 0 },
          { key: "instrument", label: "Instruments", note: "", count: 0 },
        ];
      };

      const saveSet = async () => {
        try {
          await api("/api/settings", { psycho_set: setKey });
        } catch (err) {
          toast(err.message, "error");
        }
        if (state.meta) {
          state.meta.settings = state.meta.settings || {};
          state.meta.settings.psycho_set = setKey;
        }
      };

      const renderSets = () => {
        setBar.innerHTML = "";
        const offered = setList();
        if (offered.length < 2) return;
        offered.forEach((spec) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className =
            "chip psycho-set" + (spec.key === setKey ? " on" : "");
          button.textContent = spec.label;
          const bits = [];
          if (spec.note) bits.push(spec.note);
          if (spec.count) bits.push(`${formatNumber(spec.count)} tracks`);
          if (spec.estimated) bits.push("estimated");
          button.title = bits.join(" · ") || spec.label;
          button.setAttribute("aria-pressed", spec.key === setKey ? "true" : "false");
          button.onclick = () => {
            if (spec.key === setKey) return;
            setKey = spec.key;
            if (state.meta) {
              state.meta.psycho = state.meta.psycho || {};
              state.meta.psycho.set = setKey;
            }
            renderSets();
            saveSet();
            load(record ? record.rel : "");
          };
          setBar.appendChild(button);
        });
      };

      const head = document.createElement("div");
      head.className = "psycho-head";
      wrap.appendChild(head);
      const body = document.createElement("div");
      body.className = "psycho-body";

      const left = document.createElement("div");
      left.className = "psycho-left";
      const canvas = document.createElement("canvas");
      canvas.className = "psycho-radar";
      canvas.height = 320;
      canvas.setAttribute("aria-label", "Net diagram of the sound properties");
      left.appendChild(canvas);
      const key = document.createElement("div");
      key.className = "psycho-key";
      key.innerHTML =
        '<span class="k-base">current</span><span class="k-target">with your changes</span>';
      left.appendChild(key);
      const facts = document.createElement("div");
      facts.className = "psycho-facts";
      left.appendChild(facts);
      body.appendChild(left);

      const right = document.createElement("div");
      right.className = "psycho-right";
      body.appendChild(right);
      wrap.appendChild(body);

      const listWrap = document.createElement("div");
      listWrap.className = "psycho-matches";
      wrap.appendChild(listWrap);

      let record = null;
      let axes = [];
      let baseValues = {};
      let targetValues = {};
      let adjustments = {};
      let rawSpecs = [];
      let hover = "";
      let timer = null;

      const redraw = () => drawRadar(canvas, axes, baseValues, targetValues, hover);

      const renderHead = () => {
        head.innerHTML = "";
        facts.innerHTML = "";
        if (!record) {
          const note = document.createElement("p");
          note.className = "note";
          note.textContent = "Nothing is playing — search above for a song.";
          head.appendChild(note);
          return;
        }
        const title = document.createElement("div");
        title.className = "psycho-title";
        title.innerHTML = `<strong></strong><span></span>`;
        title.querySelector("strong").textContent = record.title || record.rel;
        title.querySelector("span").textContent =
          [record.artist, record.album].filter(Boolean).join(" — ");
        head.appendChild(title);
        const family = document.createElement("span");
        family.className = "psycho-family";
        family.textContent = record.family || "unfiled";
        head.appendChild(family);
        const active = setList().find((item) => item.key === setKey);
        if (active && setKey !== "psychology") {
          const lens = document.createElement("span");
          lens.className = "psycho-setname";
          lens.textContent = active.label;
          lens.title = active.note || active.label;
          head.appendChild(lens);
        }
        if (record.estimated) {
          const flag = document.createElement("span");
          flag.className = "psycho-est";
          flag.textContent = "partly estimated";
          flag.title = "Some columns still come from the older mood cache.";
          head.appendChild(flag);
          // the app can fill that in itself — no shell needed
          const fix = document.createElement("button");
          fix.className = "btn psycho-fix";
          fix.type = "button";
          fix.textContent = "Measure the library";
          fix.title = "Measure what is missing in the background";
          fix.onclick = async () => {
            fix.disabled = true;
            try {
              const data = await api("/api/psycho/update", {});
              if (data.error) toast(data.error, "error");
              else toast("Measuring the library in the background.", "good");
            } catch (err) {
              toast(err.message, "error");
            }
            fix.disabled = false;
          };
          head.appendChild(fix);
        }
        renderFacts();
      };

      const renderFacts = () => {
        facts.innerHTML = "";
        const measured = (record && record.measured) || {};
        const groups = [];
        rawSpecs.forEach((item) => {
          const value = measured[item.key];
          if (value === null || value === undefined) return;
          const name = item.group || "Measured";
          let group = groups.find((entry) => entry.name === name);
          if (!group) {
            group = { name, items: [] };
            groups.push(group);
          }
          const digits = item.digits === undefined ? 2 : item.digits;
          group.items.push({
            label: item.label,
            value: `${Number(value).toFixed(digits)}${item.unit ? ` ${item.unit}` : ""}`,
          });
        });
        if (!groups.length) return;
        const details = document.createElement("details");
        details.className = "psycho-facts-box";
        if (facts.dataset.open === "1") details.open = true;
        details.addEventListener("toggle", () => {
          facts.dataset.open = details.open ? "1" : "0";
        });
        const summary = document.createElement("summary");
        summary.textContent = `Measurements (${groups.reduce((n, g) => n + g.items.length, 0)})`;
        details.appendChild(summary);
        groups.forEach((group) => {
          const heading = document.createElement("h4");
          heading.textContent = group.name;
          details.appendChild(heading);
          const grid = document.createElement("dl");
          grid.className = "psycho-fact-grid";
          group.items.forEach((item) => {
            const dt = document.createElement("dt");
            dt.textContent = item.label;
            const dd = document.createElement("dd");
            dd.textContent = item.value;
            grid.append(dt, dd);
          });
          details.appendChild(grid);
        });
        facts.appendChild(details);
        facts.appendChild(whyBox(record));
      };

      // "Why these values" — the measurements behind an axis and their
      // declared weights, loaded once per track and lens from
      // /api/psycho/explain.  Read-only: the numbers come from the same
      // database the radar draws.
      const whyCache = new Map();

      const percent = (value) => `${Math.round(Math.max(0, Math.min(1, value)) * 100)}th`;

      const whyBox = (entry) => {
        const details = document.createElement("details");
        details.className = "psycho-facts-box";
        const summary = document.createElement("summary");
        summary.textContent = "Why these values";
        details.appendChild(summary);
        let loaded = false;
        const fill = async () => {
          if (!entry || !entry.rel) {
            const note = document.createElement("p");
            note.className = "note";
            note.textContent = "No track is loaded.";
            details.appendChild(note);
            return;
          }
          const key = `${entry.rel}|${setKey}`;
          let data = whyCache.get(key);
          if (!data) {
            try {
              data = await api("/api/psycho/explain?rel="
                + encodeURIComponent(entry.rel)
                + `&set=${encodeURIComponent(setKey)}`, undefined,
                { timeout: 90 });
            } catch (err) {
              const note = document.createElement("p");
              note.className = "note";
              note.textContent = err.message;
              details.appendChild(note);
              return;
            }
            whyCache.set(key, data);
          }
          if (data.error) {
            const note = document.createElement("p");
            note.className = "note";
            note.textContent = data.error;
            details.appendChild(note);
            return;
          }
          (data.axes || []).forEach((axis) => {
            if (axis.value === null || axis.value === undefined) return;
            const heading = document.createElement("h4");
            heading.textContent = `${axis.label} ${axis.value.toFixed(2)}`
              + ` · ${percent(axis.value)} percentile`
              + (axis.estimated ? " · estimated" : "");
            details.appendChild(heading);
            const grid = document.createElement("dl");
            grid.className = "psycho-fact-grid";
            (axis.parts || []).forEach((part) => {
              const dt = document.createElement("dt");
              const share = Math.round((part.weight || 0) * 100);
              dt.textContent = `${part.label} · ${share} %`;
              dt.title = part.source ? `measured as ${part.source}` : "";
              const dd = document.createElement("dd");
              dd.textContent = part.value.toFixed(2);
              grid.append(dt, dd);
            });
            details.appendChild(grid);
            const weak = (axis.parts || []).filter((part) => part.value < 0.5);
            if (weak.length) {
              const note = document.createElement("p");
              note.className = "note";
              note.textContent = "Below average: " + weak
                .slice(0, 3).map((part) => `${part.label} ${part.value.toFixed(2)}`)
                .join(", ");
              details.appendChild(note);
            }
            (axis.missing || []).forEach((part) => {
              const note = document.createElement("p");
              note.className = "note";
              note.textContent = `Not used: ${part.label} — ${part.note}`;
              details.appendChild(note);
            });
          });
        };
        details.addEventListener("toggle", () => {
          if (!details.open || loaded) return;
          loaded = true;
          fill();
        });
        return details;
      };

      const renderSliders = () => {
        right.innerHTML = "";
        const intro = document.createElement("p");
        intro.className = "note";
        intro.textContent =
          "Move a dimension to look for music that fits the changed profile.";
        right.appendChild(intro);
        axes.forEach((axis) => {
          const row = document.createElement("div");
          row.className = "psycho-axis";
          const label = document.createElement("span");
          label.className = "psycho-axis-label";
          label.textContent = axis.label;
          const input = document.createElement("input");
          input.type = "range";
          input.min = "0";
          input.max = "1";
          input.step = "0.01";
          input.value = String(baseValues[axis.key] ?? 0.5);
          const value = document.createElement("span");
          value.className = "psycho-axis-value";
          value.textContent = Number(input.value).toFixed(2);
          input.addEventListener("pointerenter", () => {
            hover = axis.key;
            redraw();
          });
          input.addEventListener("pointerleave", () => {
            hover = "";
            redraw();
          });
          input.addEventListener("input", () => {
            const now = Number(input.value);
            value.textContent = now.toFixed(2);
            targetValues[axis.key] = now;
            const delta = now - (baseValues[axis.key] ?? 0.5);
            if (Math.abs(delta) >= 0.02) adjustments[axis.key] = delta;
            else delete adjustments[axis.key];
            redraw();
            clearTimeout(timer);
            timer = setTimeout(runSimilar, 220);
          });
          row.append(label, input, value);
          right.appendChild(row);
        });
        const actions = document.createElement("div");
        actions.className = "modal-actions psycho-actions";
        const reset = document.createElement("button");
        reset.type = "button";
        reset.className = "btn";
        reset.textContent = "Reset";
        reset.onclick = () => {
          targetValues = { ...baseValues };
          adjustments = {};
          renderSliders();
          redraw();
          runSimilar();
        };
        const ask = document.createElement("button");
        ask.type = "button";
        ask.className = "btn";
        ask.textContent = "Ask in chat";
        ask.onclick = () => {
          closeModal();
          const moved = Object.entries(adjustments)
            .map(([k, v]) => `${k} ${v > 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}`)
            .join(", ");
          els.prompt.value = moved
            ? `find music similar to ${record.title} but ${Object.keys(adjustments).join(" and ")} ${Object.entries(adjustments).map(([k, v]) => v > 0 ? `more ${k}` : `less ${k}`).join(", ")}`
            : `show the properties of ${record.title}`;
          autoGrow();
          els.prompt.focus();
        };
        actions.append(reset, ask);
        right.appendChild(actions);
      };

      const renderMatches = (data) => {
        listWrap.innerHTML = "";
        const head2 = document.createElement("div");
        head2.className = "psycho-matches-head";
        const changed = Object.keys(adjustments);
        head2.textContent = changed.length
          ? `Fits this ${data.count} track${data.count === 1 ? "" : "s"} · changed: ${changed.join(", ")}`
          : `Closest ${data.count} track${data.count === 1 ? "" : "s"} to this profile`;
        listWrap.appendChild(head2);
        if (!data.hits.length) {
          const note = document.createElement("p");
          note.className = "note";
          note.textContent = "No other track has a profile yet.";
          listWrap.appendChild(note);
          return;
        }
        data.hits.forEach((hit, index) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "search-hit psycho-hit";
          const main = document.createElement("span");
          main.className = "what";
          main.textContent = `${index + 1}. ${hit.title}`;
          const who = document.createElement("span");
          who.className = "who";
          who.textContent = [hit.artist, hit.album].filter(Boolean).join(" — ");
          const where = document.createElement("span");
          where.className = "where";
          where.textContent = [hit.duration, `distance ${hit.distance.toFixed(3)}`]
            .filter(Boolean).join(" · ");
          button.append(main, who, where);
          button.onclick = () => {
            play(hit.rel);
            toast(`Playing ${hit.title}.`);
            closeModal();
          };
          const line = document.createElement("div");
          line.className = "search-item";
          const inspect = document.createElement("button");
          inspect.type = "button";
          inspect.className = "icon-btn tiny inspect";
          inspect.title = "Show this track's sound properties";
          inspect.innerHTML = '<svg><use href="#i-brain"/></svg>';
          inspect.onclick = () => load(hit.rel);
          const edit = document.createElement("button");
          edit.type = "button";
          edit.className = "icon-btn tiny inspect";
          edit.title = "Edit this track's tags and artwork";
          edit.innerHTML = '<svg><use href="#i-pencil"/></svg>';
          edit.onclick = () => openTags(hit.rel);
          line.append(button, inspect, edit);
          listWrap.appendChild(line);
        });
      };

      const runSimilar = async () => {
        const payload = {
          rel: record ? record.rel : "",
          adjustments,
          limit: 10,
          set: setKey,
        };
        try {
          renderMatches(await api("/api/psycho/similar", payload));
        } catch (err) {
          listWrap.innerHTML = "";
          const note = document.createElement("p");
          note.className = "note";
          note.textContent = err.message;
          listWrap.appendChild(note);
        }
      };

      const load = async (which) => {
        record = null;
        axes = [];
        baseValues = {};
        targetValues = {};
        adjustments = {};
        hover = "";
        listWrap.innerHTML = "";
        right.innerHTML = "";
        renderHead();
        redraw();
        let data;
        try {
          data = await api(`/api/psycho?set=${encodeURIComponent(setKey)}`
            + (which ? `&rel=${encodeURIComponent(which)}` : ""));
        } catch (err) {
          const note = document.createElement("p");
          note.className = "note";
          note.textContent = err.message;
          listWrap.appendChild(note);
          return;
        }
        record = data && data.rel ? data : null;
        if (data && data.set && data.set !== setKey) setKey = data.set;
        if (data && Array.isArray(data.sets) && data.sets.length
          && state.meta) {
          // the answer carries the authoritative set list (labels, counts)
          state.meta.psycho = state.meta.psycho || {};
          state.meta.psycho.sets = data.sets;
        }
        renderSets();
        axes = data.axes || [];
        rawSpecs = data.raw || [];
        baseValues = { ...(data.values || {}) };
        targetValues = { ...baseValues };
        axes.forEach((axis) => {
          if (baseValues[axis.key] === undefined) baseValues[axis.key] = 0.5;
          if (targetValues[axis.key] === undefined) targetValues[axis.key] = 0.5;
        });
        renderHead();
        renderSliders();
        redraw();
        if (!record || data.error) {
          const note = document.createElement("p");
          note.className = "note";
          note.textContent = data.error;
          listWrap.appendChild(note);
          return;
        }
        runSimilar();
      };

      let findTimer = null;
      const findTracks = async () => {
        const query = find.value.trim();
        if (!query) {
          found.hidden = true;
          found.innerHTML = "";
          return;
        }
        let data;
        try {
          data = await api(`/api/search?q=${encodeURIComponent(query)}&limit=14`);
        } catch (_) {
          return;
        }
        found.innerHTML = "";
        if (!data.hits.length) {
          const note = document.createElement("p");
          note.className = "note";
          note.textContent = `Nothing matches “${query}”.`;
          found.appendChild(note);
        }
        data.hits.forEach((hit) => {
          const row = document.createElement("button");
          row.type = "button";
          row.className = "search-hit";
          const main = document.createElement("span");
          main.className = "what";
          main.textContent = hit.title || hit.rel;
          const who = document.createElement("span");
          who.className = "who";
          who.textContent = [hit.artist, hit.album].filter(Boolean).join(" — ");
          row.append(main, who);
          row.onclick = () => {
            found.hidden = true;
            found.innerHTML = "";
            find.value = "";
            load(hit.rel);
          };
          found.appendChild(row);
        });
        found.hidden = false;
      };
      find.addEventListener("input", () => {
        clearTimeout(findTimer);
        findTimer = setTimeout(findTracks, 180);
      });
      find.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          found.hidden = true;
          return;
        }
        if (event.key === "Enter") {
          event.preventDefault();
          clearTimeout(findTimer);
          findTracks();
        }
      });

      load(rel);

      window.addEventListener("resize", redraw, { once: true });
      return wrap;
    }, { wide: true });
  }

  // ---------------------------------------------------------------- lyrics
  //
  // The lines come from GET /api/lyrics, which only reads the local database;
  // the panel appears when it is switched on and the highlight follows
  // status.position, so the line being sung is the marked one.  Transcribing
  // runs on this machine as a background job, so the panel polls its progress.

  function lyricsVisible() {
    return Boolean(state.lyricsOn && els.lyricsPanel && !els.lyricsPanel.hidden);
  }

  function applyLyrics(on) {
    state.lyricsOn = Boolean(on);
    if (els.lyricsPanel) els.lyricsPanel.hidden = !state.lyricsOn;
    if (els.lyricsToggle) {
      els.lyricsToggle.classList.toggle("on", state.lyricsOn);
      els.lyricsToggle.title = state.lyricsOn
        ? "Hide the lyrics"
        : "Show the lyrics of the current song, line by line";
    }
    if (!state.lyricsOn) {
      stopLyricClock();
      return;
    }
    startLyricClock();
    const rel = state.status && state.status.rel ? state.status.rel : "";
    loadLyrics(rel);
  }

  // the playhead the server last reported, moved on locally between polls so a
  // line changes at its real moment instead of up to two seconds late
  function lyricPosition() {
    const status = state.status;
    if (!status) return 0;
    let position = Number(status.position) || 0;
    if (status.playing && !status.paused && state.statusAt) {
      position += (performance.now() - state.statusAt) / 1000;
      const duration = Number(status.duration) || 0;
      if (duration) position = Math.min(position, duration);
    }
    return Math.max(0, position);
  }

  // the status poll is every two seconds; the highlight is redrawn four times
  // a second from the interpolated playhead, which costs nothing while the
  // panel is hidden
  function startLyricClock() {
    if (state.lyricsTimer) return;
    state.lyricsTimer = setInterval(() => {
      const status = state.status;
      if (!lyricsVisible() || !status || !status.playing) return;
      highlightLyric();
    }, 250);
  }

  function stopLyricClock() {
    if (state.lyricsTimer) clearInterval(state.lyricsTimer);
    state.lyricsTimer = null;
  }

  async function toggleLyrics() {
    applyLyrics(!state.lyricsOn);
    try {
      await api("/api/settings", { lyrics_panel: state.lyricsOn });
    } catch (err) {
      toast(err.message, "error");
    }
  }

  const lyricClock = (seconds) => {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  };

  function lyricsNote(text) {
    if (!els.lyricsLines) return;
    els.lyricsLines.innerHTML = "";
    const note = document.createElement("p");
    note.className = "lyrics-note";
    note.textContent = text;
    els.lyricsLines.appendChild(note);
  }

  function lyricsMeta(text) {
    if (els.lyricsMeta) els.lyricsMeta.textContent = text || "";
  }

  // A transcription guesses where a line begins; the listener can nudge it.
  // The shift is stored per song on the server, so the panel, the chat and an
  // exported .lrc all agree.
  async function shiftLyrics(delta) {
    const rel = state.lyricsLoadedRel
      || (state.status && state.status.rel ? state.status.rel : "");
    if (!rel) {
      toast("Nothing is playing — the shift belongs to one song.", "error");
      return;
    }
    try {
      const answer = await api("/api/lyrics",
                               { action: "offset", rel, value: delta,
                                 relative: true });
      if (answer.error) throw new Error(answer.error);
      state.lyricsLoadedRel = "";        // reload with the new timing
      await loadLyrics(rel);
      const value = Number(answer.offset) || 0;
      toast(value
        ? `Lyrics shifted ${value > 0 ? "+" : ""}${value.toFixed(1)} s`
        : "Lyrics timing reset");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function exportLyrics() {
    const rel = state.lyricsLoadedRel
      || (state.status && state.status.rel ? state.status.rel : "");
    if (!rel) {
      toast("Nothing is playing — export the lyrics of a song.", "error");
      return;
    }
    try {
      const answer = await api("/api/lyrics", { action: "export", rel });
      if (answer.error) {
        // an existing .lrc is not overwritten silently: a second click says so
        if (answer.exists) {
          const again = await api("/api/lyrics",
                                  { action: "export", rel, overwrite: true });
          if (again.error) throw new Error(again.error);
          toast(`Replaced ${again.name || "the .lrc file"} · ${again.lines} lines`);
          return;
        }
        throw new Error(answer.error);
      }
      toast(`Saved ${answer.name || "the .lrc file"} · ${answer.lines} lines`);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function loadLyrics(rel) {
    if (!lyricsVisible()) return;
    rel = rel || "";
    if (rel && rel === state.lyricsLoadedRel && state.lyricsLines.length) {
      highlightLyric();
      return;
    }
    // the song is set as "asked for" at once, and as "loaded" only once an
    // answer arrived — an empty answer is not asked for again
    state.lyricsRel = rel;
    if (rel !== state.lyricsLoadedRel) {
      state.lyricsLoadedRel = "";
      state.lyricsLines = [];
      state.lyricsIndex = -1;
    }
    if (!rel) {
      state.lyricsLoadedRel = "";
      lyricsMeta("");
      lyricsNote("Nothing is playing — start a song and its lyrics follow it.");
      return;
    }
    lyricsMeta("");
    lyricsNote("loading …");
    let data;
    try {
      data = await api(`/api/lyrics?rel=${encodeURIComponent(rel)}`);
    } catch (err) {
      lyricsNote(err.message);
      return;
    }
    if (state.lyricsRel !== rel) return;      // the track changed meanwhile
    state.lyricsLoadedRel = rel;
    renderLyrics(data || {});
  }

  function renderLyrics(data) {
    if (!els.lyricsLines) return;
    const lines = Array.isArray(data.lines) ? data.lines : [];
    state.lyricsLines = lines;
    state.lyricsIndex = -1;
    els.lyricsLines.innerHTML = "";
    const meta = [];
    if (data.language) meta.push(data.language);
    if (data.model) meta.push(data.model);
    if (lines.length) meta.push(`${lines.length} lines`);
    const shift = Number(data.offset) || 0;
    if (shift) meta.push(`${shift > 0 ? "+" : ""}${shift.toFixed(1)} s`);
    lyricsMeta(meta.join(" · "));
    if (!lines.length) {
      if (data.available === false && data.error) {
        lyricsNote(data.error);
      } else {
        lyricsNote(data.title
          ? `No lyrics for “${data.title}” yet.  The sparkle button `
            + "transcribes it on this machine."
          : "No lyrics for this song yet.");
      }
      return;
    }
    const fragment = document.createDocumentFragment();
    lines.forEach((line, index) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "lyrics-line";
      row.dataset.index = String(index);
      row.dataset.start = String(line.start || 0);
      const when = document.createElement("span");
      when.className = "lyrics-time";
      when.textContent = lyricClock(line.start || 0);
      const text = document.createElement("span");
      text.className = "lyrics-text";
      text.textContent = line.text || "";
      row.appendChild(when);
      row.appendChild(text);
      row.title = "Jump to this line";
      row.addEventListener("click", () => {
        const status = state.status;
        if (!status || !status.playing) return;
        const delta = Math.round((line.start || 0) - (status.position || 0));
        transport("seek", delta);
      });
      fragment.appendChild(row);
    });
    els.lyricsLines.appendChild(fragment);
    if (data.current === false) {
      const stale = document.createElement("p");
      stale.className = "lyrics-note";
      stale.textContent = "The file changed since it was transcribed — "
        + "transcribe it again for the current audio.";
      els.lyricsLines.appendChild(stale);
    }
    // the first render jumps straight to the line being sung; following it
    // afterwards glides
    highlightLyric(true);
  }

  function highlightLyric(force = false) {
    if (!lyricsVisible() || !els.lyricsLines) return;
    const lines = state.lyricsLines;
    if (!lines.length) return;
    const at = lyricPosition();
    // the line that has started and not yet ended is the one being sung; a
    // short lead makes the change land on the beat rather than behind it, and
    // a gap between two lines keeps the line just sung lit (like a lyric sheet)
    let index = -1;
    for (let i = 0; i < lines.length; i += 1) {
      const start = Number(lines[i].start) || 0;
      if (start <= at + 0.12) index = i;
      else break;
    }
    if (index === state.lyricsIndex && !force) return;
    state.lyricsIndex = index;
    const rows = els.lyricsLines.querySelectorAll(".lyrics-line");
    rows.forEach((row, i) => {
      row.classList.toggle("on", i === index);
      row.classList.toggle("past", i < index);
    });
    if (index < 0 || !rows[index]) return;
    // scroll against the container's own box, so a scrollbar, a heading or a
    // positioned ancestor can never move the target line off by an offset
    const row = rows[index];
    const box = els.lyricsLines;
    const boxRect = box.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    const target = box.scrollTop + (rowRect.top - boxRect.top)
      - (box.clientHeight - rowRect.height) / 2;
    const top = Math.max(0, target);
    if (Math.abs(box.scrollTop - top) < 2) return;
    box.scrollTo({ top, behavior: force ? "auto" : "smooth" });
  }

  async function transcribeLyrics(all = false) {
    const rel = state.status && state.status.rel ? state.status.rel : "";
    if (!all && !rel) {
      toast("Play a song first — or use the + button to transcribe the next "
        + "25 songs without lyrics, or the lyrics chip in the footer for "
        + "the whole library.", "error");
      return;
    }
    const body = all ? { missing: 25 } : { rel };
    let data;
    try {
      data = await api("/api/lyrics/transcribe", body, { timeout: 30 });
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    if (data.error) {
      toast(data.error, "error");
      return;
    }
    lyricsNote(all
      ? "Transcribing the next 25 songs without lyrics · this machine only …"
      : "Transcribing this song · this machine only …");
    pollLyricsJob(all);
  }

  async function pollLyricsJob(batch = false) {
    if (state.lyricsPolling) return;
    state.lyricsPolling = setInterval(async () => {
      let data;
      try {
        data = await api("/api/lyrics/update", undefined, { retries: 0, timeout: 15 });
      } catch (err) {
        return;
      }
      const status = (data && data.status) || {};
      const counts = (data && data.counts) || {};
      if (status.running) {
        lyricsNote(`${status.step || "transcribing"} · `
          + `${Number(status.percent || 0).toFixed(0)} %`);
        return;
      }
      clearInterval(state.lyricsPolling);
      state.lyricsPolling = null;
      if (state.meta && state.meta.lyrics) {
        state.meta.lyrics.tracks = counts.tracks || state.meta.lyrics.tracks;
        state.meta.lyrics.lines = counts.lines || state.meta.lyrics.lines;
      }
      if (status.state === "failed") {
        lyricsNote("The transcription stopped — see Settings → Lyrics for the log.");
        return;
      }
      const rel = state.status && state.status.rel ? state.status.rel : "";
      state.lyricsRel = "";                 // force a reload
      loadLyrics(rel);
      if (batch) toast("Lyrics updated", "ok");
    }, 2500);
  }

  // ----------------------------------------------------------- queue (up next)
  //
  // The player keeps the real queue, so this panel is a window on it: the
  // current entry and what follows.  A click jumps inside the queue, the ✕
  // removes one entry — both are mpv commands, so no file is touched and the
  // server journals nothing but the session it remembers.

  function queueVisible() {
    return Boolean(state.queueOn && els.queuePanel && !els.queuePanel.hidden);
  }

  function applyQueue(on) {
    state.queueOn = Boolean(on);
    if (els.queuePanel) els.queuePanel.hidden = !state.queueOn;
    if (els.queueToggle) {
      els.queueToggle.classList.toggle("on", state.queueOn);
      els.queueToggle.title = state.queueOn
        ? "Hide what plays next"
        : "Show what plays next: the queue with jump and remove";
    }
    if (!state.queueOn) return;
    state.queueKey = "";
    refreshQueue();
  }

  async function refreshQueue() {
    if (!queueVisible() || state.queueBusy) return;
    state.queueBusy = true;
    let data;
    try {
      data = await api("/api/queue?limit=200", undefined,
                       { retries: 0, timeout: 15 });
    } catch (err) {
      state.queueBusy = false;
      return;
    }
    state.queueBusy = false;
    renderQueue(data || {});
  }

  function renderQueue(data) {
    if (!els.queueList) return;
    const entries = Array.isArray(data.entries) ? data.entries : [];
    const count = Number(data.count || entries.length || 0);
    const at = Number(data.position || 0);
    els.queueList.innerHTML = "";
    state.queueKey = `${Number(data.count || 0)}:${Math.max(0, at - 1)}`;
    renderOwn(data.own);
    if (els.queueMeta) {
      const flags = [];
      if (data.shuffle) flags.push("shuffle");
      if (data.endless) flags.push(data.endless_repeat ? "endless · repeating" : "endless");
      els.queueMeta.textContent = entries.length
        ? `${at} / ${count}${flags.length ? " · " + flags.join(" · ") : ""}`
        : "";
    }
    if (!entries.length) {
      const empty = document.createElement("li");
      empty.className = "queue-empty";
      empty.textContent = data.playing
        ? "The queue holds only the current track."
        : "Nothing is queued — play something and its queue shows up here.";
      els.queueList.appendChild(empty);
      return;
    }
    entries.forEach((entry) => {
      const row = document.createElement("li");
      row.className = "queue-row";
      if (entry.index === at) row.classList.add("current");
      const number = document.createElement("span");
      number.className = "queue-number";
      number.textContent = String(entry.index);
      const play = document.createElement("button");
      play.type = "button";
      play.className = "queue-name";
      play.title = entry.rel ? `Play ${entry.rel}` : "Play this entry";
      const title = document.createElement("span");
      title.className = "queue-title";
      title.textContent = entry.title || entry.rel || "?";
      play.appendChild(title);
      if (entry.artist) {
        const artist = document.createElement("span");
        artist.className = "queue-artist";
        artist.textContent = entry.artist;
        play.appendChild(artist);
      }
      play.addEventListener("click", () => queueAction("play", entry.index));
      const time = document.createElement("span");
      time.className = "queue-duration";
      time.textContent = entry.duration || "";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "icon-btn tiny queue-remove";
      remove.title = "Remove this entry from the queue";
      remove.innerHTML = icon("trash");
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        queueAction("remove", entry.index);
      });
      row.append(number, play, time, remove);
      els.queueList.appendChild(row);
    });
    if (Number(data.more || 0) > 0) {
      const more = document.createElement("li");
      more.className = "queue-empty";
      more.textContent = `${formatNumber(data.more)} more after these.`;
      els.queueList.appendChild(more);
    }
  }

  // --------------------------------------------------- ad-hoc (own) queue

  function ownDefaultName(own) {
    const text = String((own && (own.label || own.name)) || "Mix").trim();
    return text.replace(/_\d{8}-\d{6}$/, "").slice(0, 60) || "Mix";
  }

  function renderOwn(own) {
    const bar = els.queueOwn;
    if (!bar) return;
    state.own = own && own.rel ? own : null;
    bar.hidden = !state.own;
    if (els.queueOwnForm) els.queueOwnForm.hidden = true;
    if (!state.own) return;
    if (els.queueOwnName) {
      const label = state.own.label || state.own.name || "Ad-hoc list";
      els.queueOwnName.textContent =
        `Ad-hoc list “${label}” · ${state.own.count} tracks — not kept yet`;
      els.queueOwnName.title = state.own.rel;
    }
    if (els.queueOwnDiscard) {
      els.queueOwnDiscard.textContent = "Remove";
      els.queueOwnDiscard.dataset.armed = "";
    }
  }

  function ownNamePrompt() {
    if (!state.own || !els.queueOwnForm) return;
    const shown = !els.queueOwnForm.hidden;
    els.queueOwnForm.hidden = shown;
    if (!shown) {
      if (els.queueOwnInput) {
        els.queueOwnInput.value = ownDefaultName(state.own);
        els.queueOwnInput.focus();
        els.queueOwnInput.select();
      }
    }
  }

  async function ownAction(action, name) {
    let data;
    try {
      data = await api("/api/queue", { action, name: name || "" },
                       { timeout: 30 });
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    if (data.error) {
      toast(data.text || "That did not work.", "error");
      return;
    }
    toast(action === "save" ? "Playlist saved"
          : "Ad-hoc list removed", "ok");
    state.queueKey = "";
    if (data.queue) renderQueue(data.queue);
    else refreshQueue();
    refreshMeta();
  }

  function wireOwnBar() {
    if (els.queueOwnSave) {
      els.queueOwnSave.addEventListener("click", ownNamePrompt);
    }
    if (els.queueOwnForm) {
      els.queueOwnForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const name = els.queueOwnInput ? els.queueOwnInput.value.trim() : "";
        ownAction("save", name);
      });
    }
    if (els.queueOwnDiscard) {
      els.queueOwnDiscard.addEventListener("click", () => {
        // two clicks: the first arms, the second removes — no dialog needed
        if (els.queueOwnDiscard.dataset.armed === "1") {
          ownAction("discard");
          return;
        }
        els.queueOwnDiscard.dataset.armed = "1";
        els.queueOwnDiscard.textContent = "Remove?";
        setTimeout(() => {
          if (els.queueOwnDiscard
              && els.queueOwnDiscard.dataset.armed === "1") {
            els.queueOwnDiscard.dataset.armed = "";
            els.queueOwnDiscard.textContent = "Remove";
          }
        }, 4000);
      });
    }
  }

  async function queueAction(action, index) {
    let data;
    try {
      data = await api("/api/queue", { action, index }, { timeout: 20 });
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    if (data.error) {
      toast(data.text || "That did not work.", "error");
      return;
    }
    if (action === "play") toast(`Jumped to entry ${index}`, "ok");
    // the queue changed under the playhead, so skip the "same queue" check
    state.queueKey = "";
    refreshQueue();
  }

  // --------------------------------------------------- continue last session

  function applyResume(info) {
    const resume = info || {};
    const available = Boolean(resume.available);
    state.resume = resume;
    if (!els.resumeRow) return;
    // only while idle: a running song is already "continuing"
    const idle = !(state.status && state.status.playing);
    els.resumeRow.hidden = !(available && idle);
    if (els.resumeWhat) els.resumeWhat.textContent = resume.label || "";
  }

  async function resumeSession() {
    let data;
    try {
      data = await api("/api/resume", {}, { timeout: 30 });
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    if (data.error) {
      toast(data.text || "Nothing to continue.", "error");
      return;
    }
    toast("Continuing where you left off", "ok");
    if (els.resumeRow) els.resumeRow.hidden = true;
    pollStatus();
    if (queueVisible()) {
      state.queueKey = "";
      refreshQueue();
    }
  }

  async function forgetSession() {
    try {
      await api("/api/queue", { action: "forget" }, { timeout: 15 });
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    if (els.resumeRow) els.resumeRow.hidden = true;
    refreshMeta();
    toast("Forgot where I stopped", "ok");
  }

  // ------------------------------------------------------------- settings

  function section(title, hint) {
    const box = document.createElement("section");
    box.className = "settings-section";
    const head = document.createElement("div");
    head.className = "settings-head";
    const name = document.createElement("h3");
    name.textContent = title;
    head.appendChild(name);
    if (hint) {
      const note = document.createElement("span");
      note.className = "note";
      note.textContent = hint;
      head.appendChild(note);
    }
    box.appendChild(head);
    return box;
  }

  function bar(percent, label) {
    const wrap = document.createElement("div");
    wrap.className = "settings-bar";
    const track = document.createElement("div");
    track.className = "settings-bar-track";
    const fill = document.createElement("div");
    fill.className = "settings-bar-fill";
    fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    track.appendChild(fill);
    const text = document.createElement("span");
    text.className = "note";
    text.textContent = label;
    wrap.append(track, text);
    return wrap;
  }

  async function openSettings() {
    // Every control comes from `GET /api/settings`, i.e. from the registry in
    // `lib/chat/settings.py`: a new setting needs no JavaScript here.  The
    // blocks below a group add actions that are not values (measuring the
    // library, listing the model server's models, diagnostics).
    let data;
    try {
      data = await api("/api/settings", undefined, { timeout: 90, retries: 1 });
    } catch (err) {
      toast(`Could not read the settings: ${err.message}`, "error");
      return;
    }
    openModal("Settings", () => {
      const wrap = document.createElement("div");
      wrap.className = "settings";
      const controls = [];
      const byKey = new Map();
      // one tab per category (the group of a registry row); the actions that
      // are not values (the index job, diagnostics) live in their own tab or
      // beside the values they belong to
      const sections = new Map();
      const tabs = new Map();
      const order = [];
      let useLibrary = null;
      let indexCounts = null, indexProgress = null, indexBtn = null;
      let indexPauseBtn = null, indexCancelBtn = null;
      let indexLog = null;
      let indexWasRunning = false;
      let indexData = null;
      let backupCounts = null, backupProgress = null, backupBtn = null;
      let backupPauseBtn = null, backupLog = null;
      let backupWasRunning = false;
      let backupData = null;
      let importFile = null, importCheck = null, importBtn = null;
      let importOverwrite = null, importNote = null;
      const rows = Array.isArray(data.rows) ? data.rows : [];

      const groupHint = {
        Library: "The app works on this folder and everything below it.",
        Chat: "How a request is planned and how much it asks for.",
        Playback: "The now-playing strip and what happens when a track is chosen.",
        "Sound profile": "Measured from the audio, then turned into axes and families.",
        Lyrics: "The timed lines under the now-playing strip.",
        Voice: "Local speech input — the clip is transcribed on this machine.",
        "Language model": "Optional: an OpenAI-compatible server plans the harder sentences.",
      };

      const tabBar = document.createElement("div");
      tabBar.className = "settings-tabs";
      tabBar.setAttribute("role", "tablist");
      tabBar.setAttribute("aria-label", "Settings categories");
      const tabBody = document.createElement("div");
      tabBody.className = "settings-tabbody";
      wrap.append(tabBar, tabBody);

      const showTab = (name) => {
        let visible = null;
        tabs.forEach((entry, key) => {
          const on = key === name;
          entry.tab.classList.toggle("on", on);
          entry.tab.setAttribute("aria-selected", on ? "true" : "false");
          entry.tab.tabIndex = on ? 0 : -1;
          entry.panel.hidden = !on;
          if (on) visible = entry;
        });
        if (!visible) return;
        state.settingsTab = name;
        els.modalBody.scrollTop = 0;
      };

      // the panel of one category, created on first use so the order follows
      // the registry (`Library` … `Language model`) and any extra tab lands
      // where it was asked for
      const tabPanel = (name, hint) => {
        if (tabs.has(name)) return tabs.get(name).panel;
        const index = order.length;
        const id = `settings-tab-${index}`;
        const tab = document.createElement("button");
        tab.type = "button";
        tab.className = "settings-tab";
        tab.id = `${id}-btn`;
        tab.setAttribute("role", "tab");
        tab.setAttribute("aria-controls", id);
        tab.setAttribute("aria-selected", "false");
        tab.tabIndex = -1;
        tab.textContent = name;
        if (hint) tab.title = hint;
        const panel = document.createElement("div");
        panel.className = "settings-panel";
        panel.id = id;
        panel.setAttribute("role", "tabpanel");
        panel.setAttribute("aria-labelledby", tab.id);
        panel.hidden = true;
        if (hint) {
          const note = document.createElement("p");
          note.className = "note settings-hint";
          note.textContent = hint;
          panel.appendChild(note);
        }
        tabBar.appendChild(tab);
        tabBody.appendChild(panel);
        tab.onclick = () => showTab(name);
        tabs.set(name, { tab, panel });
        order.push(name);
        return panel;
      };

      // ←/→ (and Home/End) walk the tabs, as a real tab list does
      tabBar.addEventListener("keydown", (event) => {
        const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
        if (!keys.includes(event.key)) return;
        const index = order.indexOf(state.settingsTab || "");
        let next = index;
        if (event.key === "ArrowLeft") next = (index - 1 + order.length) % order.length;
        else if (event.key === "ArrowRight") next = (index + 1) % order.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = order.length - 1;
        const name = order[next];
        if (!name) return;
        event.preventDefault();
        showTab(name);
        tabs.get(name).tab.focus();
      });

      // -- one control per registry row -------------------------------------
      const settingField = (row) => {
        const box = document.createElement("div");
        box.className = "settings-field";
        let control = null;
        let read = null;

        if (row.kind === "bool") {
          box.classList.add("settings-toggle");
          const line = document.createElement("label");
          line.className = "settings-check";
          control = document.createElement("input");
          control.type = "checkbox";
          control.checked = Boolean(row.value);
          control.dataset.key = row.key;
          const text = document.createElement("span");
          text.textContent = row.label;
          line.append(control, text);
          box.appendChild(line);
          read = () => Boolean(control.checked);
        } else {
          const line = document.createElement("div");
          line.className = "settings-row";
          const label = document.createElement("label");
          label.className = "settings-label";
          label.textContent = row.label;
          line.appendChild(label);

          if (row.kind === "choice") {
            control = document.createElement("select");
            (row.choices || []).forEach((choice) => {
              const option = document.createElement("option");
              option.value = String(choice.value);
              option.textContent = choice.label;
              control.appendChild(option);
            });
            const wanted = String(row.value == null ? "" : row.value);
            if (wanted && !Array.from(control.options)
                    .some((option) => option.value === wanted)) {
              const option = document.createElement("option");
              option.value = wanted;
              option.textContent = wanted;
              control.appendChild(option);
            }
            control.value = wanted;
            read = () => control.value;
          } else if (row.kind === "int") {
            control = document.createElement("input");
            control.type = "number";
            control.inputMode = "numeric";
            if (row.minimum != null) control.min = String(row.minimum);
            if (row.maximum != null) control.max = String(row.maximum);
            control.value = row.value == null ? "" : String(row.value);
            read = () => (control.value === "" ? "" : Number(control.value));
          } else if (row.kind === "secret") {
            control = document.createElement("input");
            control.type = "password";
            control.autocomplete = "new-password";
            control.spellcheck = false;
            control.placeholder = row.has_value
              ? "stored — leave empty to keep it"
              : "paste the key";
            control.dataset.secret = "1";
            // an empty field means "keep what is stored", never "delete it"
            read = () => (control.value.trim() ? control.value.trim() : null);
          } else {
            control = document.createElement("input");
            control.type = "text";
            control.spellcheck = false;
            control.placeholder = row.default ? String(row.default) : "";
            control.value = row.value == null ? "" : String(row.value);
            read = () => control.value.trim();
          }
          control.dataset.key = row.key;
          line.appendChild(control);
          box.appendChild(line);
        }
        if (row.help) {
          const help = document.createElement("p");
          help.className = "note";
          help.textContent = row.help;
          box.appendChild(help);
        }
        return { row, wrap: box, control, read };
      };

      rows.forEach((row) => {
        const name = String(row.group || "Other");
        const panel = tabPanel(name, groupHint[name] || "");
        sections.set(name, panel);
        const field = settingField(row);
        byKey.set(row.key, field);
        controls.push(field);
        panel.appendChild(field.wrap);
      });

      // -- library: open the folder right away ------------------------------
      const libField = byKey.get("library");
      if (libField && sections.has("Library")) {
        const box = sections.get("Library");
        const row = document.createElement("div");
        row.className = "settings-row";
        useLibrary = document.createElement("button");
        useLibrary.className = "btn";
        useLibrary.type = "button";
        useLibrary.textContent = "Use this folder now";
        useLibrary.title = "Open the folder that is typed above";
        row.appendChild(useLibrary);
        box.appendChild(row);
        const note = document.createElement("p");
        note.className = "note";
        note.textContent =
          "A folder this app can see is opened at once: the player stops, the " +
          "library is re-read and the window switches over. One it cannot see " +
          "(outside the mounted folder) is saved for the next start — the " +
          "launcher reads it and mounts that folder.";
        box.appendChild(note);
      }

      // -- library index: the pipeline the app runs itself ------------------
      const indexBox = section(
        "Library index",
        "The inventory, the reports and the generated playlists.");
      {
        indexCounts = document.createElement("p");
        indexCounts.className = "note settings-counts";
        indexCounts.textContent = "Reading the inventory…";
        indexProgress = document.createElement("div");
        indexProgress.className = "settings-progress";
        indexProgress.hidden = true;
        indexLog = document.createElement("pre");
        indexLog.className = "settings-log";
        indexLog.hidden = true;
        const row = document.createElement("div");
        row.className = "settings-row";
        indexBtn = document.createElement("button");
        indexBtn.className = "btn primary";
        indexBtn.type = "button";
        indexBtn.textContent = "Index the library";
        indexBtn.title = "Scan new or changed files, refresh the reports and "
          + "rebuild the playlists";
        row.appendChild(indexBtn);
        indexPauseBtn = document.createElement("button");
        indexPauseBtn.className = "btn";
        indexPauseBtn.type = "button";
        indexPauseBtn.textContent = "Pause";
        indexPauseBtn.title = "Hold the scan where it is; it keeps its place";
        indexPauseBtn.hidden = true;
        row.appendChild(indexPauseBtn);
        indexCancelBtn = document.createElement("button");
        indexCancelBtn.className = "btn";
        indexCancelBtn.type = "button";
        indexCancelBtn.textContent = "Cancel";
        indexCancelBtn.title = "Stop the job for good; the inventory keeps "
          + "what it already wrote";
        indexCancelBtn.hidden = true;
        row.appendChild(indexCancelBtn);
        const hint = document.createElement("p");
        hint.className = "note";
        hint.textContent =
          "Runs in the background and reads only what is new.  It writes the " +
          "inventory, the reports and the generated playlists — never a music " +
          "file, and it deletes nothing.  Pause holds it where it is; cancel " +
          "ends it, and a later run picks up from the inventory.";
        indexBox.append(indexCounts, indexProgress, row, hint, indexLog);
        // the job lives with the folder it scans, at the end of its tab
        (sections.get("Library") || tabPanel("Library")).appendChild(indexBox);
      }

      // -- library health: a read-only audit --------------------------------
      const healthBox = section(
        "Library health",
        "Tags, artwork, decode errors and the index — read-only.");
      {
        const row = document.createElement("div");
        row.className = "settings-row";
        const check = document.createElement("button");
        check.className = "btn";
        check.type = "button";
        check.textContent = "Check the library";
        check.title = "Read the inventory and the databases; nothing is "
          + "written or deleted";
        row.appendChild(check);
        const out = document.createElement("div");
        out.className = "health-list";
        out.hidden = true;
        healthBox.append(row, out);
        check.onclick = async () => {
          check.disabled = true;
          out.hidden = false;
          out.textContent = "Reading the inventory…";
          try {
            const data = await api("/api/health", undefined, { timeout: 90 });
            renderHealth(out, data);
          } catch (err) {
            out.textContent = err.message;
          } finally {
            check.disabled = false;
          }
        };
        (sections.get("Library") || tabPanel("Library")).appendChild(healthBox);
      }

      // -- duplicate review: every copy stays where it is -------------------
      const duplicateBox = section(
        "Duplicate review",
        "The same song on two albums — read-only, nothing is deleted.");
      {
        const row = document.createElement("div");
        row.className = "settings-row";
        const show = document.createElement("button");
        show.className = "btn";
        show.type = "button";
        show.textContent = "Show the duplicate groups";
        show.title = "Read the duplicate reports; nothing is moved or deleted";
        row.appendChild(show);
        const out = document.createElement("div");
        out.className = "health-list";
        out.hidden = true;
        duplicateBox.append(row, out);
        show.onclick = async () => {
          show.disabled = true;
          out.hidden = false;
          out.textContent = "Reading the duplicate reports…";
          try {
            renderDuplicates(out, await api("/api/duplicates", undefined,
                                            { timeout: 90 }));
          } catch (err) {
            out.textContent = err.message;
          } finally {
            show.disabled = false;
          }
        };
        (sections.get("Library") || tabPanel("Library"))
          .appendChild(duplicateBox);
      }

      // -- recently added: the newest files, newest first -------------------
      const arrivalsBox = section(
        "Recently added",
        "The newest files by their own time — read-only.");
      {
        const row = document.createElement("div");
        row.className = "settings-row";
        const span = document.createElement("select");
        span.setAttribute("aria-label", "How far back");
        [["7", "Last 7 days"], ["31", "Last 31 days"], ["0", "Everything"]]
          .forEach(([value, label]) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = label;
            span.appendChild(option);
          });
        span.value = "31";
        const show = document.createElement("button");
        show.className = "btn";
        show.type = "button";
        show.textContent = "Show the newest tracks";
        show.title = "Read the inventory's file times; nothing is changed";
        row.append(span, show);
        const out = document.createElement("div");
        out.className = "health-list";
        out.hidden = true;
        arrivalsBox.append(row, out);
        show.onclick = async () => {
          show.disabled = true;
          out.hidden = false;
          out.textContent = "Reading the inventory…";
          try {
            const days = span.value === "0" ? "" : `?days=${span.value}`;
            renderArrivals(out, await api("/api/recent-tracks" + days,
                                          undefined, { timeout: 90 }));
          } catch (err) {
            out.textContent = err.message;
          } finally {
            show.disabled = false;
          }
        };
        (sections.get("Library") || tabPanel("Library"))
          .appendChild(arrivalsBox);
      }

      // -- library statistics: what the collection holds --------------------
      const statsBox = section(
        "Library statistics",
        "Size, genres, decades, formats and how complete the tags are.");
      {
        const row = document.createElement("div");
        row.className = "settings-row";
        const show = document.createElement("button");
        show.className = "btn";
        show.type = "button";
        show.textContent = "Show the statistics";
        show.title = "Read the inventory; nothing is written";
        row.appendChild(show);
        const out = document.createElement("div");
        out.className = "health-list";
        out.hidden = true;
        statsBox.append(row, out);
        show.onclick = async () => {
          show.disabled = true;
          out.hidden = false;
          out.textContent = "Reading the inventory…";
          try {
            const data = await api("/api/stats", undefined, { timeout: 90 });
            renderStats(out, data);
          } catch (err) {
            out.textContent = err.message;
          } finally {
            show.disabled = false;
          }
        };
        (sections.get("Library") || tabPanel("Library")).appendChild(statsBox);
      }

      // -- library data: the daily plain-text backup and its import ---------
      const backupBox = section(
        "Library data",
        "The sound profile and the lyrics, as plain text in LibraryData/.");
      {
        backupCounts = document.createElement("p");
        backupCounts.className = "note settings-counts";
        backupCounts.textContent = "Reading the backup folder…";
        backupProgress = document.createElement("div");
        backupProgress.className = "settings-progress";
        backupProgress.hidden = true;
        backupLog = document.createElement("pre");
        backupLog.className = "settings-log";
        backupLog.hidden = true;

        const row = document.createElement("div");
        row.className = "settings-row";
        backupBtn = document.createElement("button");
        backupBtn.className = "btn primary";
        backupBtn.type = "button";
        backupBtn.textContent = "Back up now";
        backupBtn.title = "Fingerprint what is new, write today's plain-text "
          + "export and prune the old ones";
        row.appendChild(backupBtn);
        backupPauseBtn = document.createElement("button");
        backupPauseBtn.className = "btn";
        backupPauseBtn.type = "button";
        backupPauseBtn.textContent = "Pause";
        backupPauseBtn.title = "Hold the backup where it is; it keeps its place";
        backupPauseBtn.hidden = true;
        row.appendChild(backupPauseBtn);

        const hint = document.createElement("p");
        hint.className = "note";
        hint.textContent =
          "Once a day, and whenever you press the button, the app writes one " +
          "plain-text file per day into the library's LibraryData folder: " +
          "every measured property, the profile axes and the timed lyric " +
          "lines, one JSON object per line. Each record carries a hash of the " +
          "audio itself (tags and file name excluded), so it can be matched " +
          "again after files were renamed or retagged.";

        const importRow = document.createElement("div");
        importRow.className = "settings-row";
        importFile = document.createElement("input");
        importFile.type = "text";
        importFile.spellcheck = false;
        importFile.placeholder = "Path to a library-data-….jsonl export";
        importCheck = document.createElement("button");
        importCheck.className = "btn";
        importCheck.type = "button";
        importCheck.textContent = "Check";
        importCheck.title = "Report what this file would fill in; writes "
          + "nothing";
        importBtn = document.createElement("button");
        importBtn.className = "btn";
        importBtn.type = "button";
        importBtn.textContent = "Import";
        importBtn.title = "Write the matching records into the databases, "
          + "matched by audio fingerprint";
        importRow.append(importFile, importCheck, importBtn);

        const importOpts = document.createElement("label");
        importOpts.className = "settings-check";
        importOverwrite = document.createElement("input");
        importOverwrite.type = "checkbox";
        importOpts.append(importOverwrite,
                          document.createTextNode(
                            " Replace measurements and lyrics that already exist"));
        importNote = document.createElement("p");
        importNote.className = "note";
        importNote.hidden = true;

        backupBox.append(backupCounts, backupProgress, row, hint,
                         importRow, importOpts, importNote, backupLog);
        (sections.get("Library") || tabPanel("Library")).appendChild(backupBox);
      }

      // -- language model: what is in use, and the server's models ----------
      const llmBox = sections.get("Language model");
      let renderLlm = null;
      let renderModels = null;
      if (llmBox) {
        const summary = document.createElement("p");
        summary.className = "note settings-llm";
        llmBox.insertBefore(summary, llmBox.children[1] || null);
        const modelsNote = document.createElement("p");
        modelsNote.className = "note";
        const list = document.createElement("datalist");
        list.id = "llm-model-options";
        const modelField = byKey.get("llm_model");
        if (modelField && modelField.control) {
          modelField.control.setAttribute("list", "llm-model-options");
        }
        llmBox.append(list, modelsNote);

        renderModels = (info) => {
          list.innerHTML = "";
          const models = Array.isArray(info && info.models) ? info.models : [];
          models.forEach((name) => {
            const option = document.createElement("option");
            option.value = String(name);
            list.appendChild(option);
          });
          modelsNote.textContent = models.length
            ? `${models.length} model${models.length === 1 ? "" : "s"} on this server — pick one in the field above, or type a name.`
            : (info && info.available
               ? "The server did not list its models."
               : "No model list yet: choose a provider, give the key, then reload.");
        };

        renderLlm = (info) => {
          info = info || {};
          const bits = [`Planner: ${info.summary || "unknown"}`];
          if (info.base_url) bits.push(info.base_url);
          if (info.key_set) bits.push("key stored");
          else if (info.provider && info.provider !== "auto") {
            bits.push("no key yet");
          }
          summary.textContent = bits.join(" · ");
          renderModels(info);
        };
        renderLlm(data.llm);

        const reload = document.createElement("div");
        reload.className = "settings-row";
        const reloadBtn = document.createElement("button");
        reloadBtn.className = "btn";
        reloadBtn.type = "button";
        reloadBtn.textContent = "Reload the model list";
        reloadBtn.title = "Ask the server for the models it offers";
        reload.appendChild(reloadBtn);
        llmBox.appendChild(reload);
        reloadBtn.onclick = async () => {
          reloadBtn.disabled = true;
          reloadBtn.textContent = "Asking the server…";
          try {
            const fresh = await api("/api/settings", undefined,
                                    { timeout: 90, retries: 0 });
            if (!alive()) return;
            renderLlm(fresh.llm);
            if (fresh.llm && fresh.llm.available) {
              say("Model list updated.", "good");
            } else {
              say("The server did not answer — check the address and the key.",
                  "error");
            }
          } catch (err) {
            if (alive()) say(err.message, "error");
          } finally {
            reloadBtn.disabled = false;
            reloadBtn.textContent = "Reload the model list";
          }
        };
      }

      // -- voice: can this machine transcribe at all ------------------------
      const voiceBox = sections.get("Voice");
      if (voiceBox) {
        const info = data.voice || {};
        const note = document.createElement("p");
        note.className = "note";
        const bits = [];
        if (!info.available) {
          bits.push("No local transcriber is available in this installation.");
        } else if (!info.ready) {
          bits.push("The transcriber is installed, but this interpreter cannot " +
                    "load faster-whisper — run setup_asr.sh.");
        } else {
          const gpu = info.gpu && info.gpu !== "unknown" ? ` · ${info.gpu}` : " · CPU";
          bits.push(`Local transcriber ready${gpu}.`);
        }
        if (Array.isArray(info.models) && info.models.length) {
          bits.push(`installed models: ${info.models.join(", ")}`);
        }
        bits.push("The microphone button in the composer records; the clip is " +
                  "deleted as soon as it has been transcribed.");
        note.textContent = bits.join(" ");
        voiceBox.appendChild(note);
      }

      // -- sound profile: the measurement job -------------------------------
      const psychoBox = sections.get("Sound profile");
      let counts = null, progress = null, measure = null, remeasure = null;
      if (psychoBox) {
        counts = document.createElement("p");
        counts.className = "note settings-counts";
        counts.textContent = "Reading the profile database…";
        progress = document.createElement("div");
        progress.className = "settings-progress";
        progress.hidden = true;
        const row = document.createElement("div");
        row.className = "settings-row";
        measure = document.createElement("button");
        measure.className = "btn primary";
        measure.type = "button";
        measure.textContent = "Measure the library";
        measure.title = "Measure every track the profile is missing, then rebuild the axes and families";
        remeasure = document.createElement("button");
        remeasure.className = "btn";
        remeasure.type = "button";
        remeasure.textContent = "Re-measure everything";
        remeasure.title = "Ignore what is cached and measure all tracks again";
        row.append(measure, remeasure);
        const note = document.createElement("p");
        note.className = "note";
        note.textContent =
          "The measurement runs in the background — the window keeps working " +
          "and the progress appears here.";
        psychoBox.append(counts, progress, row, note);
      }

      // -- diagnostics ------------------------------------------------------
      const diagBox = tabPanel("Diagnostics",
                               "What to look at when the window says “Load failed”.");
      const diagNote = document.createElement("p");
      diagNote.className = "note settings-diag";
      diagNote.textContent = "Reading the server counters…";
      const diagRow = document.createElement("div");
      diagRow.className = "settings-row";
      const diagRefresh = document.createElement("button");
      diagRefresh.className = "btn";
      diagRefresh.type = "button";
      diagRefresh.textContent = "Check again";
      const diagCopy = document.createElement("button");
      diagCopy.className = "btn";
      diagCopy.type = "button";
      diagCopy.textContent = "Copy report";
      diagCopy.title = "Copy the counters, the last errors and this window's failed calls";
      const diagClear = document.createElement("button");
      diagClear.className = "btn";
      diagClear.type = "button";
      diagClear.textContent = "Clean up logs";
      diagClear.title =
        "Empty server-errors.log and server-requests.log; the previous " +
        "content is kept next to them as .1";
      diagRow.append(diagRefresh, diagClear, diagCopy);
      diagBox.append(diagNote, diagRow);

      // -- saving -----------------------------------------------------------
      const saveRow = document.createElement("div");
      saveRow.className = "modal-actions settings-save";
      const status = document.createElement("span");
      status.className = "note";
      status.style.margin = "0";
      const save = document.createElement("button");
      save.className = "btn primary";
      save.type = "button";
      save.textContent = "Save";
      saveRow.append(status, save);
      wrap.appendChild(saveRow);

      let closed = false;
      const alive = () => !closed && !els.modal.hidden;

      const say = (text, kind = "") => {
        status.textContent = text;
        if (kind === "error") status.style.color = "var(--bad)";
        else if (kind === "good") status.style.color = "var(--good)";
        else status.style.color = "";
      };

      // what changed since the panel was filled in
      const collect = () => {
        const values = {};
        controls.forEach(({ row, read }) => {
          let value;
          try { value = read(); } catch (_) { return; }
          if (value === null) return;                 // untouched secret
          if (row.kind === "int") {
            if (value === "" || Number(value) === Number(row.value)) return;
          } else if (row.kind === "bool") {
            if (Boolean(value) === Boolean(row.value)) return;
          } else if (String(value) === String(row.value == null ? "" : row.value)) {
            return;
          }
          values[row.key] = value;
        });
        return values;
      };

      // a response carries every current value; make it the new baseline so a
      // second Save does not send the same change again
      const rebase = (current) => {
        if (!current) return;
        controls.forEach(({ row, control }) => {
          if (!(row.key in current)) return;
          const value = current[row.key];
          row.value = value;
          if (!control) return;
          if (row.kind === "bool") control.checked = Boolean(value);
          else if (row.kind === "secret") {
            row.has_value = Boolean(value);
            control.value = "";
            control.placeholder = row.has_value
              ? "stored — leave empty to keep it" : "paste the key";
          } else control.value = value == null ? "" : String(value);
        });
      };

      const put = async (values, okText) => {
        try {
          const result = await api("/api/settings", values,
                                   { timeout: 90, retries: 0 });
          rebase(result);
          if (renderLlm && result.llm) renderLlm(result.llm);
          if (result.library_result && result.library_result.error) {
            say(result.library_result.error, "error");
            refreshMeta();
            return result;
          }
          refreshMeta();
          const problems = Array.isArray(result.errors) ? result.errors : [];
          if (problems.length) say(problems.join(" · "), "error");
          else say(okText || "Saved.", "good");
          return result;
        } catch (err) {
          say(err.message, "error");
          return null;
        }
      };

      if (useLibrary && byKey.has("library")) {
        useLibrary.onclick = async () => {
          const wanted = byKey.get("library").control.value.trim();
          if (!wanted) {
            say("Give a folder, for example /home/you/Musik.", "error");
            return;
          }
          useLibrary.disabled = true;
          say("Opening…");
          const result = await put({ library: wanted }, "");
          useLibrary.disabled = false;
          if (!result) return;
          const answer = result.library_result || {};
          if (answer.error) {
            say(answer.error, "error");
            return;
          }
          const shown = answer.library || result.library || wanted;
          byKey.get("library").control.value = shown;
          say(answer.applied
              ? `Now working on ${shown}.`
              : `${shown} is saved — the next start opens it.`);
        };
      }

      save.onclick = async () => {
        const values = collect();
        if (!Object.keys(values).length) {
          say("Nothing to change.");
          return;
        }
        save.disabled = true;
        say("Saving…");
        try {
          await put(values, "Saved.");
        } finally {
          save.disabled = false;
        }
        if (byKey.has("visualizer") || byKey.has("viz_style")) applyViz();
      };

      // -- the profile counts and the job -----------------------------------
      const renderJob = (job) => {
        if (!progress) return;
        const total = job.total || 0;
        const done = job.done || 0;
        if (!job.running) {
          progress.hidden = true;
          return;
        }
        progress.hidden = false;
        const label = total
          ? `${job.step || "measuring"} · ${formatNumber(done)} of ${formatNumber(total)} · ${Math.round(job.percent || 0)} %`
          : `${job.step || "measuring"}…`;
        progress.innerHTML = "";
        progress.appendChild(bar(job.percent || 0, label));
      };

      const renderCounts = (data) => {
        if (!counts) return;
        const stats = data.counts || {};
        if (!data.available) {
          counts.textContent =
            "This library has no scripts/psycho_features.py, so the app " +
            "cannot measure it.";
          measure.disabled = true;
          remeasure.disabled = true;
          return;
        }
        const total = stats.total || 0;
        const measured = stats.measured || 0;
        const detailed = stats.detailed || 0;
        const parts = [];
        if (total) {
          parts.push(`${formatNumber(measured)} of ${formatNumber(total)} measured`);
          if (detailed < measured) {
            parts.push(`${formatNumber(measured - detailed)} without the newer measurements`);
          }
        } else {
          parts.push("no measurements yet");
        }
        parts.push(`${formatNumber(stats.profiles || 0)} profiles`);
        parts.push(`${formatNumber(stats.families || 0)} families`);
        counts.textContent = parts.join(" · ");
        const done = total > 0 && measured >= total && detailed >= measured;
        measure.disabled = Boolean(data.running);
        remeasure.disabled = Boolean(data.running);
        measure.textContent = done ? "Check for new tracks" : "Measure the library";
        renderJob(data);
      };

      const refreshJob = async () => {
        try {
          const data = await api("/api/psycho/update");
          if (!alive()) return;
          renderCounts(data);
        } catch (_) { /* a locked database is not fatal */ }
      };

      const startJob = async (refresh) => {
        measure.disabled = true;
        remeasure.disabled = true;
        say(refresh ? "Re-measuring every track…" : "Measuring what is missing…");
        try {
          const data = await api("/api/psycho/update",
                                 refresh ? { refresh: true } : {});
          if (data.error) {
            say(data.error, "error");
          } else if (data.running) {
            say("Measuring in the background.");
          }
          renderCounts(data);
          toast(refresh ? "Re-measuring the library in the background"
                        : "Measuring the library in the background", "good");
        } catch (err) {
          say(err.message, "error");
          measure.disabled = false;
          remeasure.disabled = false;
        }
      };

      if (measure) measure.onclick = () => startJob(false);
      if (remeasure) remeasure.onclick = () => startJob(true);

      // -- the library index job -------------------------------------------
      const indexTime = (iso) => {
        const text = String(iso || "");
        if (!text) return "";
        return text.replace("T", " ").replace(/\.\d+/, "")
                   .replace(/\+00:00$/, " UTC");
      };

      const renderIndex = (data) => {
        indexData = data || null;
        if (!indexCounts || !indexBtn) return;
        const stats = (data && data.counts) || {};
        if (data && data.available === false) {
          indexCounts.textContent =
            "This library has no scripts/phase0_inventory.py, so the app "
            + "cannot index it.";
          indexBtn.disabled = true;
          return;
        }
        const parts = [];
        if (stats.tracks) {
          parts.push(`${formatNumber(stats.tracks)} tracks in the inventory`);
        } else {
          parts.push("no inventory yet");
        }
        if (stats.playlists) {
          parts.push(`${formatNumber(stats.playlists)} playlists`);
        }
        if (stats.indexed_at) parts.push(`written ${indexTime(stats.indexed_at)}`);
        indexCounts.textContent = parts.join(" · ");
        const state = String((data && data.state) || "");
        const active = Boolean(data && (data.active || data.running));
        const paused = state === "paused";
        const stopping = state === "cancelling";
        indexBtn.disabled = active;
        indexBtn.textContent = active ? "Indexing…" : "Index the library";
        if (indexPauseBtn) {
          indexPauseBtn.hidden = !active || stopping;
          indexPauseBtn.disabled = stopping;
          indexPauseBtn.textContent = paused ? "Continue" : "Pause";
          indexPauseBtn.title = paused
            ? "Carry on from where the scan stopped"
            : "Hold the scan where it is; it keeps its place";
        }
        if (indexCancelBtn) {
          indexCancelBtn.hidden = !active;
          indexCancelBtn.disabled = stopping;
          indexCancelBtn.textContent = stopping ? "Stopping…" : "Cancel";
        }
        indexProgress.hidden = !active;
        const tail = (data && data.lines) || [];
        const stopped = Boolean(data) && !active
          && (state === "failed" || state === "cancelled") && tail.length;
        indexLog.hidden = !stopped;
        if (stopped) indexLog.textContent = tail.join("\n");
        if (!active) return;
        const label = data.phase === "scan" && data.total
          ? `${paused ? "paused at" : (data.step || "scanning")} · `
            + `${formatNumber(data.done)} of ${formatNumber(data.total)} · `
            + `${Math.round(data.percent || 0)} %`
          : (paused ? "paused" : (data.step || "indexing") + "…");
        indexProgress.innerHTML = "";
        indexProgress.appendChild(bar(data.percent || 0, label));
      };

      const refreshIndex = async () => {
        let data;
        try {
          data = await api("/api/index/update");
        } catch (_) {
          return;                         // a locked inventory is not fatal
        }
        if (!alive()) return;
        const state = String((data && data.state) || "");
        const active = Boolean(data && (data.running || data.active));
        if (indexWasRunning && !active) {
          if (state === "done") {
            const stats = data.counts || {};
            say(`Library index updated · ${formatNumber(stats.tracks)} tracks.`,
                "good");
            toast("Library index updated", "good");
            refreshMeta();
          } else if (state === "cancelled") {
            say("The index was cancelled — the inventory keeps what it wrote.",
                "");
            toast("Library index cancelled", "");
            refreshMeta();
          } else if (state === "failed") {
            say("The index stopped — the log tail is below.", "error");
          }
        }
        indexWasRunning = active;
        renderIndex(data);
      };

      const controlIndex = async (action) => {
        const labels = { pause: "Paused", resume: "Continued",
                         cancel: "Cancelled" };
        indexBtn.disabled = true;
        if (indexPauseBtn) indexPauseBtn.disabled = true;
        if (indexCancelBtn) indexCancelBtn.disabled = true;
        try {
          const data = await api("/api/index/update", { action });
          if (!alive()) return;
          if (data.error) {
            say(data.error, "error");
          } else if (data.note) {
            say(data.note, "");
          } else {
            say(`${labels[action] || "Index"} · ${data.state || ""}`.trim(), "");
          }
          indexWasRunning = Boolean(data && (data.running || data.active));
          renderIndex(data);
          if (!indexWasRunning) refreshMeta();
        } catch (err) {
          if (alive()) say(err.message, "error");
          if (indexData) renderIndex(indexData);   // re-arm the buttons
        }
      };

      if (indexBtn) {
        indexBtn.onclick = async () => {
          indexBtn.disabled = true;
          say("Indexing the library in the background…");
          try {
            const data = await api("/api/index/update", {});
            if (data.error) {
              say(data.error, "error");
              indexBtn.disabled = false;
              return;
            }
            indexWasRunning = Boolean(data.running || data.active);
            renderIndex(data);
            toast("Indexing the library in the background", "good");
          } catch (err) {
            say(err.message, "error");
            indexBtn.disabled = false;
          }
        };
      }
      if (indexPauseBtn) {
        indexPauseBtn.onclick = () => {
          const paused = String((indexData && indexData.state) || "") ===
            "paused";
          controlIndex(paused ? "resume" : "pause");
        };
      }
      if (indexCancelBtn) {
        indexCancelBtn.onclick = () => controlIndex("cancel");
      }

      // -- the daily backup and its import ----------------------------------
      const renderBackup = (data) => {
        backupData = data || null;
        if (!backupCounts || !backupBtn) return;
        const stats = (data && data.counts) || {};
        if (data && data.available === false) {
          backupCounts.textContent =
            "This library has no scripts/library_data.py, so the app cannot "
            + "back its data up.";
          backupBtn.disabled = true;
          importCheck.disabled = true;
          importBtn.disabled = true;
          return;
        }
        const latest = stats.latest || {};
        const prints = stats.fingerprints || {};
        const parts = [];
        parts.push(`${formatNumber(stats.psycho || 0)} measured tracks`);
        parts.push(`${formatNumber(stats.lyrics || 0)} lyric songs`);
        parts.push(`${formatNumber(prints.known || 0)} fingerprinted`);
        if (latest.name) {
          parts.push(`latest ${latest.day} · ${Math.round((latest.bytes || 0)
            / (1024 * 1024) * 10) / 10} MB`);
        } else {
          parts.push("no export yet");
        }
        parts.push(`${formatNumber(stats.exports || 0)} kept`);
        backupCounts.textContent = parts.join(" · ");
        // the newest export is the obvious thing to import, so offer it
        if (importFile && !importFile.value && latest.name) {
          importFile.value = `LibraryData/${latest.name}`;
        }

        const state = String((data && data.state) || "");
        const active = Boolean(data && (data.active || data.running));
        const paused = state === "paused";
        backupBtn.disabled = active;
        backupBtn.textContent = active ? "Backing up…" : "Back up now";
        if (backupPauseBtn) {
          backupPauseBtn.hidden = !active;
          backupPauseBtn.textContent = paused ? "Continue" : "Pause";
          backupPauseBtn.title = paused
            ? "Carry on from where the backup stopped"
            : "Hold the backup where it is; it keeps its place";
        }
        backupProgress.hidden = !active;
        const tail = (data && data.lines) || [];
        const stopped = Boolean(data) && !active
          && state === "failed" && tail.length;
        backupLog.hidden = !stopped;
        if (stopped) backupLog.textContent = tail.join("\n");
        if (!active) return;
        const mode = String((data && data.mode) || "backup");
        const label = data.total
          ? `${paused ? "paused at" : (data.step || mode)} · `
            + `${formatNumber(data.done)} of ${formatNumber(data.total)} · `
            + `${Math.round(data.percent || 0)} %`
          : (paused ? "paused" : (data.step || mode) + "…");
        backupProgress.innerHTML = "";
        backupProgress.appendChild(bar(data.percent || 0, label));
      };

      const refreshBackup = async () => {
        let data;
        try {
          data = await api("/api/library-data");
        } catch (_) {
          return;
        }
        if (!alive()) return;
        const state = String((data && data.state) || "");
        const active = Boolean(data && (data.running || data.active));
        if (backupWasRunning && !active) {
          if (state === "done") {
            const mode = String((data && data.mode) || "backup");
            say(mode === "import"
              ? "Library data imported."
              : `Library data backed up · ${(data.counts || {}).latest
                  ? "today's export written" : "done"}.`, "good");
            toast(mode === "import" ? "Library data imported"
                                    : "Library data backed up", "good");
            refreshMeta();
            refreshWork();
          } else if (state === "failed") {
            say("The backup stopped — the log tail is below.", "error");
          }
        }
        backupWasRunning = active;
        renderBackup(data);
      };

      const startBackup = async () => {
        backupBtn.disabled = true;
        say("Backing up the library data…");
        try {
          const data = await api("/api/library-data", { action: "start" });
          if (!alive()) return;
          if (data.error) {
            say(data.error, "error");
            backupBtn.disabled = false;
            return;
          }
          backupWasRunning = Boolean(data.running || data.active);
          renderBackup(data);
          toast("Backing up the library data in the background", "good");
          refreshWork();
        } catch (err) {
          say(err.message, "error");
          backupBtn.disabled = false;
        }
      };

      const importPayload = () => {
        const file = String(importFile.value || "").trim();
        if (!file) {
          say("Give the path of an export file first.", "error");
          return null;
        }
        return { file, overwrite: Boolean(importOverwrite.checked) };
      };

      if (backupBtn) backupBtn.onclick = () => startBackup();
      if (backupPauseBtn) {
        backupPauseBtn.onclick = async () => {
          const paused = String((backupData && backupData.state) || "") ===
            "paused";
          try {
            const data = await api("/api/library-data",
                                   { action: paused ? "resume" : "pause" });
            if (!alive()) return;
            if (data.error) say(data.error, "error");
            else if (data.note) say(data.note, "");
            renderBackup(data);
            refreshWork();
          } catch (err) {
            say(err.message, "error");
          }
        };
      }
      if (importCheck) {
        importCheck.onclick = async () => {
          const payload = importPayload();
          if (!payload) return;
          importCheck.disabled = true;
          importNote.hidden = false;
          importNote.textContent = "Checking the file…";
          try {
            const data = await api("/api/library-data",
                                   { action: "preview", file: payload.file },
                                   { timeout: 180 });
            if (!alive()) return;
            if (data.error) {
              importNote.textContent = String(data.error);
              return;
            }
            const counts = data.counts || {};
            const bits = [
              `${formatNumber(data.matched || 0)} of ${formatNumber(data.records || 0)} records match this library`,
              `${formatNumber(data.by_path || 0)} only by path`,
              `${formatNumber(data.unmatched || 0)} unmatched`,
              `${formatNumber(counts.tracks || 0)} measured tracks`,
              `${formatNumber(counts.lyrics || 0)} lyric songs`,
              `${formatNumber(counts.lines || 0)} lines`,
            ];
            importNote.textContent = bits.join(" · ") + ".";
          } catch (err) {
            if (alive()) importNote.textContent = err.message;
          } finally {
            if (alive()) importCheck.disabled = false;
          }
        };
      }
      if (importBtn) {
        importBtn.onclick = async () => {
          const payload = importPayload();
          if (!payload) return;
          importBtn.disabled = true;
          try {
            const data = await api("/api/library-data",
                                   { action: "import", ...payload });
            if (!alive()) return;
            if (data.error) {
              say(data.error, "error");
              importNote.hidden = false;
              importNote.textContent = String(data.error);
              return;
            }
            backupWasRunning = true;
            renderBackup(data);
            say("Importing the export — matching every record by audio "
                + "fingerprint.", "");
            toast("Importing the library data in the background", "good");
            refreshWork();
          } catch (err) {
            say(err.message, "error");
          } finally {
            if (alive()) importBtn.disabled = false;
          }
        };
      }

      // -- diagnostics ------------------------------------------------------
      let diagData = null;
      const renderDiag = (data) => {
        diagData = data || null;
        const lines = [];
        if (!data) {
          lines.push("The server did not answer the diagnostics call.");
        } else if (data.error) {
          lines.push(String(data.error));
        } else {
          lines.push(`server pid ${data.pid} · up ${formatUptime(data.uptime_s)} · ` +
                     `${formatNumber(data.requests)} requests · ` +
                     `${formatNumber(data.errors)} errors · ` +
                     `${formatNumber(data.slow)} slow`);
          const since = formatClock(data.started_at);
          if (since) lines.push(`started ${since}`);
          const last = data.last_error;
          if (last) {
            const text = String(last.error || "").trim().split("\n").filter(Boolean).pop() || "";
            lines.push(`last error: ${last.method} ${last.path} → ${last.code || 500} · ` +
                       `${last.ms} ms · ${text.slice(0, 200)}`);
          } else {
            lines.push("last error: none logged");
          }
          if (data.log) lines.push(`log: ${data.log}`);
        }
        const mine = state.clientErrors.slice(-3);
        if (mine.length) {
          lines.push("this window: " + mine
            .map((row) => `${row.at} ${row.method} ${row.path}: ${row.message}`)
            .join(" | "));
        }
        diagNote.textContent = lines.join("\n");
      };

      const diagReport = () => JSON.stringify({
        server: diagData,
        window: state.clientErrors,
        settings: (state.meta && state.meta.settings) || {},
        at: new Date().toISOString(),
      }, null, 2);

      const refreshDiag = async () => {
        try {
          const data = await api("/api/diagnostics", undefined,
                                 { retries: 1, timeout: 15 });
          if (!alive()) return;
          renderDiag(data);
        } catch (err) {
          if (!alive()) return;
          renderDiag({ error: err.message });
        }
      };

      diagRefresh.onclick = () => {
        diagNote.textContent = "Reading the server counters…";
        refreshDiag();
      };
      let diagArmed = false;
      diagClear.onclick = async () => {
        if (!diagArmed) {
          diagArmed = true;
          diagClear.textContent = "Really clean up?";
          return;
        }
        diagClear.disabled = true;
        try {
          const answer = await api("/api/diagnostics", { action: "clear" });
          if (!alive()) return;
          const kb = (Number(answer.bytes || 0) / 1024).toFixed(1);
          const list = (answer.cleared || []).map((p) => p.split("/").pop());
          toast(list.length
            ? `Cleaned ${list.join(", ")} (${kb} kB kept as .1)`
            : "Nothing to clean up");
          await refreshDiag();
        } catch (err) {
          if (alive()) toast(err.message, "error");
        } finally {
          diagClear.disabled = false;
          diagArmed = false;
          diagClear.textContent = "Clean up logs";
        }
      };
      diagCopy.onclick = async () => {
        const text = diagReport();
        try {
          await navigator.clipboard.writeText(text);
          diagCopy.textContent = "Copied";
          setTimeout(() => { diagCopy.textContent = "Copy report"; }, 1800);
        } catch (_) {
          // no clipboard permission in this window: hand the text to a box
          const area = document.createElement("textarea");
          area.value = text;
          area.rows = 10;
          area.style.width = "100%";
          diagBox.appendChild(area);
          area.select();
        }
      };

      // keep the progress alive while the dialog is open, stop when it closes
      const tick = setInterval(async () => {
        if (!alive()) {
          closed = true;
          clearInterval(tick);
          return;
        }
        if (psychoBox) await refreshJob();
        if (indexBtn) await refreshIndex();
        if (backupBtn) await refreshBackup();
      }, 2500);
      if (psychoBox) refreshJob();
      if (indexBtn) refreshIndex();
      if (backupBtn) refreshBackup();
      refreshDiag();

      // reopen on the tab that was used last, so a revisit lands where the
      // change was being made; the first tab otherwise
      showTab(tabs.has(state.settingsTab) ? state.settingsTab : order[0]);

      return wrap;
    }, { wide: true });
  }

  // ------------------------------------------------------- youtube jobs

  async function pollYoutube() {
    let data;
    try {
      data = await api("/api/youtube");
    } catch (_) {
      return;
    }
    const seen = new Set();
    (data.jobs || []).forEach((job) => {
      const finished = ["done", "partial", "failed", "unknown", "duplicate"]
        .includes(job.status);
      if (!finished) return;
      seen.add(job.id);
      if (!state.youtubeReady || state.youtubeSeen.has(job.id)) return;
      if (job.status === "done") {
        toast(`Added to the library: ${job.label}`, "good");
        refreshMeta();
        pollStatus();
      } else if (job.status === "duplicate") {
        toast(`Already in the library: ${job.label}`, "good");
        refreshMeta();
      } else if (job.status === "partial") {
        toast(`Partly filed: ${job.label}`, "error");
        refreshMeta();
      } else if (job.status === "failed") {
        toast(`Import failed: ${job.label} — ask for “download status”.`,
              "error");
      }
    });
    state.youtubeSeen = seen;
    state.youtubeReady = true;
  }

  function openImport(prefillUrl = "") {
    if (!state.downloads) {
      toast("This build has no download module installed.", "error");
      return;
    }
    openModal("Import audio", () => {
      const form = document.createElement("form");
      const links = document.createElement("textarea");
      links.rows = prefillUrl.includes("\n") ? 4 : 2;
      links.placeholder =
        "https://example.com/audio\none link per line — several are queued together";
      links.value = prefillUrl;
      form.appendChild(field("Video links", links,
                             "(one per line; a whole list can be pasted)"));

      const preview = document.createElement("div");
      preview.className = "import-preview";
      preview.hidden = true;
      form.appendChild(preview);
      const previewBtn = document.createElement("button");
      previewBtn.type = "button";
      previewBtn.className = "btn small";
      previewBtn.textContent = "Check the first link";
      previewBtn.title = "Ask the module what this link is, before anything "
        + "downloads";
      previewBtn.onclick = async () => {
        const first = links.value.split(/\s+/).filter(Boolean)[0] || "";
        if (!first) {
          toast("Paste a link first.", "error");
          return;
        }
        previewBtn.disabled = true;
        previewBtn.textContent = "Checking…";
        try {
          const found = await api("/api/import/preview", { url: first });
          if (found.error) throw new Error(found.error);
          preview.hidden = false;
          preview.innerHTML = "";
          const title = document.createElement("strong");
          title.textContent = found.title || "(no title)";
          const line = document.createElement("span");
          const bits = [];
          if (found.channel) bits.push(found.channel);
          if (found.duration_s) {
            const total = Math.max(0, Math.round(Number(found.duration_s)));
            bits.push(`${Math.floor(total / 60)}:`
                      + `${String(total % 60).padStart(2, "0")}`);
          }
          if (found.artist) bits.push(`artist: ${found.artist}`);
          line.textContent = bits.join(" · ");
          preview.append(title, line);
          if (!artist.value.trim() && found.artist) {
            artist.value = found.artist;
          }
        } catch (err) {
          toast(err.message, "error");
        } finally {
          previewBtn.disabled = false;
          previewBtn.textContent = "Check the first link";
        }
      };
      form.appendChild(previewBtn);

      const row = document.createElement("div");
      row.className = "field-row";
      const artist = input("from the video's metadata");
      const album = input("optional — a new album folder");
      row.appendChild(field("Artist", artist, "(optional)"));
      row.appendChild(field("Album", album, "(optional)"));
      form.appendChild(row);
      const genre = input("optional — e.g. Soundtrack");
      form.appendChild(field("Genre", genre, "(optional tag)"));

      const format = document.createElement("select");
      [["best", "best available (keeps the source codec)"],
       ["mp3", "MP3"], ["m4a", "M4A / AAC"], ["opus", "Opus"], ["flac", "FLAC"]]
        .forEach(([value, label]) => {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = label;
          format.appendChild(option);
        });
      form.appendChild(field("Format", format));

      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn";
      cancel.textContent = "Cancel";
      cancel.onclick = closeModal;
      const submit = document.createElement("button");
      submit.type = "submit";
      submit.className = "btn primary";
      submit.textContent = "Download";
      actions.append(cancel, submit);
      form.appendChild(actions);

      const note = document.createElement("p");
      note.className = "note";
      note.innerHTML =
        "The audio is fetched by the installed download module, tagged " +
        "through the journal and filed under " +
        "<code>Artists/&lt;artist&gt;/&lt;album&gt;/</code>. " +
        "Several links are queued: one download runs, the rest wait in the " +
        "downloads panel, where you can reorder or cancel them. " +
        "You can also type <code>add &lt;url&gt;</code> in the chat.";
      form.appendChild(note);

      form.onsubmit = (event) => {
        event.preventDefault();
        submit.disabled = true;
        submit.textContent = "Starting…";
        startImport(links.value.trim(), artist.value.trim(),
                    album.value.trim(), format.value, genre.value.trim());
      };
      return form;
    });
  }

  async function startImport(url, artist, album, format, genre = "") {
    let result;
    try {
      result = await api("/api/import",
                         { url, artist, album, format, genre });
    } catch (err) {
      toast(err.message, "error");
      openImport(url);
      return;
    }
    const ids = result.ids || [result.id].filter(Boolean);
    if (result.message) toast(result.message, "good");
    if (ids.length > 1 || result.skipped && result.skipped.length) {
      openDownloads();
      return;
    }
    openJob(ids[0], url);
  }

  function openJob(id, url) {
    openModal("Importing audio", () => {
      const wrap = document.createElement("div");
      const head = document.createElement("div");
      head.className = "job-head";
      const spinner = document.createElement("div");
      spinner.className = "spinner";
      const label = document.createElement("span");
      label.textContent = "Starting the import…";
      head.append(spinner, label);
      wrap.appendChild(head);

      const where = document.createElement("p");
      where.className = "note";
      where.textContent = url;
      wrap.appendChild(where);

      const log = document.createElement("pre");
      log.className = "log";
      log.textContent = "waiting for output…";
      wrap.appendChild(log);

      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const background = document.createElement("button");
      background.type = "button";
      background.className = "btn";
      background.textContent = "Run in background";
      background.onclick = () => {
        toast("Download continues — I will tell you when it is done.");
        closeModal();
      };
      const done = document.createElement("button");
      done.type = "button";
      done.className = "btn primary";
      done.textContent = "Close";
      done.disabled = true;
      done.onclick = closeModal;
      actions.append(background, done);
      wrap.appendChild(actions);

      let firstLine = true;
      trackJob(id, (job) => {
        if (!wrap.isConnected) return;
        if (job.log && job.log.length) {
          log.textContent = job.log.join("\n");
          log.scrollTop = log.scrollHeight;
          if (firstLine) {
            firstLine = false;
            label.textContent = "Downloading and tagging…";
          }
        }
        if (job.status === "queued") {
          label.textContent = "Queued behind another import…";
        } else if (job.status === "duplicate") {
          spinner.remove();
          head.insertAdjacentHTML("afterbegin", icon("check", "ok-icon"));
          label.textContent = "Already in the library — nothing was added.";
          background.hidden = true;
          done.disabled = false;
          done.focus();
        } else if (job.status === "done" || job.status === "partial") {
          spinner.remove();
          head.insertAdjacentHTML("afterbegin", icon("check", "ok-icon"));
          label.textContent = job.status === "done"
            ? "Done — the file is in the library."
            : "Partly filed — some downloads failed, see the output.";
          background.hidden = true;
          done.disabled = false;
          done.focus();
        } else if (job.status === "failed" || job.status === "unknown") {
          spinner.remove();
          label.textContent = "Failed — see the output below.";
          background.hidden = true;
          done.disabled = false;
        }
      });
      return wrap;
    });
  }

  async function trackJob(id, onUpdate) {
    let last = null;
    for (let attempt = 0; attempt < 1800; attempt += 1) {
      let job;
      try {
        job = await api(`/api/import/${encodeURIComponent(id)}`);
      } catch (err) {
        toast(`Lost the import job: ${err.message}`, "error");
        return last;
      }
      last = job;
      try { onUpdate(job); } catch (_) { /* detached view */ }
      if (job.status !== "running" && job.status !== "queued") {
        if (job.status === "done") {
          toast("Imported — tagged and filed in the library.", "good");
          refreshMeta();
          pollStatus();
        } else if (job.status === "duplicate") {
          toast("Already in the library — the same recording is there.", "good");
          refreshMeta();
        } else if (job.status === "partial") {
          toast("Partly filed — some downloads failed.", "error");
          refreshMeta();
        } else {
          toast("The download failed — nothing was added to the library.",
                "error");
        }
        return job;
      }
      await sleep(1400);
    }
    return last;
  }

  // ----------------------------------------------------- downloads panel
  //
  // Clicking the footer's downloads chip opens this list: every import the
  // library remembers — running, queued, filed, already here and failed —
  // with the buttons that apply to each one.  The records live in
  // `library-work/app/chat/youtube/`, so a job stays listed after a restart.

  const DOWNLOAD_ORDER = { running: 0, queued: 1, failed: 2, partial: 3,
                           unknown: 4, cancelled: 5, duplicate: 6, done: 7 };

  const DOWNLOAD_KIND = {
    running: "run", queued: "run", done: "ok", duplicate: "here",
    partial: "part", failed: "bad", unknown: "bad", cancelled: "here",
  };

  function downloadCounts(jobs) {
    const counts = { total: jobs.length, running: 0, queued: 0, done: 0,
                     duplicate: 0, failed: 0, cancelled: 0 };
    jobs.forEach((job) => {
      const status = job.status || "unknown";
      if (status === "running") counts.running += 1;
      else if (status === "queued") counts.queued += 1;
      else if (status === "done") counts.done += 1;
      else if (status === "duplicate") counts.duplicate += 1;
      else if (status === "cancelled") counts.cancelled += 1;
      else counts.failed += 1;
    });
    return counts;
  }

  function downloadSummary(job) {
    if (job.duplicate) return `already in the library: ${job.duplicate}`;
    if (job.filed) return `filed as ${job.filed}`;
    if (job.status === "running") return "downloading and tagging…";
    if (job.status === "queued") {
      return job.position
        ? `waiting — position ${job.position} of ${job.queued} in the queue`
        : "waiting behind the running import";
    }
    if (job.status === "cancelled") {
      return job.summary || "cancelled before it started";
    }
    return job.summary || "";
  }

  function downloadRow(job, reload) {
    const row = document.createElement("div");
    row.className = "download-row";
    row.dataset.kind = DOWNLOAD_KIND[job.status] || "bad";

    const badge = document.createElement("span");
    badge.className = "download-state";
    badge.textContent = job.state || job.status || "unknown";
    row.appendChild(badge);

    const main = document.createElement("div");
    main.className = "download-main";
    const title = document.createElement("div");
    title.className = "download-title";
    title.textContent = job.label || job.url || "download";
    main.appendChild(title);

    const sub = document.createElement("div");
    sub.className = "download-sub";
    const bits = [];
    if (job.artist) bits.push(job.artist);
    if (job.channel && job.channel !== job.artist) bits.push(job.channel);
    if (job.ago) bits.push(job.ago);
    if (job.retries) {
      bits.push(`${job.retries} earlier attempt${job.retries === 1 ? "" : "s"}`);
    }
    sub.textContent = bits.join(" · ");
    main.appendChild(sub);

    const note = downloadSummary(job);
    if (note) {
      const line = document.createElement("div");
      line.className = "download-note";
      line.textContent = note;
      main.appendChild(line);
    }

    const tags = [job.artist, job.title, job.album,
                  job.genre ? `genre: ${job.genre}` : "",
                  job.track ? `track ${job.track}` : ""].filter(Boolean);
    if (tags.length) {
      const line = document.createElement("div");
      line.className = "download-tags";
      line.textContent = tags.join(" · ");
      main.appendChild(line);
    }
    if (job.integrity) {
      const line = document.createElement("div");
      line.className = "download-integrity";
      line.textContent = `⚠ the audio may be damaged: ${job.integrity}`;
      main.appendChild(line);
    }
    row.appendChild(main);

    const actions = document.createElement("div");
    actions.className = "download-actions";
    const target = job.filed || job.duplicate;
    if (target) {
      const playBtn = document.createElement("button");
      playBtn.type = "button";
      playBtn.className = "btn small";
      playBtn.textContent = "Play";
      playBtn.title = `Play ${target}`;
      playBtn.onclick = () => { closeModal(); play(target); };
      actions.appendChild(playBtn);
    }
    const tagsBtn = document.createElement("button");
    tagsBtn.type = "button";
    tagsBtn.className = "btn small";
    tagsBtn.textContent = "Tags";
    tagsBtn.title = "Change artist, title, album, genre or track number";
    tagsBtn.onclick = () => openDownloadTags(job);
    actions.appendChild(tagsBtn);
    const logBtn = document.createElement("button");
    logBtn.type = "button";
    logBtn.className = "btn small";
    logBtn.textContent = "Log";
    logBtn.title = "Show the importer's output";
    logBtn.onclick = () => openJobLog(job);
    actions.appendChild(logBtn);
    if (job.status === "queued") {
      const upBtn = document.createElement("button");
      upBtn.type = "button";
      upBtn.className = "btn small";
      upBtn.textContent = "↑";
      upBtn.title = "Move this import up the queue";
      upBtn.onclick = async () => {
        upBtn.disabled = true;
        await workAction("downloads", "up", job.id);
        reload();
      };
      const downBtn = document.createElement("button");
      downBtn.type = "button";
      downBtn.className = "btn small";
      downBtn.textContent = "↓";
      downBtn.title = "Move this import down the queue";
      downBtn.onclick = async () => {
        downBtn.disabled = true;
        await workAction("downloads", "down", job.id);
        reload();
      };
      actions.append(upBtn, downBtn);
    }
    if (job.cancellable) {
      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "btn small";
      cancelBtn.textContent = "Cancel";
      cancelBtn.title = job.status === "queued"
        ? "Drop this import from the queue (nothing has been downloaded)"
        : "Stop the download; its log and record stay";
      let armed = false;
      cancelBtn.onclick = async () => {
        if (!armed) {
          armed = true;
          cancelBtn.textContent = "Really cancel?";
          cancelBtn.classList.add("danger");
          return;
        }
        cancelBtn.disabled = true;
        await workAction("downloads", "cancel", job.id);
        reload();
      };
      actions.appendChild(cancelBtn);
    }
    if (job.retry) {
      const retryBtn = document.createElement("button");
      retryBtn.type = "button";
      retryBtn.className = "btn small primary";
      retryBtn.textContent = "Retry";
      retryBtn.onclick = async () => {
        retryBtn.disabled = true;
        await workAction("downloads", "retry", job.id);
        reload();
      };
      actions.appendChild(retryBtn);
    }
    if (job.dismissible) {
      const clearBtn = document.createElement("button");
      clearBtn.type = "button";
      clearBtn.className = "btn small";
      clearBtn.textContent = "Clear";
      clearBtn.title = "Hide this failure from the footer "
        + "(the job and its log stay in the queue)";
      clearBtn.onclick = async () => {
        clearBtn.disabled = true;
        await workAction("downloads", "dismiss", job.id);
        reload();
      };
      actions.appendChild(clearBtn);
    }
    if (job.status !== "running") {
      // deleting queue state is the listener's decision: the track itself
      // stays in the library, only the record and its logs go away
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn small";
      delBtn.textContent = "Delete";
      delBtn.title = "Remove this entry from the download queue "
        + "(the imported track stays in the library)";
      let armed = false;
      delBtn.onclick = async () => {
        if (!armed) {
          armed = true;
          delBtn.textContent = "Really delete?";
          delBtn.classList.add("danger");
          return;
        }
        delBtn.disabled = true;
        try {
          const result = await api("/api/downloads/forget", { id: job.id });
          toast(`Removed “${job.label}” from the queue.`, "good");
          reload();
          return result;
        } catch (err) {
          const detail = (err && err.detail) || {};
          toast(detail.note || detail.error || err.message, "error");
          delBtn.disabled = false;
          armed = false;
          delBtn.textContent = "Delete";
          delBtn.classList.remove("danger");
        }
      };
      actions.appendChild(delBtn);
    }
    row.appendChild(actions);
    return row;
  }

  function openDownloadTags(job) {
    openModal(`Tags · ${job.label || "import"}`, () => {
      const form = document.createElement("form");
      const note = document.createElement("p");
      note.className = "note";
      note.textContent = (job.filed || job.duplicate)
        ? "These values are written to the filed audio file through the "
          + "journalled tag writer, so the change can be rolled back. "
          + "The file stays where it is."
        : "The queue keeps these values and the importer tags the file "
          + "with them when it runs.";
      form.appendChild(note);

      const title = input("the video's title", job.title || "");
      const artist = input("the performer", job.artist || "");
      const album = input("optional — the album folder", job.album || "");
      const genre = input("optional — e.g. Soundtrack", job.genre || "");
      const track = input("optional — track number", job.track || "");
      form.appendChild(field("Title", title));
      form.appendChild(field("Artist", artist));
      form.appendChild(field("Album", album));
      form.appendChild(field("Genre", genre));
      form.appendChild(field("Track", track, "(number)"));

      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn";
      cancel.textContent = "Cancel";
      cancel.onclick = openDownloads;
      const save = document.createElement("button");
      save.type = "submit";
      save.className = "btn primary";
      save.textContent = "Save tags";
      actions.append(cancel, save);
      form.appendChild(actions);

      form.onsubmit = async (event) => {
        event.preventDefault();
        save.disabled = true;
        save.textContent = "Saving…";
        try {
          const result = await api("/api/downloads/tags", {
            id: job.id,
            title: title.value.trim(),
            artist: artist.value.trim(),
            album: album.value.trim(),
            genre: genre.value.trim(),
            track: track.value.trim(),
          }, { timeout: 120 });
          toast(result.note || "Tags saved.", "good");
          openDownloads();
          if (result.written) {
            refreshMeta();
            pollStatus();
          }
        } catch (err) {
          const detail = (err && err.detail) || {};
          toast(detail.note || detail.error || err.message, "error");
          save.disabled = false;
          save.textContent = "Save tags";
        }
      };
      return form;
    });
  }

  async function openJobLog(job) {
    let detail = null;
    try {
      detail = await api(`/api/import/${encodeURIComponent(job.id)}`);
    } catch (err) {
      toast(`Could not read the log: ${err.message}`, "error");
      return;
    }
    openModal(`Log · ${job.label || "import"}`, () => {
      const wrap = document.createElement("div");
      const head = document.createElement("p");
      head.className = "note";
      head.textContent = [job.state, job.ago, job.url]
        .filter(Boolean).join(" · ");
      const log = document.createElement("pre");
      log.className = "log";
      const lines = (detail && detail.log) || [];
      log.textContent = lines.length ? lines.join("\n") : "No output recorded.";
      wrap.append(head, log);
      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const back = document.createElement("button");
      back.type = "button";
      back.className = "btn";
      back.textContent = "Back to downloads";
      back.onclick = openDownloads;
      const done = document.createElement("button");
      done.type = "button";
      done.className = "btn primary";
      done.textContent = "Close";
      done.onclick = closeModal;
      actions.append(back, done);
      wrap.appendChild(actions);
      return wrap;
    }, { wide: true });
  }

  function openDownloads() {
    if (!state.downloads) {
      toast("This build has no download module installed.", "error");
      return;
    }
    openModal("Downloads", () => {
      const wrap = document.createElement("div");

      const head = document.createElement("div");
      head.className = "modal-actions downloads-head";
      const counts = document.createElement("span");
      counts.className = "note downloads-counts";
      counts.textContent = "Loading the download queue…";
      const refresh = document.createElement("button");
      refresh.type = "button";
      refresh.className = "btn small";
      refresh.textContent = "Refresh";
      refresh.onclick = () => load();
      head.append(counts, refresh);

      const body = document.createElement("div");
      body.className = "downloads-list";
      wrap.append(head, body);

      let timer = null;
      const load = async () => {
        if (timer) { clearTimeout(timer); timer = null; }
        let data = null;
        try {
          data = await api("/api/youtube");
        } catch (err) {
          body.innerHTML = "";
          const failed = document.createElement("p");
          failed.className = "note";
          failed.textContent = `Could not read the download queue: ${err.message}`;
          body.appendChild(failed);
          return;
        }
        const jobs = ((data && data.jobs) || []).slice();
        jobs.sort((a, b) => {
          const byStatus = (DOWNLOAD_ORDER[a.status] ?? 9)
            - (DOWNLOAD_ORDER[b.status] ?? 9);
          if (byStatus) return byStatus;
          // the waiting jobs keep the order they will really start in
          return (a.position || 0) - (b.position || 0);
        });
        const tally = downloadCounts(jobs);
        const parts = [`${tally.total} import${tally.total === 1 ? "" : "s"}`];
        if (tally.running) parts.push(`${tally.running} running`);
        if (tally.queued) parts.push(`${tally.queued} queued`);
        if (tally.done) parts.push(`${tally.done} filed`);
        if (tally.duplicate) parts.push(`${tally.duplicate} already here`);
        if (tally.failed) parts.push(`${tally.failed} failed`);
        if (tally.cancelled) parts.push(`${tally.cancelled} cancelled`);
        if (data && data.failed && !tally.failed) {
          parts.push(`${data.failed} need attention`);
        }
        counts.textContent = parts.join(" · ");

        body.innerHTML = "";
        if (!jobs.length) {
          const empty = document.createElement("p");
          empty.className = "note";
          empty.textContent =
            "No downloads yet. Say “search for …” and then " +
            "“download 1”, or use the import dialog.";
          body.appendChild(empty);
          return;
        }
        jobs.forEach((job) => body.appendChild(downloadRow(job, load)));

        // a running or queued import changes by itself; keep the list live
        if (jobs.some((job) => job.status === "running"
                              || job.status === "queued")) {
          timer = setTimeout(() => {
            if (wrap.isConnected) load();
          }, 2500);
        }
      };
      load();
      return wrap;
    }, { wide: true });
  }

  // --------------------------------------------------------------- actions

  function autoGrow() {
    els.prompt.style.height = "auto";
    els.prompt.style.height = `${Math.min(168, els.prompt.scrollHeight)}px`;
  }

  function clearConversation(announce = true) {
    els.transcript.innerHTML = "";
    els.scrollBottom.hidden = true;
    if (state.meta && state.meta.welcome_html) {
      addAssistant(state.meta.welcome_html);
    }
    if (announce) toast("Conversation cleared.");
    els.prompt.focus();
  }

  async function play(target) {
    try {
      const reply = await api("/api/play", { target });
      addReply(reply);
      pollStatus();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function silentCommand(message) {
    try {
      const reply = await api("/api/chat", { message });
      toast(plainText(reply.html) || "Done.");
      refreshMeta();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function togglePlanner() {
    const enabled = Boolean(state.meta && state.meta.planner && state.meta.planner.enabled);
    silentCommand(enabled ? "/llm off" : "/llm on");
  }

  function seekFromEvent(event) {
    const status = state.status;
    if (!status || !status.playing || !status.duration) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const target = fraction * status.duration;
    const delta = Math.round(target - status.position);
    if (Math.abs(delta) >= 2) transport("seek", delta);
  }

  // -------------------------------------------------------------- listeners

  els.composer.addEventListener("submit", (event) => {
    event.preventDefault();
    send(els.prompt.value);
  });

  els.prompt.addEventListener("input", autoGrow);
  els.prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send(els.prompt.value);
    }
  });

  document.addEventListener("click", (event) => {
    const playTarget = event.target.closest("[data-play]");
    if (playTarget) {
      play(playTarget.dataset.play);
      return;
    }
    // a track named in an answer: its whole line carries the path, so the
    // title and the path both start it
    const listed = event.target.closest("[data-rel]");
    if (listed) {
      const rel = listed.dataset.rel;
      play(rel);
      toast(`Playing ${rel.split("/").pop()}.`);
      return;
    }
    const chip = event.target.closest("[data-send]");
    if (chip) {
      send(chip.dataset.send);
      return;
    }
    const playlist = event.target.closest("[data-playlist]");
    if (playlist) {
      play(playlist.dataset.playlist);
      toast(`Playing ${playlist.dataset.playlist}.`);
      return;
    }
    const transportBtn = event.target.closest("[data-action]");
    if (transportBtn) {
      transport(transportBtn.dataset.action);
      return;
    }
    const feedback = event.target.closest("[data-feedback]");
    if (feedback) {
      api("/api/feedback", { kind: feedback.dataset.feedback })
        .then((reply) => {
          if (reply.html) addReply(reply);
          toast(feedback.dataset.feedback === "like" ? "Noted — more like this."
                                                     : "Noted — less like this.");
        })
        .catch((err) => toast(err.message, "error"));
    }
  });

  els.transcript.addEventListener("scroll", () => {
    if (nearBottom()) els.scrollBottom.hidden = true;
  });

  els.scrollBottom.addEventListener("click", () => scrollToBottom());

  els.npProgress = $("#np-progress");
  els.npProgress.addEventListener("click", seekFromEvent);

  els.volume.addEventListener("input", () => {
    els.volumeLabel.textContent = els.volume.value;
  });
  let volumeTimer = null;
  els.volume.addEventListener("change", () => {
    clearTimeout(volumeTimer);
    volumeTimer = setTimeout(() => transport("volume", Number(els.volume.value)), 120);
  });

  $("#btn-search").addEventListener("click", () => openSearch());
  if (els.btnVoice) {
    els.btnVoice.hidden = true;
    els.btnVoice.addEventListener("click", () => toggleRecording());
  }
  $("#btn-psycho").addEventListener("click", () => openPsycho());
  if (els.npTags) {
    els.npTags.addEventListener("click", () => {
      const rel = state.status && state.status.rel ? state.status.rel : "";
      openTags(rel);
    });
  }
  if (els.npCover) {
    els.npCover.classList.add("clickable");
    els.npCover.title = "Show the sound properties of this track";
    els.npCover.addEventListener("click", () => {
      const rel = state.status && state.status.rel ? state.status.rel : "";
      openPsycho(rel);
    });
  }
  if (els.vizStyle) {
    els.vizStyle.addEventListener("click", () => cycleVizStyle());
  }
  if (els.npFacts) {
    els.npFacts.disabled = true;
    els.npFacts.addEventListener("click", () => {
      const rel = state.status && state.status.rel ? state.status.rel : "";
      openPsycho(rel);
    });
  }
  if (els.radioToggle) {
    els.radioToggle.addEventListener("click", async () => {
      const on = els.radioToggle.classList.contains("on");
      els.radioToggle.disabled = true;
      try {
        const answer = await api("/api/radio",
                                 { action: on ? "stop" : "start" });
        if (answer.error) throw new Error(answer.error);
        toast(on ? "Radio off" : "Radio on — it keeps the queue topped up");
        await refreshMeta();
        setTimeout(pollStatus, 250);
      } catch (err) {
        toast(err.message, "error");
      } finally {
        els.radioToggle.disabled = false;
      }
    });
  }
  if (els.endlessToggle) {
    els.endlessToggle.addEventListener("click", async () => {
      const on = els.endlessToggle.classList.contains("on");
      els.endlessToggle.disabled = true;
      try {
        const answer = await api("/api/endless",
                                 { action: on ? "stop" : "start" });
        if (answer.error) throw new Error(answer.error);
        toast(on
          ? "Endless mode off"
          : "Endless on — it grows from the song that just played");
        await refreshMeta();
        setTimeout(pollStatus, 250);
      } catch (err) {
        toast(err.message, "error");
      } finally {
        els.endlessToggle.disabled = false;
      }
    });
  }
  if (els.shuffleToggle) {
    els.shuffleToggle.addEventListener("click", async () => {
      const on = els.shuffleToggle.classList.contains("on");
      els.shuffleToggle.disabled = true;
      try {
        const answer = await api("/api/shuffle", { on: !on });
        if (answer.error) throw new Error(answer.error);
        applyShuffle(answer.on);
        toast(answer.on
          ? "Shuffle on — the next playlist starts in random order"
          : "Shuffle off");
        setTimeout(pollStatus, 250);
      } catch (err) {
        toast(err.message, "error");
      } finally {
        els.shuffleToggle.disabled = false;
      }
    });
  }
  $("#btn-import").addEventListener("click", () => openImport());
  $("#btn-activity").addEventListener("click", openActivity);
  $("#btn-clear").addEventListener("click", () => clearConversation());
  $("#new-playlist").addEventListener("click", () => {
    els.prompt.value = "create a playlist for ";
    autoGrow();
    els.prompt.focus();
  });
  $("#reload-library").addEventListener("click", async () => {
    await refreshMeta();
    toast("Library re-read.");
  });
  if (els.indexChip) {
    els.indexChip.addEventListener("click", () => openSettings());
  }
  $("#btn-skills").addEventListener("click", openSkills);
  $("#btn-settings").addEventListener("click", () => openSettings());
  if (els.vizToggle) els.vizToggle.addEventListener("click", toggleVisualizer);
  if (els.work) {
    els.work.addEventListener("click", (event) => {
      const button = event.target.closest("[data-work]");
      const chip = event.target.closest(".work-chip");
      if (!button) {
        // a plain click on the downloads chip opens the whole queue
        if (chip && chip.dataset.job === "downloads") openDownloads();
        return;
      }
      const job = chip ? chip.dataset.job : "";
      const id = chip ? (chip.dataset.jobId || "") : "";
      if (job) workAction(job, button.dataset.work, id);
    });
    els.work.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const chip = event.target.closest(".work-chip-link");
      if (!chip || event.target !== chip) return;
      event.preventDefault();
      openDownloads();
    });
  }
  if (els.lyricsToggle) {
    els.lyricsToggle.addEventListener("click", toggleLyrics);
  }
  if (els.lyricsEarlier) {
    els.lyricsEarlier.addEventListener("click", () => shiftLyrics(-0.5));
  }
  if (els.lyricsLater) {
    els.lyricsLater.addEventListener("click", () => shiftLyrics(0.5));
  }
  if (els.lyricsExport) {
    els.lyricsExport.addEventListener("click", () => exportLyrics());
  }
  if (els.queueToggle) {
    els.queueToggle.addEventListener("click", () => applyQueue(!state.queueOn));
  }
  if (els.queueRefresh) {
    els.queueRefresh.addEventListener("click", () => {
      state.queueKey = "";
      refreshQueue();
    });
  }
  wireOwnBar();
  if (els.resumePlay) {
    els.resumePlay.addEventListener("click", resumeSession);
  }
  if (els.resumeForget) {
    els.resumeForget.addEventListener("click", forgetSession);
  }
  if (els.lyricsTranscribe) {
    els.lyricsTranscribe.addEventListener("click", () => transcribeLyrics(false));
  }
  if (els.lyricsNext) {
    els.lyricsNext.addEventListener("click", () => transcribeLyrics(true));
  }

  window.addEventListener("resize", () => {
    if (state.vizEnabled) drawWaveform();
  });

  els.plannerToggle.addEventListener("click", togglePlanner);

  $("#quit-btn").addEventListener("click", () => {
    openModal("Stop JulAIn Music Player?", (close) => {
      const wrap = document.createElement("div");
      const note = document.createElement("p");
      note.className = "note";
      note.textContent =
        "The local server stops and the window can be closed. Playlists, tags " +
        "and everything the chat learned are already saved.";
      wrap.appendChild(note);
      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const cancel = document.createElement("button");
      cancel.className = "btn";
      cancel.textContent = "Keep running";
      cancel.onclick = close;
      const stop = document.createElement("button");
      stop.className = "btn danger";
      stop.textContent = "Stop the server";
      stop.onclick = async () => {
        stop.disabled = true;
        state.quitting = true;          // a dying server is not a fault
        try { await api("/api/quit", {}); } catch (_) { /* it may vanish */ }
        closeModal();
        els.statusLine.textContent = "server stopped";
        setConnBanner(null);
        els.npTitle.textContent = "Stopped";
        els.npArtist.textContent = "You can close this window.";
        toast("Server stopped.");
        setTimeout(() => window.close(), 400);
      };
      actions.append(cancel, stop);
      wrap.appendChild(actions);
      return wrap;
    });
  });

  $("#modal-close").addEventListener("click", closeModal);
  els.modal.addEventListener("click", (event) => {
    if (event.target === els.modal) closeModal();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.modal.hidden) {
      closeModal();
      return;
    }
    if (event.key.toLowerCase() === "k" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      els.prompt.focus();
      return;
    }
    if (event.key.toLowerCase() === "f" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      openSearch();
      return;
    }
    if (event.key.toLowerCase() === "l" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      clearConversation();
      return;
    }
    if (event.key.toLowerCase() === "v" && event.ctrlKey && event.shiftKey) {
      event.preventDefault();
      toggleRecording();
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollStatus();
  });

  // ------------------------------------------------------------------ start

  // The window is also a small web app: a home-screen icon opens it through
  // /manifest.webmanifest, and this worker keeps the shell (CSS, script, icon)
  // available when the server is briefly busy.  It never caches the page or an
  // API answer — those carry the token and live library data.
  const registerWorker = () => {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  };

  (async function boot() {
    registerWorker();
    let blocked = false;
    try {
      const data = await api("/api/state");
      renderMeta(data);
      if (data.welcome_html) addAssistant(data.welcome_html);
      els.statusLine.textContent = "ready — ask for something to play";
      els.prompt.focus();
      const params = new URLSearchParams(location.search);
      if (params.has("profile")) openPsycho();
      if (params.has("settings")) openSettings();
    } catch (err) {
      blocked = /forbidden/i.test(err.message || "");
      addAssistant(blocked
        ? "<p>The app was restarted, so this window's access token is out of date. "
          + "Open it from the launcher (or run <code>music-cli chat --web</code>), "
          + "then add it to the home screen again.</p>"
        : `<p>Could not reach the library: <code>${escapeHtml(err.message)}</code></p>`,
        { error: true });
    }
    if (blocked) return;
    pollStatus();
    pollYoutube();
    refreshWork();
    setInterval(pollStatus, 2000);
    setInterval(pollYoutube, 6000);
  })();
})();
