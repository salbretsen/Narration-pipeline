# 4. Frontend

A dependency-free single-page app: ES modules, no build step, no CDN, no external fonts. Files are served from
`src/app/studio/web/static/` (so JS/CSS edits take effect on the next page load even while the server runs; Python edits need a
server restart). `index.html` loads `/static/app.css` and the module `/static/js/app.js`.

## Hard constraints (the CSP forbids breaking them)

- **No inline scripts, no inline `style=""` attributes, no inline event handlers.** CSP: `default-src 'self'; script-src 'self';
  style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'`. Set dynamic sizes through the CSSOM:
  progress bars use `data-pct` plus `applyBars()`; the meter sets `el.style.width` from JS. A `<canvas>` is fine.
- **Never interpolate untrusted text into HTML directly.** Use the `html` tagged template from `ui.js`: every `${value}` is escaped
  unless wrapped in `raw()`. Manuscript text, filenames, ASR text and error strings all flow through it.
- **`[hidden]` is overridden by any `display:` rule** on the same element. Add an explicit `.x[hidden]{display:none}` (this caused a
  visible "Password / Sign out" bug once).
- All requests are same-origin relative URLs (never hardcode `localhost` or a host); the `api()` helper adds `X-Studio-Request: 1`.
- Respect accessibility: skip link, landmarks, `aria-live` toasts, focus moves to the page `<h1>` on route change (only when the route really changed), visible focus
  rings, `prefers-reduced-motion`, light/dark tokens, layout works to 360 px wide with no horizontal page scroll.

## Files

| File | Role |
|---|---|
| `index.html` | shell: skip link, top bar (brand, worker pill, mock-engine badge, user box), side nav, `<main id="main">`, toast container |
| `app.css` | design tokens on `:root` (light) and `prefers-color-scheme: dark`; all components; responsive breakpoints at 1100/900/600/480 px |
| `js/app.js` | hash router (per-navigation host, stale-render guard, loading state; contract below), auth bootstrap (`/api/auth/status` → sign-in screen or app), SSE connection with polling fallback, worker pill, version in the sidebar, sign-out and change-password |
| `js/ui.js` | shared helpers (below), including the v0.8.0 repaint helpers `patchHTML`, `patchRegions`, `pager`/`wirePager`, `snapshotForm`/`applyForm`, `sameState` |
| `js/runtime.js` | v0.8.0: the only place that turns a server `runtime` dict into text (`fmtRuntime`, `fmtSpan`, `fmtClock`); Overview, Library and Queue call it |
| `js/recorder.js`, `js/recorder-worklet.js` | microphone capture (`Mic`), WAV encoder, room/noise metrics, waveform envelope |
| `js/views/overview.js` | dashboard: fixed shell with live regions (cards, strip, recent books, attention list capped at 5 with *Show all*, activity feed with a "N new events" hint, worker/update notices) |
| `js/views/library.js` | upload, import preview (form values survive redraws; narrator must be chosen), library list grouped by manuscript with a narrator dropdown when a book has several narrations |
| `js/views/queue.js` | queue with drag/keyboard reorder, logs, controls, processing-time line (`fmtRuntime`), one *Recently finished* row per book with a "+N earlier runs" disclosure, 10-per-page pagers for finished books and for assembly/export/preview/analysis jobs (Book column) |
| `js/views/voices.js` | Narrator Studio (**v0.9.0**): one **tile** per narrator (name and description link to the narrator page; ▶ generates the kept sample once via `POST /api/voices/{id}/sample`, then plays it; a blocked autoplay shows "Ready: press to play"), and a collapsed **Add a narrator** section with the tabs (Preset, Designed, Clone by upload, **Record a reference voice**). Live via `onState` + `patchRegions('#voice-grid')` |
| `js/views/narrator.js` | **v0.9.0** the narrator page `#/narrator/ID?tab=overview|emotions|preview|record|tools`: details and version history; **Emotion references** (clones only: 13 emotion rows grouped by family; one take at a time from the chosen source; each take shows Whisper and speaker similarity with Adopt / Discard; Discard all; Remove clip for admins); Preview (form drawn once, result region live; Promote for designed voices); Record & upload (the lab); Tools (Edit, Duplicate & retune, Retune from source, Archive). Dialogs live in `narrator-common.js`, pure helpers in `narrator-model.js` (unit-tested by `tests/js/narrator_model.test.mjs`) |
| `js/views/voicelab.js` | the Reference voice lab (mounted inside the "Record a reference voice" tab) |
| `js/views/review.js` | review workspace (segment list, detail, takes, ASR diff, shortcuts J K A R E P /); v0.8.0: **Regenerate with direction...** form with a Preview that calls `delivery-preview`, the light `?view=review` book payload, regions repainted with `patchRegions`, the list scrolls to the selection only for an explicit pick (`repaintList(reveal)`) |
| `js/views/book.js` | book page: chapters/masters (per-chapter Edit title, Regenerate chapter, M4B), exports, QA report, release signoff, metadata, narrator, storage move. v0.8.0: **form cards are drawn once; only the live regions (stable `bk-...` ids) are repainted** (rule below) |
| `js/views/settings.js`, `settings-model.js` | seven tabs over one form and one Save (map below), storage paths with Check / Browse / Finder, the folder-browser dialog. `settings-model.js` is the pure part (field definitions, `TABS`, `resolveTab`, `tabKeyTarget`, `changedNames`, `countByGroup`, `collectSettings`) and has a node unit suite |
| `js/views/narrative.js`, `narrative-fields.js` | route `#/narrative/ID`: analysis stages and actions, **Apply new delivery settings to existing takes**, **Blind A/B listening results**, project overrides, character directory (edit, merge, voices), story notes; the settings fields (including the v0.7.0 delivery, chapter/scene/translator model and chapter-pass fields) are shared with Settings |
| `js/views/inspector.js` | route `#/inspect/ID[?ab=ROUND]`: the Segment Inspector (context, attribution evidence, direction layers, instruction sent, boundary editing, preview, attempts) and the **Compare settings (blind A/B)** card |
| `js/views/disk.js` | route `#/disk` (admin): paints at once with "Measuring disk usage..." placeholders, then fills each card when `/api/disk/usage` (60 s cache; *Refresh* sends `?refresh=1`; a "Measured ..." line shows `age_s`) and the backup list answer; disk usage per book and category, free space, purge/delete with typed or dialog confirmation (the server's `409` preview feeds the dialog), book and database backup, restore |
| `js/views/pronunciation.js` | route `#/pronunciation` (side nav "Pronunciation"): global or per-book dictionary (add/edit/toggle/delete via dialogs), a Try box, and per-voice pace; changes that would re-render finished segments ask for confirmation using the `409 details.stale_segments` convention |
| `js/views/about.js` | About page (version, credits), worker, environment check, QA limits, licences |

## Patterns

- **Router:** `#/overview`, `#/library`, `#/queue`, `#/voices`, `#/narrator/ID[?tab=]` (v0.9.0), `#/review[/BOOK_ID?seg=&filter=&chapter=]`, `#/book/ID`, `#/settings[/TAB]`, `#/about`.
  Each view module exports `async render(host, parts, query, nav)` and may return a **cleanup function** (remove listeners, stop timers,
  close the microphone). The router calls the previous cleanup before rendering the next view. Router contract (v0.8.0):
  - `host` is a **per-navigation element** (`div.view-page`) created inside `#view` and kept hidden (`.view-pending`) until `render()` resolves; use it as you used `#view` before. Only then is it swapped in and the old page removed,
    so a slow or superseded render can never paint over a newer page (its host is discarded and its cleanup, if it returned one, is called at once).
  - `nav.signal` is an `AbortSignal` that fires when the user navigates away: pass it to GET requests that should be cancelled. **Never use it for writes.** `nav.isCurrent()` says whether this navigation is still the latest;
    check it after an `await` before touching module state or anything outside `host`.
  - Loading state: the old page stays visible; after `LOADING_MS` = 120 ms `#view` gets `.view-stale` (old page dimmed and not clickable) and a `.view-loading` pill ("Loading...", `role=status`) appears; both go when the new page is in
    (`aria-busy` is set meanwhile). Do not add a full-page spinner of your own.
  - Views start their requests together (`Promise.all`) rather than one after another; a page with a slow part (Disk & Backups) paints its shell first and fills that part when it answers.
  - Focus moves to the new `<h1>` and the window scrolls to the top only when the route (path without query) changed; a query-only change (`?seg=`, `?filter=`) keeps the scroll.
- **Live updates:** `app.js` opens an `EventSource('/api/events')` and re-dispatches a `studio:state` window event. Views subscribe
  with `onState(fn, minIntervalMs)` (throttled, returns an unsubscribe) and refetch only what they show. Do not repaint while the
  user is typing in a form or an `<audio>` element is playing (several views check this). Since v0.8.0 `onState` **delivers a payload only if it differs from the last one delivered to that subscriber**
  (`sameState`, a JSON compare; polled-fallback payloads, `state.polled`, are always delivered). The server digest carries no clock values, so on an idle studio the callback is not called at all:
  **a view must not rely on periodic ticks** (for example to refresh an "x minutes ago" label); use its own timer for that and clear it in the cleanup.
- **Live-region rule (v0.8.0).** A live view is a *fixed shell* plus *live regions*. (1) Everything the user can type into or choose (metadata, release sign-off, delivery-instruction fields, a narrator `<select>`, notes) is drawn
  **once** and never re-created by a live update; if a region must contain a form, `snapshotForm(form)` before and `applyForm(form, snap)` after. (2) Each region has a **stable `id`** and is repainted with
  `patchRegions(root, {"#id": (el, i) => template})`, which touches the DOM only when the markup differs from what it painted there last time. (3) Anything inside a region that scrolls, takes focus or is a `<details>` needs its own
  stable `id`, so `patchHTML` can restore it. (4) A fetch result older than a newer one is dropped (a request counter), and a view skips its refresh while an `<audio>` is playing. (5) Scrolling a selected item into view
  happens only for an explicit user action, never inside a live refresh (the Review list once jumped back to the selection on every refresh).
- **`ui.js` repaint helpers (v0.8.0).**
  - `patchHTML(el, tpl)`: like `setHTML`, but keeps `scrollTop/Left` of `el` and of scrollable descendants **that have an id**, keyboard focus and text selection of the focused element if it has an id that exists afterwards, and the open state of every `<details id>`. It does not keep id-less elements, typed form values or `<audio>` position.
  - `patchRegions(root, regions)`: `regions` = `{"css selector": (el, i) => template}`; repaints a region (through `patchHTML`) only when its markup differs from what this helper last painted there (kept in a `WeakMap`); returns the number of regions repainted. `htmlOf(tpl)` gives a template's markup string.
  - `pager(total, offset, limit, {noun})` renders "1–10 of 37" with Prev/Next buttons (`button[data-pager]`, `data-offset` = the offset to go to; only the range text when `total <= limit`). `wirePager(container, (newOffset) => ...)` delegates the clicks once per container (it survives repaints) and returns an unwire function.
  - `snapshotForm(form)` / `applyForm(form, snap)`: capture and restore the named controls (checkbox groups by value).
  - `sameState(a, b)`: JSON equality used by `onState`.
  - `fmtRuntime(rt, {compact})` (`runtime.js`) renders a server `runtime` dict: "about 1 h 20 m left · done about 14:30", "about 2 h 05 m processing · starts about 15:40", "processed in 3 h 12 m", "paused · ...", "no estimate yet" / "estimate after N more segments". Its tooltip says it is processing time, not audio length. Views never format these themselves.
- **Sign-in:** `api()` dispatches `studio:unauth` on a 401 (except `/api/auth/*`); `app.js` then shows the sign-in card. Sessions are
  cookies; the SPA never handles tokens.
- **Feedback:** `busy(btn, fn, okMsg)` disables a button, shows progress, toasts success, and shows errors via `toastError`
  (messages come straight from the server's `error` field, so write server messages for humans).
- **Dialogs:** `dialog({title, body, buttons, form})` / `confirmDialog(...)` use the native `<dialog>`; the folder browser and the lab
  build their own.
- **Audio:** `player(src, label, extraClass)` renders a native `<audio controls>`; seeking relies on the server's HTTP Range support.
- **Identity:** `STATE_LABEL`/`STATE_TONE` in `ui.js` define how segment, job and book states are named and coloured. Keep the four
  production states distinct everywhere: *draft audio rendered*, *QA passed*, *human review required*, *approved*.

## The Reference voice lab (`voicelab.js`)

`createLab({onVoiceCreated})` returns `{el, init, releaseMic, destroy}`. The host view appends `lab.el` into `#lab-host` after every
repaint, so the lab keeps its state while tabs switch. `voices.js` calls `lab.releaseMic()` when the user leaves the record tab
(the microphone must never stay on in the background; the lab then repaints to show it off) and `lab.destroy()` on route change.

Steps: **1 Set up** (guidance, device list, mic on, live meter, Bluetooth-headset warning) → **2 Room check** (4 s silence,
noise-floor verdict) → **3 Record** (script picker, teleprompter, optional 3-2-1 countdown, timer, stop at 30 s, local preview,
waveform, editable "exact words", save) → **4 Review & audition** (take cards with the server quality report, Whisper check,
audition, FAQ on multiple takes and emotion) → **5 Create narrator** (name, rights holder, consent checkbox).
If the take list already has drafts the lab opens on step 4.

**Take comparison (step 4):** each take card has a "compare" checkbox (`data-cmp`, state `L.cmp`); the compare panel can also add a take from an
audio file (`#upload-form`, reuses `POST /api/recordings`). "Compare N takes" calls `GET /api/recordings/compare` and renders a ranked table
(verdict, length, peak, clipping, noise floor, voice-to-noise, Whisper transcript match, audio player, "Use for narrator"). "Whisper-check all" runs
the existing `check` job per take; `paintJob` refreshes the table cells as jobs finish. Ticking or deleting takes clears the comparison.

Recorder facts (`recorder.js`): `getUserMedia({channelCount:1, echoCancellation:false, noiseSuppression:false, autoGainControl:false})`
(raw audio on purpose), an `AudioWorklet` forwards samples, the node is routed through a zero-gain node so the graph runs without
playing the mic back, the WAV is 16-bit mono at the AudioContext's rate (often 48 kHz), uploaded to `POST /api/recordings`.

**Secure-context requirement:** `navigator.mediaDevices` is **undefined on plain `http://` pages except localhost**. `micSupport()`
detects this and the lab shows a notice with the HTTPS URL (from `/api/auth/status.https_port`), the certificate download and
trust steps, and the upload alternative. Do not "fix" this in code; it is a browser rule. Keep `Permissions-Policy: microphone=(self)`.

## v0.7.0 panels (driven in Edge by `tools/e2e/delivery_ui.py` against the mock engine: panel renders, blind A/B pair, vote, reveal, Narrative card; the bank generate/adopt flow and the apply-delivery dialog were not exercised; no real engine, no Safari)

**Reference clips panel (`voices.js`, `bankPanel`).** Shown on every cloned voice card (`#bank-<id>`, loaded from `GET /api/voices/{id}/refs` into the module-level `banks` map; candidate jobs live in
`cands` and are polled every 2 s via `GET /api/jobs/{id}` until finished; the timers are cleared with the rest on route change). One row per bucket (tense, tender, sorrow, bright): the adopted clip (length,
synthetic or recorded, pitch/brightness drift from the base clip, a "mock test tones" badge for mock-engine clips, an audio player on `/media/voice/{id}/ref/{ref}`), **Remove** (only when `isAdmin()`; confirmation),
**Generate candidates...** (only when the clone came from a designed voice that can still be found; posts `n: 3`, rows appear under *Candidates* with Whisper WER and an *Adopt for bucket* button that is disabled
unless the Whisper check matched) and **Adopt a lab recording...** (lists only recordings whose `asr_check.matches` is true; for a human clone a dialog asks for the ownership checkbox and a consent statement,
for a synthetic clone a plain confirmation). The panel only manages clips; switching the feature on is Settings > Narrative > Delivery > *Cloned voices: emotion via reference clips*.

**Blind A/B card (`inspector.js`).** `render(view, parts, query)` now builds two elements, `mainEl` (the redrawn inspector) and `abEl` (`#ab-card`), because the inspector repaints on live updates and a round
in progress (selected comparison, playing clips, the chosen radio, the checkbox) must not be rebuilt by those repaints. State lives in the `ab` object. Flow: pick a comparison (`GET /api/books/{id}/ab`), *Create A/B pair*
(`POST /api/segments/{id}/ab`), poll `GET /api/ab/{round}` every 1.5 s until `ready`, two players (clips X and Y; a MOCK ENGINE warning when the engine was mock), choose X, Y or "No real difference" plus
"Sounds like a different narrator", *Submit vote* (`POST /api/ab/{round}/vote`), then the reveal and the book's running tally for that comparison. *Pick a segment where they differ* opens the segment returned by
`GET /api/books/{id}/ab/random`. `?ab=<round_id>` resumes a round (the Narrative page uses it). A `change` listener on the view handles the form; the cleanup sets `ab.dead` to stop polling.

**Narrative page additions (`narrative.js`).** `#nar-ab` renders the tally table from `status.ab` (per comparison: votes, share of non-tie votes for the first setting with an at/above-60% badge, "another
narrator" flags with an at/below-10% badge, and *Start a round on a random differing segment*, which creates the pair and navigates to the inspector with `?ab=`). `applyDelivery` calls
`POST /api/books/{id}/narrative/apply-delivery` without `confirm` first; the `409` message and counts feed a dialog (with an *Also regenerate approved segments* checkbox when approved ones would change), and
confirming posts again with `confirm: true`. Settings' **Test connection** now also sends the chapter, scene and translator model names.

## Settings tabs and page layout (v0.8.0)

Settings is one `<form>` with **one Save** (and Discard) bar, shown as seven tabs (`TABS` in `settings-model.js`, route `#/settings/<id>`; an unknown id falls back to the first tab; `role=tablist`, arrow keys wrap, Home/End jump):

| Tab id | Label | Cards |
|---|---|---|
| `storage` | Storage | Library location (Check / Browse / Finder), where existing books live |
| `production` | Production | Segmentation limits for classic books, pauses & assembly, export, worker |
| `quality` | Quality & verification | Whisper verification (the summary follows the master switch), pass/fail limits |
| `models` | Voices & models | models, sampling, delivery instructions per model |
| `narrative` | Narrative | semantic segmentation (its segment limits appear only while it is on), speakers & characters, context windows, Director & continuity |
| `llm` | Local LLM | endpoint, API key (Save / Test connection), models for other tasks, timeouts & limits |
| `server` | Server & advanced | port, upload limits (applies after restart) |

Every tab panel stays in the DOM (`hidden` toggled), so a value typed in one tab is still there after visiting another. `changedNames(base, snapshotForm(form))` puts an unsaved dot on each tab with edits (write-only `secret.*` controls never count);
leaving the page with edits asks first, and a back/forward re-render restores the draft. `collectSettings(form)` builds the same `PUT /api/settings` body as before v0.8.0: hiding a tab or a dependent field never removes a key. The tab model
is unit-tested in node (`tests/js/settings_model.test.mjs`); the page itself by `tools/e2e/polish_settings.py` (the folder browser, the Finder picker and the API key Save / Test connection clicks were not exercised there).

**Page width.** `#view` is a centred column, `max-width: 1440px` (`1680px` when the page contains `.review-layout`). Grids cap their column count with `minmax(min(100%, max(Npx, (100% - gaps) / maxColumns)), 1fr)`:
`.stats` and `.stats.compact` at most 6 columns, `.voice-grid` and `.takes` 3, `.ov-grid` 4; `.form-grid` auto-fills at 250 px. Globally, `code { overflow-wrap: anywhere }` and `[hidden] { display: none !important }`.
Known gap: the Book page at 360 px wide still overflows horizontally (a `nowrap` table in `book.js`).

## Folder browser (`settings.js`)

`browseFolder(startPath)` is a custom `<dialog>`: shortcuts (Home, Documents..., `/`, `/Volumes/*`), editable path, folder list,
writable/free-space status from `POST /api/storage/validate`, "Create folder here", "Use this folder". It browses the **hosting
Mac**, not the user's computer (wording says so). The native "Finder…" button only appears when the page is served from
localhost, because it opens on the Mac's own screen.

## Adding a view or control: checklist

1. Add the route name to `ROUTES` in `app.js` and a nav link in `index.html` (with `data-nav`).
2. The view returns a cleanup function; unsubscribe `onState`, remove listeners, clear timers.
3. Use `html\`...\`` for all markup, `busy()` for actions, `toastError` for failures; every control must call a real endpoint.
4. Add CSS using the existing tokens; check light, dark and 390 px wide (the lab/e2e scripts take screenshots).
5. Syntax-check: `node --input-type=module --check < file.js` (CI-equivalent here). Then run the browser scripts in `tools/e2e/`.
