# Audiobook Studio

**v0.2.1** · Developed by Scott Albretsen, TSAL Solutions LLC

A local, autonomous audiobook production studio for Apple Silicon Macs. Upload complete
manuscripts in the browser, pick an approved narrator for each, queue them, and let the Mac
narrate them. Then listen in the same interface, fix flagged passages, export chapter-marked
audiobooks and record a human release approval.

Everything runs on this Mac: Qwen3-TTS through mlx-audio for narration, mlx-whisper for
independent verification, FFmpeg for mastering, and SQLite for state. The only network traffic
is the one-off download of public Python packages and model weights. Manuscripts and voice
recordings never leave the machine, and there is no telemetry or analytics.

## Install and start

```bash
bash install_audiobook_studio_claude.sh          # needs Homebrew; installs python@3.12 + ffmpeg if missing
cd ~/audiobook-studio-claude
./start-studio                                   # opens http://127.0.0.1:7861
```

### Installer options

| Option | Effect |
|---|---|
| `--no-models` | Skip the weight download. Models then download on first use. |
| `--prefetch-all` | Also download the Base (cloning) and VoiceDesign models. |
| `--port 7862` | Use a different port. |
| `--skip-tests` | Don't run the test suite after installing. |
| `--stop-running` | Stop a running studio before upgrading it. |
| `AUDIOBOOK_STUDIO_HOME=/path` | Install somewhere other than `~/audiobook-studio-claude`. |

The default prefetch downloads CustomVoice 1.7B and Whisper large-v3-turbo, about 6 GB. The
Base and VoiceDesign models add about 9 GB more.

### Re-running the installer

You can run the installer again at any time. It upgrades the application code and leaves
everything else alone:

* The previous code is moved to `backups/app-<timestamp>/`, and the last 5 copies are kept.
* The database is backed up before any schema migration, to `backups/db/`.
* These are never touched: `config/`, `data/`, `library/`, `cache/` and your exports.

## Daily use

1. **Narrator Studio.** Preview the built-in English speakers, Ryan and Aiden, or add a narrator
   of your own:
   * A designed voice, created from a text description.
   * An authorised clone, made from a 5–15 s clip and its exact transcript. You must confirm that
     you own the voice or have the speaker's written permission.

   Previews are real local synthesis. Each one has a player and a WAV download.
2. **Library & Import.** Drop in EPUB, DOCX, TXT or MD files. Several at once is fine. Each file
   gets a preview showing:
   * the chapters, in EPUB spine order;
   * word and segment counts;
   * exactly what will and will not be read (images, tables, footnotes, navigation, code);
   * any parse warnings.

   Set the metadata, pick the narrator, choose the chapters, then click **Add & queue**.
3. **Queue.** Books are narrated one segment at a time by a single GPU worker, in the order you
   set. Drag rows or use the arrow buttons to reorder. You can pause, resume, retry or cancel.
   Other books keep going if one fails.
4. **Overview.** Shows live progress, QA flags and failures. Click a flag to jump straight to the
   segment that needs review.
5. **Review & QA.** Work through segments by chapter and filter. For each one you see:
   * the original text, the spoken rendition and Whisper's transcript, with a word diff;
   * the WER, the issues found, and every take with its own player.

   The actions are **Approve**, **Regenerate**, **Edit spoken rendition** (the manuscript text
   never changes), **Use this take** and **Note**. Keyboard: `J`/`K` move between segments, `A`
   approves, `R` regenerates, `E` edits, `P` plays.
6. **Book page.** This is where you work with the finished audio:
   * Assemble the chapter masters (WAV and MP3).
   * Export a chapter-marked M4B, a single chapter as M4B, or a ZIP of chapter MP3s.
   * Write the QA report as HTML and JSON.
   * Record release signoff.

### Reference voice lab (record a cloned narrator in the browser)

**Narrator Studio → Add a narrator → Record a reference voice** walks a new user from a blank page to a cloned narrator
without uploading anything:

1. **Set up.** Read the room, microphone and delivery checklist, pick a microphone, and watch the
   live level meter. The studio records raw audio and switches echo cancellation, noise suppression
   and auto-gain off on purpose.
2. **Room check.** Four seconds of silence measure your room's background noise, with advice.
3. **Record.** Pick a sample script and read it from the teleprompter. There are six neutral core
   scripts of 10–18 seconds each (calm narration, a long sentence, hard consonants, dates and names,
   dialogue, non-fiction), plus four optional emotional ones. You can also read a page from your own
   book. The studio saves a 16-bit WAV and shows its waveform.
4. **Review & audition.** Each saved take gets an objective quality report (length, clipping, level,
   background noise, signal-to-noise, pauses) with a verdict from excellent to poor. You can also:
   * check the transcript with Whisper, because the clone learns the words you typed next to your
     voice, so they must match exactly;
   * audition the take, which synthesizes the same passage with the Base model so you can compare
     takes by ear.
5. **Create narrator.** Confirm that the voice is yours or that you have permission. The take then
   becomes a cloned narrator, and a take rated poor cannot.

**Microphone access needs a secure page.** Browsers disable the microphone on plain `http://` network
addresses. With `--lan` the installer therefore also serves **HTTPS on port 8443** with a self-signed
certificate generated on the Mac for its own host name and addresses. Open
`https://<mac-name>:8443`, accept the one-time browser warning (or install the certificate from the
lab's notice), and recording works from any computer. On the Mac itself, `http://localhost:<port>` also
works. Without HTTPS the lab explains this and you can still upload a recording you made elsewhere.

**Several takes?** One narrator uses **one reference clip**. Record 2–4 neutral takes and audition them
to keep the best. Keep the emotional register neutral-to-warm and consistent: a clone inherits the delivery
of its reference. For a different mood, create a separate narrator from an emotional take.

### Drafts and release

Exports are labelled **DRAFT** until a person records release approval. Approval is only
possible once every segment has audio, no automatic QA flags remain open, and the chapter
masters are current. A full listen is strongly recommended. Changing any audio afterwards
revokes the approval automatically.

The studio produces local publication candidates. It does not upload anything to Audible, ACX
or any other host.

## How the pipeline works

**Import.** The original file is stored read-only, together with its SHA-256 hash. The parser:

* keeps a manifest of what is and isn't narrated;
* splits the text into natural 150–400 character segments at sentence and clause boundaries,
  never mid-word;
* gives each segment a stable ID such as `bk_xxxx-c003-s0042`.

A pronounceable rendition (numbers, currency, abbreviations) is stored alongside the immutable
original, and an operator override always takes precedence.

**Per-segment QA.** Each segment goes through the same steps:

1. Synthesize with Qwen3-TTS.
2. Write a 16-bit WAV atomically, with a provenance sidecar.
3. Transcribe it with Whisper.
4. Normalise both texts so that harmless differences (numbers, contractions, punctuation,
   British/American spelling) are ignored.
5. Align them word by word and classify the differences: dropped words, a cut-off ending,
   repetitions, hallucinated speech, substitutions.
6. Run the acoustic checks: empty or corrupt audio, clipping, dead air, abnormal level and
   suspicious duration.

**Retries.** The default budget is 3 attempts in total, each with a different seed and
temperature. If every attempt fails, the best take is kept, the segment is flagged for a human,
and generation moves on. Flagged segments are never retried automatically, including after a
restart.

**Crash and sleep safety.**

* Every attempt is committed to SQLite as it happens. An attempt interrupted by a crash, sleep
  or force-quit counts against the budget, so the worker cannot loop forever.
* The worker holds an SQLite lease with a heartbeat. The web server restarts it if it dies.
* While jobs run, `caffeinate -i` prevents idle sleep. You can turn this off in Settings.

**Narrator consistency.** Each book freezes its narrator version and synthesis settings at import
and records them as a fingerprint. Takes rendered with a different fingerprint can never be
assembled into the book. To change the narrator, confirm the change explicitly; the book is then
regenerated.

**Assembly.**

* Takes are trimmed and joined with bounded pauses (sentence, paragraph, scene break, after
  headings) plus room tone.
* Each chapter is loudness-normalised with two-pass EBU R128 (default −19 LUFS, −3 dBTP) and
  resampled to 44.1 kHz.
* The outputs are a WAV master, a 192 kbps CBR MP3, and an AAC M4B with chapter markers taken
  from the measured chapter durations.
* Every output is checked with ffprobe for codec, sample rate, duration and chapter count.
* Missing audio blocks the export, with an explicit list of what is missing. It is never
  silently skipped.

## Storage

Open **Settings & Storage** to set the library root (for example `~/AudiobookLibrary` or
`/Volumes/AudiobooksSSD/Library`). You can also give each category its own location: manuscripts,
reference clips, selected takes, alternate takes, QA reports, masters and exports.

**How locations are chosen.** A browser cannot give the server access to a folder, so you type
the path and the server validates it. The check reports:

* the resolved path;
* whether it is writable;
* free space and current usage;
* whether a `/Volumes/...` drive is mounted.

On macOS, **Choose…** opens the real Finder folder picker on this Mac.

**What happens when you change a location.** Each project keeps its own record of where its
files are. A new location applies only to books imported afterwards, and existing books stay
where they are and remain findable. To move an existing book, use **Move project** on its book
page. That copies every file, verifies each one by size and SHA-256, and then switches the
database in a single transaction. The old copy is kept until you delete it.

**Removable drives.** If a drive is disconnected, that book's job pauses with a storage reason,
the other books continue, and the paused job resumes automatically when the drive returns. The
database stays on the internal disk at `data/studio.db`, so queue state is never lost with a
drive.

**Directory layout.**

```
~/audiobook-studio-claude/
  app/        application code (replaced on upgrade; previous versions in backups/)
  venv/       Python 3.12 environment
  config/     settings.json
  data/       studio.db (SQLite, WAL), previews, pending imports
  library/    default storage root: manuscripts/ segments/ alternates/ masters/ exports/ reports/ references/
  cache/      Hugging Face model cache (HF_HOME)
  logs/       server.log, worker.log, install test logs, smoke-test reports
  backups/    previous app code and pre-migration database copies
```

## Security

* The studio binds to `127.0.0.1` only. Settings refuse a non-loopback address unless you set
  `allow_remote` explicitly, and `studio serve` also needs `--allow-remote`. There is no
  authentication, so do not expose the studio to a network.
* Requests are checked against a Host allowlist, which blocks DNS rebinding.
* Every mutating request needs a same-origin `Origin` and an `X-Studio-Request` header, which
  blocks cross-site requests.
* The Content Security Policy only allows content from the studio itself.
* Uploads are validated: extension, file content, size limits and sanitised names.
* Audio and downloads are served only by database ID, and each resolved path must lie inside the
  book's registered storage, so path traversal is impossible.

## Limits of automatic QA

* Whisper agreement is evidence, not proof. Whisper can mis-hear names (false flags), and it can
  "autocorrect" a mispronounced word into the right spelling (a missed error).
* ASR cannot judge pronunciation, emphasis, emotion, pacing or character voices.
* VoiceDesign re-creates the voice from its description on every segment, so the voice may drift
  over a long book. Promote a preview you like to a stable narrator: it becomes a clone reference.
* Listen before releasing. Check your distributor's technical and AI-narration requirements, and
  confirm that you hold the rights to every manuscript and voice. See MODEL_LICENSES.md.

## Command line

The web UI is the main interface. The CLI is for automation and diagnostics:

```bash
./studio doctor                       # environment check
./studio import book.epub --voice Ryan --queue
./studio run                          # process the queue in the foreground (when the web studio is not running)
./studio list | ./studio issues <book> | ./studio approve <sid> | ./studio approve <book> --release --by "Name"
./studio report <book> | ./studio export <book> --kind m4b|chapter_m4b|mp3_zip
./run-tests                           # automated tests (mock engine)
./smoke-test                          # real-hardware test (below)
```

## Tests

`./run-tests` runs the unit tests and the HTTP integration tests through FastAPI's TestClient.
These tests use the real database, scheduler, worker, FFmpeg assembly and Range streaming, but
the audio comes from the **mock engine**. Its output is synthetic test tones, labelled `mock`
everywhere, and is never presented as MLX inference.

`./smoke-test` must be run on the target Mac. It exercises real hardware:

* Qwen3-TTS CustomVoice (Ryan), Base cloning (`ref_audio`/`ref_text`, or a synthetic reference)
  and VoiceDesign;
* Whisper verification;
* two queued manuscripts processed by the real worker;
* a chapter M4B export validated with ffprobe.

It writes `logs/smoke-<timestamp>/smoke_report.md` and `.json` with the measured RTF, peak
memory, model download size and any defects. Then finish the check by hand:

1. Open the UI and listen to a segment.
2. Approve it.
3. Record release signoff.
4. Play the M4B in Apple Books and check the chapter markers.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "GPU worker offline" | Open System & Licenses and click **Restart worker**. Then check `logs/worker.log` and `logs/worker.out.log`. |
| Model download fails | Check the network. You can re-run the installer, or let the studio download on first use. To work fully offline, cache every model (`--prefetch-all`), then disable downloads in Settings. |
| Out of memory | In Settings > Models, set "Models kept loaded" to 1. |
| Uninstall | Run `./stop-studio`, then delete `~/audiobook-studio-claude`. Your library may live elsewhere if you changed its location. |
