// Grabs frames off the <video> element while it plays, sends each one to the
// backend (which forwards it to Jev as a Box + Noul bundle), and draws the
// boxes that come back on the panel beside the video.

const $ = (id) => document.getElementById(id);

const video = $("video");
const detections = $("detections");
const dctx = detections.getContext("2d");

// Jev's image limits: 256-1568 px per side, 78,400-2,458,624 total pixels.
const LIMITS = { maxSide: 1568, minSide: 256, minPix: 78400, maxPix: 2458624 };

// $42 per billion input tokens.
const USD_PER_TOKEN = 42 / 1e9;

const COLORS = { ship: "#4cc9f0", person: "#ffd166", character: "crimson" };

// A box that resolved to a named character is drawn green, so an identified
// face is distinguishable from an anonymous person at a glance.
const CHARACTER_COLOR = "#51e5a8";

// Boxes that came from a per-character question rather than a region are drawn
// crimson, so the two ways of finding a character stay visually separate.
const PER_CHARACTER_COLOR = "crimson";

// A box picked from the list is drawn orange, which belongs to no kind, so it
// stands out against cyan, yellow, green and crimson alike.
const PICKED_COLOR = "#ff9f1c";

// Bands overlap, and one big ship can answer two of them. Boxes that cover the
// same thing collapse into the most confident one.
const IOU_MAX = 0.5;

// Presence and box confidence answer different questions. The Noul gate says
// whether the target is there; the Box's own confidence says how sure it is of
// the exact edges, and runs low on small or soft targets even when the find is
// right. So a low-confidence box is drawn dashed, not thrown away.
const SURE = 0.35;

// Below this the character pick is too close to call, so the box keeps its
// band label rather than asserting a name.
const NAME_MIN = 0.4;

const state = {
  cfg: null,
  on: true,
  show: { ship: true, person: true, named: true, character: true },
  castLoading: false,
  castSkipped: false,
  selected: null,
  selectedFrom: null,
  videoName: "",
  serverSide: false,
  url: null,
  isYt: false,
  transcoding: false,
  inFlight: false,
  lastSentAt: 0,
  minGap: 250,
  presenceMin: 0.6,
  boxMin: 0.25,
  slots: { ship: 10, person: 10 },
  last: null,
  view: [],
  stats: { requests: 0, errors: 0, tokensIn: 0, tokensOut: 0 },
};

const grab = document.createElement("canvas");
const gctx = grab.getContext("2d", { willReadFrequently: false });

// ---------------------------------------------------------------- setup

async function boot() {
  const res = await fetch("/api/config");
  state.cfg = await res.json();

  startBuildStamp();
  $("model-name").textContent = state.cfg.model;
  $("video-name").textContent = state.cfg.video_name || "no video";

  // Slot counts come from the backend and are not exposed in the page; change
  // them with --max-ships / --max-people.
  for (const kind of ["ship", "person"]) {
    state.slots[kind] = state.cfg.max_slots[kind] ?? 10;
  }

  if (!state.cfg.has_key) {
    showError({ detail: "No JEV_API_KEY found. Put it in .env next to app.py." });
  }

  showCast(state.cfg.cast_source);

  state.videoName = state.cfg.video_name || "";
  if (state.cfg.video_url) {
    state.serverSide = true;
    loadSource(state.cfg.video_url);
    syncCast(state.cfg.video_name || "");
  }

  wireControls();
  startEventLog();
  requestAnimationFrame(tick);
}

let objectUrl = null;

function playFile(file) {
  logEvent(`Opened ${file.name}`, {
    megabytes: +(file.size / 1048576).toFixed(1),
    type: file.type || "unknown",
  });
  state.serverSide = false;
  state.url = null;
  state.isYt = false;
  state.transcoding = false;
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = URL.createObjectURL(file);
  loadSource(objectUrl, file.name);
  // The cast is per-show, so a new file needs a new one.
  syncCast(file.name);
}

// Opening a URL plays it through the backend so the frames the page captures
// stay same-origin (a cross-origin video taints the canvas and detection would
// fail on most hosts). The backend proxies the bytes and forwards Range.
function isYtHost(hostname) {
  return /(^|\.)(youtube\.com|youtu\.be)$/i.test(hostname);
}

// iOS Safari plays fragmented MP4 only through MSE, so the same sources are
// served as HLS there; desktop keeps the fragmented-MP4 stream. iPadOS 13+
// reports as MacIntel, hence the touch-point check.
function isIOS() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function playUrl(rawUrl) {
  const trimmed = rawUrl.trim();
  if (!trimmed) return;

  // Assume http when no scheme was typed; a pasted https:// URL is left alone.
  const candidate = /^https?:\/\//i.test(trimmed) ? trimmed : `http://${trimmed}`;
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch {
    showError({ detail: "That is not a valid URL." });
    return;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    showError({ detail: "Only http(s) URLs are supported." });
    return;
  }

  const isYt = isYtHost(parsed.hostname);
  logEvent(`Opened URL ${parsed.href}`, {});
  state.serverSide = true;
  state.url = parsed.href;
  state.isYt = isYt;
  state.transcoding = false;
  if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }

  if (isYt) {
    // A watch URL has no filename to identify a show from, and the stream is
    // already merged for the browser, so there is no cast lookup or transcode
    // fallback — clear any stale cast and just play the resolved stream.
    const name = parsed.searchParams.get("v")
      || parsed.pathname.split("/").filter(Boolean).pop()
      || parsed.hostname;
    clearCast();
    loadSource(
      isIOS()
        ? `/hls?yt=${encodeURIComponent(parsed.href)}`
        : `/yt?url=${encodeURIComponent(parsed.href)}`,
      name,
    );
    return;
  }

  const name = decodeURIComponent(
    parsed.pathname.split("/").filter(Boolean).pop() || parsed.hostname,
  );
  loadSource(`/remote?url=${encodeURIComponent(parsed.href)}`, name);
  syncCast(name);
}

// The cast decides which questions get asked, so playing before it lands would
// detect against the previous show's characters.
let gateTimer = null;

function setCastLoading(loading) {
  state.castLoading = loading;
  // Controls stay visible: hiding them made a slow lookup look like a video
  // that would not play.
  $("cast-gate").hidden = !loading || state.castSkipped;

  // Detection would otherwise go out against the previous show's characters.
  setDetectionBlocked(loading && !state.castSkipped);

  clearInterval(gateTimer);
  if (loading) {
    state.castSkipped = false;
    video.pause();
    const started = performance.now();
    const tick = () => {
      const seconds = Math.round((performance.now() - started) / 1000);
      $("gate-text").textContent = `Loading cast\u2026 ${seconds}s`;
    };
    tick();
    gateTimer = setInterval(tick, 1000);
  }
}

// An escape hatch: the cast only decides which questions get asked, so playing
// early is a real choice, not a broken state.
function setDetectionBlocked(blocked) {
  $("detect-once").disabled = blocked;
  $("detect-on").disabled = blocked;
}

function skipGate() {
  logEvent("Play anyway — playing before the cast finished loading", null, "warn");
  state.castSkipped = true;
  $("cast-gate").hidden = true;
  // Detecting now uses the cast still in memory; the snapshot records which.
  setDetectionBlocked(false);
  video.play().catch(() => {});
}

function showCast(source, busy, failed) {
  const node = $("cast-info");
  node.classList.toggle("stale", Boolean(failed));
  if (busy) {
    node.textContent = "cast: looking up\u2026";
  } else if (failed) {
    // The cast is cleared rather than kept: another show's characters on this
    // video is worse than none.
    node.textContent = `cast: ${source || "none"}`;
  } else {
    node.textContent = `cast: ${source || "built-in"}`;
  }
}

// Ask the backend to point the cast at this video. It answers "unchanged" when
// the show already matches, so re-opening the same file costs nothing.
async function syncCast(name) {
  if (!name) return;
  showCast(null, true);
  setCastLoading(true);
  try {
    const body = new FormData();
    body.append("name", name);
    const response = await fetch("/api/cast", { method: "POST", body });
    const data = await response.json();

    const failed = data.status === "failed" || data.status === "skipped";
    if (data.characters) state.cfg.cast = data.characters;
    state.cfg.cast_source = data.source;
    showCast(data.source, false, failed);

    if (failed) {
      showError({ detail: `Cast lookup: ${data.detail}` });
    }
  } catch (error) {
    showCast(state.cfg.cast_source);
  } finally {
    setCastLoading(false);
  }
}

// A watch URL has no filename to identify a show from, so there is no cast to
// look up: forget whatever the previous video left behind.
async function clearCast() {
  state.cfg.cast = {};
  state.cfg.cast_source = null;
  showCast("none");
  try {
    await fetch("/api/cast/clear", { method: "POST" });
  } catch {}
}

function loadSource(url, name) {
  if (name) state.videoName = name;
  $("empty-video").hidden = true;
  video.src = url;
  video.load();
  if (name) $("video-name").textContent = name;
}

function showVideoError(message) {
  const node = $("empty-video");
  node.hidden = false;
  node.innerHTML = `<b>${message}</b>
    <span class="hint">Convert it first, then open the result:</span>
    <code>ffmpeg -i in.mkv -c:v libx264 -c:a aac -movflags +faststart out.mp4</code>`;
}

function wireControls() {
  $("file").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    // Clear it so picking the same file twice still fires a change.
    event.target.value = "";
    if (file) playFile(file);
  });

  const openUrl = () => playUrl($("url").value);
  $("open-url").addEventListener("click", openUrl);
  $("url").addEventListener("keydown", (event) => {
    if (event.key === "Enter") openUrl();
  });

  // The picker's filter hides containers macOS doesn't map to a video type,
  // so a drop is the way in for anything it greys out.
  const stage = $("stage-video");
  for (const name of ["dragenter", "dragover"]) {
    stage.addEventListener(name, (event) => {
      event.preventDefault();
      stage.classList.add("dropping");
    });
  }
  for (const name of ["dragleave", "drop"]) {
    stage.addEventListener(name, () => stage.classList.remove("dropping"));
  }
  stage.addEventListener("drop", (event) => {
    event.preventDefault();
    const file = event.dataTransfer?.files?.[0];
    if (file) playFile(file);
  });

  // A file the browser can't decode otherwise fails to a silent black panel.
  video.addEventListener("error", () => {
    // If the source is one the backend can reach, hand it to ffmpeg instead of
    // giving up: Matroska and AC-3 are common in rips and unplayable here.
    // A yt-dlp stream is already transcoded, so it skips this.
    if (!state.isYt && state.serverSide && state.cfg.can_transcode && !state.transcoding) {
      logEvent("Browser cannot decode this file; falling back to ffmpeg", null, "warn");
      state.transcoding = true;
      $("empty-video").hidden = false;
      $("empty-video").innerHTML =
        "<b>Transcoding\u2026</b><span class='hint'>Your browser can't play this file, "
        + "so it is being converted as it streams. Seeking is unavailable.</span>";
      video.src = isIOS()
        ? (state.url ? `/hls?url=${encodeURIComponent(state.url)}` : "/hls")
        : (state.url ? `/stream?url=${encodeURIComponent(state.url)}` : "/stream");
      video.load();
      video.play().catch(() => {});
      return;
    }

    const code = video.error?.code;
    const why = code === 4
      ? "Your browser can't play this file."
      : code === 3
        ? "This file is damaged, or uses a codec your browser can't decode."
        : "That file could not be loaded.";
    logEvent(`Cannot play ${state.videoName || "this file"}: ${why}`, null, "error");
    showVideoError(why);
  });

  $("detect-on").addEventListener("change", (e) => { state.on = e.target.checked; });
  $("detect-once").addEventListener("click", () => { if (!state.inFlight) sendFrame(); });


  $("snap-save").addEventListener("click", saveSnapshot);
  $("raw-save").addEventListener("click", saveResponse);

  // Visibility only: these filter what is drawn, never what is asked for.
  // Read the boxes rather than assuming they start ticked — a browser restores
  // checkbox state across a reload without firing change, which otherwise
  // leaves the chips disagreeing with what is on the canvas.
  for (const key of Object.keys(state.show)) {
    const box = $(`show-${key}`);
    state.show[key] = box.checked;
    box.addEventListener("change", (event) => {
      state.show[key] = event.target.checked;
      redraw();
    });
  }

  bindRange("interval", (v) => {
    state.minGap = v;
    $("interval-out").textContent = `${v} ms`;
  });

  bindRange("presence", (v) => {
    state.presenceMin = v;
    $("presence-out").textContent = v.toFixed(2);
    redraw();
  });

  bindRange("boxconf", (v) => {
    state.boxMin = v;
    $("boxconf-out").textContent = v.toFixed(2);
    redraw();
  });

  let lastReady = "";
  // HLS reports its real dimensions only after `loadedmetadata` (they read 0
  // there on iOS), and an inline `aspect-ratio: 0 / 0` collapses the stage to a
  // sliver. So the pane shape is applied only once both dimensions are known,
  // on whichever event fires with them first.
  function fitVideoStage() {
    const vw = video.videoWidth;
    const vh = video.videoHeight;
    if (!vw || !vh) return;
    $("stage-video").style.aspectRatio = `${vw} / ${vh}`;

    // Fires again on re-load of the same source; only say so once per file.
    const signature = `${state.videoName}|${vw}x${vh}|${video.duration}`;
    if (signature !== lastReady) {
      lastReady = signature;
      logEvent(
        `Playable: ${vw}x${vh}, ${fmtTime(video.duration)}`,
        { video: state.videoName || null },
      );
    }
  }
  video.addEventListener("loadedmetadata", fitVideoStage);
  video.addEventListener("loadeddata", fitVideoStage);
  video.addEventListener("resize", fitVideoStage);

  $("gate-skip").addEventListener("click", skipGate);

  // Delegated: the rows are rebuilt on every answer.
  $("boxes").addEventListener("click", (event) => {
    const row = event.target.closest("[data-uid]");
    if (!row) return;
    const uid = row.dataset.uid;
    const same = uid === state.selected && state.selectedFrom === "list";
    state.selected = same ? null : uid;
    state.selectedFrom = same ? null : "list";
    redraw();
  });

  detections.addEventListener("click", (event) => {
    const rect = detections.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    // The canvas is laid out at the frame's aspect ratio, so a fraction of
    // the element is the same fraction of the image the boxes are given in.
    selectAt(
      (event.clientX - rect.left) / rect.width,
      (event.clientY - rect.top) / rect.height,
    );
  });

  video.addEventListener("play", () => {
    if (state.castLoading && !state.castSkipped) video.pause();
  });

  // Once the transcode is actually running, stop covering it with the notice.
  video.addEventListener("playing", () => {
    if (state.transcoding) $("empty-video").hidden = true;
  });

  video.addEventListener("timeupdate", () => {
    $("time-note").textContent = `${fmtTime(video.currentTime)} / ${fmtTime(video.duration)}`;
  });
}

function bindRange(id, apply) {
  const input = $(id);
  const run = () => apply(Number(input.value));
  input.addEventListener("input", run);
  run();
}

// ---------------------------------------------------------------- frame pump

function tick() {
  const playing = state.on && (!state.castLoading || state.castSkipped)
    && !video.paused && !video.ended && video.readyState >= 2;
  if (playing && !state.inFlight && performance.now() - state.lastSentAt >= state.minGap) {
    sendFrame();
  }
  requestAnimationFrame(tick);
}

function fitDims(w, h) {
  let scale = Math.min(
    1,
    LIMITS.maxSide / Math.max(w, h),
    Math.sqrt(LIMITS.maxPix / (w * h)),
  );
  scale = Math.max(
    scale,
    LIMITS.minSide / Math.min(w, h),
    Math.sqrt(LIMITS.minPix / (w * h)),
  );
  const clamp = (n) => Math.min(LIMITS.maxSide, Math.max(LIMITS.minSide, Math.round(n)));
  return [clamp(w * scale), clamp(h * scale)];
}

function captureFrame() {
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  if (!vw || !vh) return null;

  const [w, h] = fitDims(vw, vh);
  grab.width = w;
  grab.height = h;
  gctx.drawImage(video, 0, 0, w, h);

  return new Promise((resolve) => grab.toBlob(resolve, "image/jpeg", 0.85));
}

async function sendFrame() {
  state.inFlight = true;
  state.lastSentAt = performance.now();
  setBusy(true);

  try {
    const blob = await captureFrame();
    if (!blob) return;

    const stamp = video.currentTime;
    const bitmap = await createImageBitmap(blob);

    const body = new FormData();
    body.append("frame", blob, "frame.jpg");
    body.append("model", state.cfg.model);
    body.append("max_ship", String(state.slots.ship));
    body.append("max_person", String(state.slots.person));
    body.append("timestamp", stamp.toFixed(3));
    body.append("video_name", state.videoName);

    const started = performance.now();
    const res = await fetch("/api/detect", { method: "POST", body });
    const roundTrip = Math.round(performance.now() - started);
    const data = await res.json();

    if (!res.ok) {
      state.stats.errors += 1;
      showError(data);
      return;
    }

    clearError();
    state.stats.requests += 1;
    state.stats.tokensIn += data.usage?.input_tokens || 0;
    state.stats.tokensOut += data.usage?.output_tokens || 0;
    state.last = { data, bitmap, stamp, roundTrip };

    redraw();
    renderReadout();
  } catch (error) {
    state.stats.errors += 1;
    showError({ detail: String(error) });
  } finally {
    state.inFlight = false;
    setBusy(false);
  }
}

// ---------------------------------------------------------------- drawing

// Did the cast Choice land clearly enough to assert a name?
function isNamed(item) {
  const id = item.identity;
  return Boolean(id && id.key !== "other" && id.probability >= NAME_MIN);
}

// Which coloured group a box belongs to, and so which toggle governs it.
function colorGroup(item) {
  if (item.kind === "character") return "character";
  if (isNamed(item)) return "named";
  return item.kind;
}

function boxColor(item) {
  if (item.kind === "character") return PER_CHARACTER_COLOR;
  if (isNamed(item)) return CHARACTER_COLOR;
  return COLORS[item.kind] || "#ffffff";
}

function shown(item) {
  return state.show[colorGroup(item)] !== false;
}

// Reading order for the list: crimson per-character boxes, then green named
// ones, then yellow anonymous people, with ships last.
const GROUP_ORDER = { character: 0, named: 1, person: 2, ship: 3 };

// The character name when the Choice is clear, otherwise the band label. The
// percentage follows whichever number the label is actually claiming.
function boxLabel(item) {
  if (isNamed(item)) {
    return `${item.identity.name} ${(item.identity.probability * 100).toFixed(0)}%`;
  }
  return `${item.label} ${(item.confidence * 100).toFixed(0)}%`;
}

function iou(a, b) {
  const w = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
  const h = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
  const overlap = w * h;
  const areaA = (a.right - a.left) * (a.bottom - a.top);
  const areaB = (b.right - b.left) * (b.bottom - b.top);
  const union = areaA + areaB - overlap;
  return union > 0 ? overlap / union : 0;
}

// Score every slot the backend returned: the gate Noul first, then the box's
// own confidence, then overlap against the boxes already kept.
function evaluate() {
  const scored = (state.last?.data.candidates || []).map(
    // uid ties a drawn box to its row in the list; kind+label is unique.
    (c) => ({ ...c, uid: `${c.kind}:${c.label}`, kept: false, reason: null }),
  );

  const passed = [];
  for (const item of scored) {
    if (item.presence < state.presenceMin) item.reason = "gated out";
    else if (item.confidence < state.boxMin) item.reason = "low confidence";
    else passed.push(item);
  }

  passed.sort((a, b) => b.confidence - a.confidence);

  const kept = [];
  for (const item of passed) {
    const clash = kept.find((k) => k.kind === item.kind && iou(k.box, item.box) >= IOU_MAX);
    if (clash) {
      item.reason = `same box as ${clash.label}`;
      continue;
    }
    item.kept = true;
    kept.push(item);
  }

  return scored;
}

function visibleBoxes() {
  return state.view.filter((item) => item.kept && shown(item));
}

// What gets painted: everything that passed, plus whatever is selected. A
// gated-out box is still worth seeing when you ask for it by name — clicking
// its row and getting nothing is indistinguishable from a broken row.
function paintedBoxes() {
  const painted = visibleBoxes();
  if (!state.selected) return painted;
  if (painted.some((item) => item.uid === state.selected)) return painted;

  const chosen = state.view.find((item) => item.uid === state.selected);
  return chosen ? [...painted, chosen] : painted;
}

function boxArea(box) {
  return Math.max(0, box.right - box.left) * Math.max(0, box.bottom - box.top);
}

// Pick the box under the point, smallest first: boxes nest, and the small one
// is the specific answer. Clicking past them all clears the selection.
function selectAt(x, y) {
  const hits = paintedBoxes().filter(
    (item) => x >= item.box.left && x <= item.box.right
      && y >= item.box.top && y <= item.box.bottom,
  );
  hits.sort((a, b) => boxArea(a.box) - boxArea(b.box));

  const picked = hits[0]?.uid ?? null;
  state.selected = picked === state.selected ? null : picked;
  state.selectedFrom = state.selected ? "canvas" : null;
  redraw();

  if (state.selected) {
    const row = $("boxes").querySelector(`[data-uid="${CSS.escape(state.selected)}"]`);
    if (row) row.scrollIntoView({ block: "nearest" });
  }
}

function redraw() {
  state.view = evaluate();
  drawDetections();
  renderBoxList();
}

function drawDetections() {
  const last = state.last;
  if (!last) return;

  const { bitmap } = last;
  if (detections.width !== bitmap.width || detections.height !== bitmap.height) {
    detections.width = bitmap.width;
    detections.height = bitmap.height;
  }
  $("stage-canvas").style.aspectRatio = `${bitmap.width} / ${bitmap.height}`;
  $("empty-canvas").hidden = true;

  dctx.drawImage(bitmap, 0, 0);
  for (const box of paintedBoxes()) {
    paintBox(dctx, box, detections.width, detections.height);
  }

  $("frame-note").textContent =
    `frame at ${fmtTime(last.stamp)} · ${bitmap.width}x${bitmap.height} · ${last.data.latency_ms} ms`;
}

function paintBox(ctx, item, width, height) {
  const edges = item.box;
  const x = edges.left * width;
  const y = edges.top * height;
  const w = (edges.right - edges.left) * width;
  const h = (edges.bottom - edges.top) * height;
  let color = boxColor(item);
  const line = Math.max(2, Math.round(width / 480));
  const unsure = item.confidence < SURE;

  const picked = item.uid === state.selected;
  const fromList = picked && state.selectedFrom === "list";
  if (fromList) color = PICKED_COLOR;

  ctx.lineWidth = picked ? line * 2.5 : line;
  ctx.strokeStyle = color;
  ctx.setLineDash(unsure && !picked ? [line * 3, line * 2] : []);
  if (picked) {
    ctx.shadowColor = color;
    ctx.shadowBlur = line * 6;
  }
  ctx.strokeRect(x, y, w, h);
  ctx.shadowBlur = 0;
  ctx.setLineDash([]);

  const text = picked && !item.kept
    ? `${boxLabel(item)} — ${item.reason}`
    : `${boxLabel(item)}${unsure ? " ~" : ""}`;
  const size = Math.max(12, Math.round(width / 56));
  ctx.font = `600 ${size}px -apple-system, system-ui, sans-serif`;
  const pad = Math.round(size * 0.35);
  const chipW = ctx.measureText(text).width + pad * 2;
  const th = size + pad * 2;
  const ty = y - th < 0 ? y : y - th;

  // Per-character boxes label at the top right. They routinely overlap the
  // region boxes, and a shared corner put two labels on top of each other.
  const tx = item.kind === "character"
    ? Math.max(0, Math.min(x + w + line / 2 - chipW, width - chipW))
    : Math.max(0, Math.min(x - line / 2, width - chipW));

  ctx.fillStyle = color;
  ctx.fillRect(tx, ty, chipW, th);
  ctx.fillStyle = "#0c0f14";
  ctx.textBaseline = "middle";
  ctx.fillText(text, tx + pad, ty + th / 2);
}

// ---------------------------------------------------------------- readout

function renderReadout() {
  const { data, roundTrip } = state.last;

  $("st-latency").textContent = `${data.latency_ms} ms`;
  $("st-round").textContent = `${roundTrip} ms`;
  $("st-requests").textContent = state.stats.errors
    ? `${state.stats.requests} (${state.stats.errors} failed)`
    : String(state.stats.requests);
  $("st-questions").textContent = String(data.questions);
  $("st-tokens").textContent = state.stats.tokensIn.toLocaleString();
  $("st-cost").textContent = `$${(state.stats.tokensIn * USD_PER_TOKEN).toFixed(6)}`;
  $("st-model").textContent = data.model || "—";

  const counts = $("counts");
  counts.innerHTML = "";
  for (const [kind, info] of Object.entries(data.counts || {})) {
    const block = document.createElement("div");
    block.className = "count-block";
    block.style.color = COLORS[kind];

    const probability = info.probabilities?.[info.choice];
    const row = document.createElement("div");
    row.className = "count-row";
    row.innerHTML = `
      <span class="name">${info.label}</span>
      <span class="val">${countLabel(info.choice)}</span>
      <span class="prob">p ${fmtNum(probability)} · conf ${fmtNum(info.confidence)}</span>`;
    block.append(row);

    const histogram = buildHistogram(info);
    if (histogram) block.append(histogram);

    counts.append(block);
  }
  if (!counts.children.length) counts.innerHTML = '<p class="muted">No count questions asked.</p>';

  $("raw").innerHTML = highlightJson(data.raw);
}

function countLabel(choice) {
  if (choice === undefined || choice === null) return "\u2014";
  return choice === "more" ? `>${state.cfg.count_max ?? 10}` : choice;
}

// One bar per option, so the whole distribution is visible rather than just
// the pick. Scaled to the largest bar: these are often flat, and a flat row of
// invisible bars reads as "no answer".
function buildHistogram(info) {
  const entries = Object.entries(info.probabilities || {});
  if (entries.length < 3) return null;

  const peak = Math.max(...entries.map(([, v]) => v), 0.0001);
  const host = document.createElement("div");
  host.className = "hist";

  for (const [key, value] of entries) {
    const bin = document.createElement("span");
    bin.className = key === info.choice ? "bin pick" : "bin";
    bin.title = `${countLabel(key)}: ${value.toFixed(3)}`;
    bin.innerHTML = `<i style="height:${Math.max(2, (value / peak) * 100)}%"></i><b>${countLabel(key)}</b>`;
    host.append(bin);
  }
  return host;
}

function renderBoxList() {
  const host = $("boxes");
  // What is on screen first, then by how sure we are it is there. Colour
  // breaks ties, so crimson still reads before green before yellow.
  const candidates = state.view
    .filter(shown)
    .map((item, index) => ({ item, index }))
    .sort((a, b) => {
      const drawn = Number(b.item.kept) - Number(a.item.kept);
      if (drawn) return drawn;
      const presence = (b.item.presence ?? 0) - (a.item.presence ?? 0);
      if (presence) return presence;
      const group = GROUP_ORDER[colorGroup(a.item)] - GROUP_ORDER[colorGroup(b.item)];
      return group || a.index - b.index;
    })
    .map((entry) => entry.item);
  const kept = visibleBoxes();

  $("box-summary").textContent = candidates.length
    ? `${kept.length} of ${candidates.length} bands`
    : "";

  host.innerHTML = "";
  if (!candidates.length) {
    host.innerHTML = '<p class="muted">No answer yet.</p>';
    return;
  }

  for (const item of candidates) {
    const row = document.createElement("div");
    row.className = item.kept ? "box-row" : "box-row dropped";
    row.dataset.uid = item.uid;
    if (item.uid === state.selected) {
      row.classList.add("selected");
      if (state.selectedFrom === "list") row.classList.add("from-list");
    }
    row.innerHTML = `
      <span class="swatch" style="background:${boxColor(item)}"></span>
      <span>
        <span class="name">${item.label}${identitySuffix(item)}</span>
        <span class="where">${item.kept
          ? (item.confidence < SURE ? `${item.region} · edges approximate` : item.region)
          : item.reason}</span>
        <span class="meter"><i style="width:${(item.presence * 100).toFixed(0)}%"></i></span>
      </span>
      <span class="nums">
        present ${fmtNum(item.presence)} · conf ${fmtNum(item.confidence)}<br>
        ${fmtEdges(item.box)}
      </span>`;
    host.append(row);
  }
}

// ---------------------------------------------------------------- events

const EVENT_LIMIT = 300;
let eventCount = 0;

// Report something the viewer did, so it lands in the same ordered stream as
// the backend's own events rather than a separate list.
function logEvent(message, detail, level) {
  const body = new FormData();
  body.append("message", message);
  if (level) body.append("level", level);
  if (detail) body.append("detail", JSON.stringify(detail));
  fetch("/api/events", { method: "POST", body }).catch(() => {});
}

// A live tail of what the backend is doing. Cast lookups take tens of seconds
// and were previously silent apart from a spinner.
function startEventLog() {
  const log = $("event-log");
  const source = new EventSource("/api/events/stream");

  source.addEventListener("message", (message) => {
    let event;
    try {
      event = JSON.parse(message.data);
    } catch {
      return;
    }

    const row = document.createElement("li");
    row.className = `${event.kind} ${event.level}`;

    const when = new Date(event.at * 1000).toLocaleTimeString(undefined, { hour12: false });
    const extra = event.detail
      ? Object.entries(event.detail)
          .filter(([, v]) => v !== null && v !== undefined && v !== "")
          .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
          .join("  ")
      : "";

    row.innerHTML = `<span class="when"></span><span class="kind"></span>`
      + `<span class="what"></span>`;
    row.children[0].textContent = when;
    row.children[1].textContent = event.kind;
    row.children[2].textContent = event.message;
    if (extra) {
      const note = document.createElement("div");
      note.className = "extra";
      note.textContent = extra;
      row.children[2].append(note);
    }

    // Stay pinned to the newest unless the reader has scrolled up.
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.append(row);
    while (log.children.length > EVENT_LIMIT) log.firstChild.remove();
    if (atBottom) log.scrollTop = log.scrollHeight;

    eventCount += 1;
    $("events-count").textContent = `(${eventCount})`;
  });

  source.addEventListener("error", () => {
    // EventSource reconnects on its own; nothing to do.
  });
}

// ---------------------------------------------------------------- snapshots

// Writes the frame, the exact request, the answer and the settings to
// runtime/, so a bad result can be handed to someone else and replayed
// without this app.
async function saveSnapshot() {
  const button = $("snap-save");
  if (!state.last) {
    showError({ detail: "Nothing to snapshot yet — run a detection first." });
    return;
  }

  const label = button.textContent;
  button.textContent = "Saving\u2026";
  button.disabled = true;

  try {
    const body = new FormData();
    body.append("note", $("snap-note").value);
    // What was actually on screen, including why each box was kept or dropped.
    body.append("view", JSON.stringify(state.view.map((item) => ({
      label: boxLabel(item),
      kind: item.kind,
      colour: boxColor(item),
      drawn: item.kept && shown(item),
      reason: item.reason,
      presence: item.presence,
      confidence: item.confidence,
      identity: item.identity,
      box: item.box,
    }))));
    body.append("video_name", state.videoName);
    body.append("thresholds", JSON.stringify({
      presence: state.presenceMin, boxConfidence: state.boxMin, nameMin: NAME_MIN, iouMax: IOU_MAX,
    }));

    const response = await fetch("/api/snapshot", { method: "POST", body });
    const data = await response.json();
    if (!response.ok) {
      showError(data);
      return;
    }

    clearError();
    $("snap-note").value = "";
    button.textContent = "Saved \u2713";

    // The file stays on disk under runtime/; nothing is handed to the browser,
    // so taking several cases in a row does not bury you in downloads.
    $("frame-note").textContent = `saved runtime/${data.name}`;
  } catch (error) {
    showError({ detail: String(error) });
  } finally {
    setTimeout(() => {
      button.textContent = label;
      button.disabled = false;
    }, 1800);
  }
}

// Writes just Jev's answer to runtime/, alongside the fuller case files.
async function saveResponse() {
  const button = $("raw-save");
  if (!state.last) {
    showError({ detail: "Nothing to save yet — run a detection first." });
    return;
  }

  button.disabled = true;
  try {
    const response = await fetch("/api/response", { method: "POST" });
    const data = await response.json();
    if (!response.ok) {
      showError(data);
      return;
    }
    clearError();
    $("raw-saved").textContent = `saved runtime/${data.name}`;
  } catch (error) {
    showError({ detail: String(error) });
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- json view

const JSON_TOKENS =
  /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"\s*:?)|\b(true|false)\b|\b(null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

// Small enough to keep inline; avoids pulling a highlighter dependency into a
// page that otherwise has none.
function highlightJson(value) {
  const text = JSON.stringify(value, null, 2)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  return text.replace(JSON_TOKENS, (match, str, bool, nul, num) => {
    if (str !== undefined) {
      const isKey = str.trimEnd().endsWith(":");
      return `<span class="j-${isKey ? "key" : "str"}">${match}</span>`;
    }
    if (bool !== undefined) return `<span class="j-bool">${match}</span>`;
    if (nul !== undefined) return `<span class="j-null">${match}</span>`;
    return `<span class="j-num">${num}</span>`;
  });
}

// ---------------------------------------------------------------- build stamp

const RELATIVE_UNITS = [
  ["year", 31536000],
  ["month", 2592000],
  ["week", 604800],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
  ["second", 1],
];

function relativeTime(epochSeconds) {
  const delta = epochSeconds - Date.now() / 1000;
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of RELATIVE_UNITS) {
    if (Math.abs(delta) >= size || unit === "second") {
      return rtf.format(Math.round(delta / size), unit);
    }
  }
  return "just now";
}

// The absolute time never changes; only the "x ago" part is repainted.
function startBuildStamp() {
  const epoch = state.cfg.build_epoch;
  const node = $("build-info");
  if (!epoch) {
    node.textContent = `v${state.cfg.version ?? "?"}`;
    return;
  }

  const when = new Date(epoch * 1000);
  const absolute = when.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });

  const paint = () => {
    node.textContent = `v${state.cfg.version} · built ${absolute} · ${relativeTime(epoch)}`;
    node.title = when.toString();
  };

  paint();
  setInterval(paint, 30000);
}

// ---------------------------------------------------------------- helpers

function setBusy(busy) {
  const dot = $("busy");
  dot.classList.toggle("busy", busy);
  dot.title = busy ? "asking Jev" : "idle";
}

function showError(payload) {
  const node = $("error");
  node.hidden = false;
  node.textContent = payload?.detail || payload?.error || "Request failed.";
}

function clearError() {
  $("error").hidden = true;
}

function fmtTime(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function identitySuffix(item) {
  const id = item.identity;
  if (!id) return "";
  const pct = (id.probability * 100).toFixed(0);
  const close = id.runners_up?.length
    ? ` <span class="runnerup">vs ${id.runners_up.map(([n, v]) => `${n} ${(v * 100).toFixed(0)}%`).join(", ")}</span>`
    : "";
  return ` <span class="who">${id.name} ${pct}%</span>${close}`;
}

function fmtNum(value) {
  return typeof value === "number" ? value.toFixed(2) : "—";
}

function fmtEdges(box) {
  return `L ${box.left.toFixed(2)} T ${box.top.toFixed(2)} R ${box.right.toFixed(2)} B ${box.bottom.toFixed(2)}`;
}

boot();
