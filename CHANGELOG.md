# Changelog

Notable changes, newest first. Dates are the day the work landed.

The version in `VERSION` (`python/app.py`) and the build stamp in the page
header identify a running copy; this file explains what changed between them.

## [Unreleased]

### Added
- **Events panel.** An expandable live tail of backend telemetry over
  server-sent events: each step of a cast lookup, every detection with its
  question count, latency and tokens, transcodes, snapshots and failures. The
  last 400 are kept, 80 replayed on connect. `GET /api/events` for JSON.
- **Save JSON** beside the raw response, writing just Jev's answer to
  `runtime/…-response.json`.
- **[Who's That?](https://whosthatcast.com) as a cast source**, used when
  Wikipedia has no usable cast — a show too new or too small for an article
  with a cast table. Resolved through its TMDB id, since the site's own pages
  are not findable by search.
- **Film support.** No season in the filename means a film: the `== Cast ==`
  bullet list is read instead of a season table.
- **Character descriptions from the character's own wiki page.** Infobox
  species, hair and eyes where present, with gender read from the page's
  pronouns as a floor. A character whose actor changes between seasons is
  described per incarnation, chosen by the cast table's qualifier column.
- **Transcode fallback.** A file the browser cannot decode is piped through
  ffmpeg to fragmented MP4 and streamed, when the backend can reach it.
- **Debug cases.** `Snapshot case` writes one self-contained JSON to
  `runtime/` — frame, exact request, answer, settings, cast and what was
  drawn. The stored request replays against Jev as it stands.
- **A Box per named character** on every frame, drawn in crimson, alongside
  the region search. It finds characters that lose their region to a louder
  neighbour, at the cost of floating absent ones.
- **Colour chips** to show or hide each kind of box; visibility only.
- Version and build stamp in the page header, from the newest source mtime.
- Syntax highlighting for the raw Jev response.

### Changed
- **Detections no longer post an event per frame.** One line per frame buried
  everything else; the Calls card already shows latency, tokens and question
  count. Failed detections are still reported.
- **Character identification is always on.** The checkbox, the
  `--no-characters` flag and the request parameter are gone.
- **Slot counts are no longer in the page**, and both default to 10. Change
  them with `--max-ships` / `--max-people`.
- **A failed cast lookup clears the cast** instead of keeping the previous
  show's, and persists the empty result so a restart cannot resurrect it.
  People are still boxed, just unnamed.
- **Boxes list sorts by what is on screen**: drawn first, then by presence,
  with colour breaking ties.
- **Slots are uncapped** and the region grid tiles the frame at any count.
- **Count questions enumerate** `0 … 10, more` instead of
  `none`/`one`/`several`, and the Counts card draws the distribution.
- Snapshots are written to `runtime/` only — no browser download.
- Playback is held until the cast finishes loading, with an elapsed counter
  and a **Play anyway** escape.
- Boxes are drawn only on the detections panel, never over the source video.
- The app moved into `python/`, with `build-python.sh` and `run-python.sh`.
- Low-confidence boxes are drawn dashed rather than discarded; presence gates
  instead.

### Fixed
- **Cast tables parsed by position produced episode titles as character
  names.** Columns are now located by their headers and rows laid onto a grid
  honouring `colspan` and `rowspan`. Fixed Doctor Who, and Battlestar, whose
  column is labelled `Performer`.
- **Honorific stripping turned "The Doctor" into "The".**
- **Season 12 matched both a 1974 season and a 2020 series.** The year in the
  filename now decides.
- **A template placeholder reached a Box question verbatim** — the guard for
  it ran after the apostrophe had been stripped, so it never matched.
- **Release names leaked into search titles** (`enders game sparks`), and
  `Blade.Runner.2049.2017` was read as a film from 2049.
- **Snapshots described the wrong video.** The video name and cast are frozen
  at detect time, and a snapshot is refused when the page has moved on.
- **Colour chips desynced from the canvas** after a reload, because browsers
  restore checkbox state without firing `change`.
- **Odd region counts left a corner of the frame unsearched.**
- **Non-Latin casts collapsed into one entry** — ASCII-only slugs.
- Film cast lines took a link from the prose ("navigator") or split a name at
  its period ("Lt. Joshi" → "Lt").
- A file the browser could not decode failed to a silent black panel.

## [0.1.0] — 2026-10-15

First working version: plays a video, samples frames to Jev's System One
image API, and draws ship and person boxes beside the source.

- Region-based `Box` questions, each gated by a companion `Noul`, with counts
  as a `Choice` — all in one call per frame.
- IoU deduplication of overlapping boxes.
- Adjustable presence and box-confidence thresholds, re-filtering the last
  answer without a new call.
- Latency, token and cost readout; raw response view.
- Bundled CC0 sample clip.
