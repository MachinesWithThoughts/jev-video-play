# Architecture

How the pieces fit, and why they are shaped this way. For what the app does
and how to run it, see [README.md](README.md).

## Shape of the system

```
run-python.sh ──► python/app.py ──┬──► static/     the page (vanilla JS, no build)
                                  ├──► cast.py     cast lookup (importable + CLI)
                                  ├──► ffmpeg      transcode fallback
                                  └──► api.typesafe.ai/v1/systemone
```

One process. No database, no bundler, no framework on the page. State lives in
three places: `CONFIG` (what the server was started with), `cast.json` on disk
(the current cast), and `LAST_CALL` in memory (the most recent detection).

The split between the page and the backend follows one rule: **the browser
owns the video, the backend owns the network.** The browser is the only thing
that can decode a video and hand out frames; the backend is the only thing
holding the API keys and able to reach Wikipedia, Brave and Jev without CORS.

## The frame pipeline

1. **Capture.** `tick()` runs on `requestAnimationFrame`. When the video is
   playing, detection is on, nothing is in flight and `minGap` has elapsed, it
   draws the current frame to an offscreen canvas.
2. **Fit to Jev's limits.** `fitDims()` scales to 256–1568 px per side and
   78,400–2,458,624 total pixels — scaling *up* as well as down, because a
   small frame is rejected outright. Encoded as baseline JPEG at 0.85.
3. **Post.** `POST /api/detect` with the JPEG, the slot counts, the page's
   video name and the timestamp.
4. **Ask.** The backend builds the question set and sends one multipart
   request to Jev with `TypeSafe-Danger-Zone-Images: true`, model
   `jev-preview` (Box needs Jev 1.14).
5. **Parse.** Answers become *candidates*: `{kind, label, region, box,
   presence, confidence, identity}`.
6. **Draw.** The page filters, dedupes and paints them on the right-hand
   canvas, which shows the frame the boxes were measured on — never the frame
   currently playing.

Only one request is in flight at a time. Frames are sampled, not queued: if a
call is outstanding when the next frame comes round, that frame is skipped.

## The question model

Everything about one frame is a single call. Jev evaluates questions in
parallel against the same state, so 80 questions cost one round trip.

### Counting

One `Choice` per target kind, enumerated `0, 1, 2 … 10, more`. Not
`none/one/several`: the enumerated form returns a usable distribution, and
weight in `more` says the frame holds more targets than there are slots.

### Locating — regions, not ordinals

`Box` returns exactly one rectangle, so finding N targets means N questions.
Asking for "the second ship from the left" does not work — every ordinal slot
lands on the same salient object. Instead the frame is cut into regions and
each slot asks about its own:

| Slots | Layout |
| --- | --- |
| 1–4 | vertical bands named in words ("the left third of `frame`") |
| 5+ | a two-row grid described by percentages |

Past four, the words stop working ("the seventh tenth") and vertical strips
get too narrow to hold a person. The grid always tiles the frame: an odd count
gives the top row the extra cell and widens the bottom row, rather than
leaving a hole in a corner.

### Gating — Box has no "not found"

Ask `Box` about an empty frame and it still returns four edges. Every box is
therefore paired with a `Noul` asking whether that region holds the target at
all, worded against the same region.

**Presence and box confidence are different numbers.** Presence says whether
the thing is there; confidence says how sure the model is of the *edges*, and
runs low on small or soft targets that were found correctly. Filtering on
confidence throws away true positives — a bridge scene returned three people
at presence 0.94–0.98 with two boxes at 0.14 and 0.21. So presence gates, and
a low-confidence box is drawn dashed rather than discarded.

### Naming — identity is a Choice

Asking `Box` for "the person who is Worf" scores 0.06–0.15 and returns half
the frame. Locating the person by region and then naming them against a closed
cast reaches 0.68–0.97. Each person region gets a `Choice` over the cast,
worded identically to its Box so both land on the same person.

### The second opinion — a Box per character

Alongside the regions, every cast member gets its own gated Box, drawn in
crimson. It is the weaker method and it floats absent characters, but it fails
*differently*: it finds characters that lose their region to a louder
neighbour. Both run on every frame so they can be compared on one image.

## The filtering chain

Entirely client-side, so every threshold re-filters the last answer without a
new call.

```
candidates
  → presence  < gate?        drop ("gated out")
  → confidence < floor?      drop ("low confidence")
  → IoU ≥ 0.5 with a kept box of the same kind?   drop ("same box as X")
  → kept
```

Then for presentation: a box whose identity Choice clears `NAME_MIN` (0.40)
and is not `other` is drawn green with the character's name; a per-character
box is crimson; otherwise cyan for ships, yellow for people. Confidence below
`SURE` (0.35) draws dashed. The Boxes list sorts drawn-first, then by
presence, with colour breaking ties.

The colour chips are visibility only. They filter the drawing and the list;
they never change what is asked.

Each candidate carries a `uid` of `kind:label`, which ties a drawn box to its
row. Clicking the canvas converts the point to a fraction of the element —
the canvas is laid out at the frame's aspect ratio, so that is the same
fraction of the image the boxes are expressed in — and selects the smallest
box containing it. The list is clickable the same way, by delegation since
the rows are rebuilt on every answer. The selection records which side it
came from: picked from the list, the box is redrawn orange, a colour no kind
uses. The painted set is the kept boxes **plus whatever is selected**, so a
gated-out candidate can still be inspected on the frame, labelled with the
reason it was dropped.

## The cast lookup

`cast.py` is both a CLI and a module the backend imports. It writes
`cast.json`, which `app.py` reads at startup and after every rebuild.

### Filename first

`parse_filename()` yields a title, a season and a year. Release names are
cleaned by cutting at the first quality tag and taking the **last year before
it** as the release year — which is what keeps
`Blade.Runner.2049.2017.2160p` as *Blade Runner 2049* from 2017.

No season means a film, and films take a different path entirely.

### Sources, in order

1. **Wikipedia.** TV reads the Actor/Character table off a "list of cast
   members" page; films read the `== Cast ==` bullet list. Wikipedia goes
   first because it is the only source that records **which seasons** a
   character appears in.
2. **[Who's That?](https://whosthatcast.com)** when Wikipedia yields nothing —
   a show too new or small for an article with a cast table. Their pages are
   keyed by TMDB id and ignore the slug, and searching the site does not find
   them, so the id is lifted from a TMDB result instead. No season data.
3. Nothing found → the cast is **cleared**, not left on the previous show's.

### Reading a cast table is not simple

Pages disagree more than they look. One table with numbered season columns, or
a dozen each covering a span with an extra `Appearances` column; `Actor` or
`Performer`; cells separated by `||` on one line; a character carrying
`rowspan` across rows; `colspan="11" {{CMain}}` with no pipe before the
content. So columns are located by their headers and every row is laid onto a
grid honouring both spans. Guessing by position produced episode titles as
character names.

Where a show numbers both seasons and series from 1, the year decides which.

### Describing a character

Descriptions matter more than anything else here: a Box question with one
scores 0.56–0.63 where the bare name scores 0.09.

1. `KNOWN_LOOKS` — a hand-written table, wins outright.
2. `wiki_profile()` — the character's own wiki page, found via Brave. The
   infobox gives species, hair and eyes where the wiki has them; **gender is
   read from the page's pronouns** as a floor, because many wikis have no such
   field and gender alone is worth a lot. On a frame of the Thirteenth Doctor,
   "Look for a woman" lifts the gate from 0.57 to 0.83.
3. `describe()` — a plain snippet search, filtered for appearance words and
   against biography words. The weak one.
4. Nothing — the character is asked by name alone.

A character whose actor changes between seasons is described per incarnation:
the cast table's qualifier column (`Thirteenth`, `Fugitive`) picks the page.

Both 2 and 3 are a rate-limited search **per character**, which is why a
rebuild takes tens of seconds and why playback is gated while it runs.

## Supporting machinery

**Events.** An in-memory ring of 400, fanned out over server-sent events. The
cast builder takes a `log` callable, so its progress becomes events for free.
Only things that happen occasionally are logged — a successful detection is
not, since one line per frame drowns out everything worth reading.
Emission is thread-safe: lookups run off the loop via `asyncio.to_thread`, so
the fan-out is scheduled back with `call_soon_threadsafe`.

**Transcoding.** When the video element reports it cannot decode, the page
retries against `/stream`, which pipes the file through ffmpeg to fragmented
MP4. This needs a path the backend can open, so it only covers a file passed
on the command line — the picker hands over bytes with no location.

**Debug cases.** `write_case()` writes one self-contained JSON to `runtime/`.
The frame travels base64 inside it and the stored request inlines it as a
`data_base64` asset, so the `request` object can be POSTed to Jev as it
stands. The video name and cast are **frozen at detect time**, and a snapshot
is refused when the page has moved to a different video — otherwise a case
quietly describes the previous one.

**Version stamp.** There is no build step, so "built" is the newest mtime
across `app.py` and `static/`. It tracks the code actually being served.

## Concurrency and shared state

- One Jev request in flight per page; later frames are dropped, not queued.
- `CAST_LOCK` serialises rebuilds — each is dozens of rate-limited searches.
- `LAST_CALL` holds one detection. Snapshots read it, never live state.
- `cast.json` is **per checkout, not per server.** Two instances against the
  same directory will overwrite each other's cast. Run one.

## Known limits

- Detection lags playback. Frames are sampled, never tracked between frames,
  so boxes belong to an earlier frame — which is why they are drawn only on
  the detections panel, beside the timestamp they came from.
- Appearance descriptions cannot be sourced reliably. Search snippets are
  biography, Wikipedia prose is plot, and Fandom infoboxes are inconsistent.
  Gender from pronouns is the dependable floor.
- A transcoded stream has no index: it plays, it cannot seek.
- Images and Box are pre-production at TypeSafe; the request shape and the
  accuracy can both change between releases.
