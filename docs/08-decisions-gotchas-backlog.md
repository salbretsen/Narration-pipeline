# 8. Decisions, gotchas, known limits and backlog

## Key design decisions (and why)

| Decision | Reason |
|---|---|
| Single self-contained Bash installer generated from `src/` | The brief required one file with no dependency on other project files; generating it keeps the source maintainable |
| One serial GPU worker in a **separate process** | Avoids MLX contention and keeps HTTP responsive; the supervisor restarts it; a SQLite lease prevents two workers |
| SQLite (WAL) as the only state store; DB on the internal disk | Durable queue and audit trail; survives restarts, sleep and unplugged external drives |
| Attempt counted **before** inference | A crash/sleep cannot create endless retries; flagged segments are never auto-retried |
| Immutable original text; separate narration rendition and `override_text` | Prevents dropped/altered manuscript text; edits are explicit and audited |
| Per-book settings snapshot + synthesis **fingerprint**; takes must match to be assembled | Prevents silently mixing voices/models inside a book; narrator changes need explicit confirmation |
| Per-project path registry (not auto-migration) when the library root changes | Existing projects always stay findable; moving is explicit, verified and reversible |
| Exports blocked (never shortened) if any audio is missing; DRAFT labelling until human signoff | Never auto-publish merely because ASR passed |
| Whisper QA with word-level alignment, soft name substitutions, short-segment rule | Reduce false positives (names, 5-word headings) while catching drops, repeats, hallucinations |
| Server-side folder browser (admin) instead of the browser file picker | A web page cannot grant the server access to folders; the browser must browse the *hosting Mac* |
| Stateless signed session cookie; PBKDF2 users in `config/auth.json` | No session table; password change/removal invalidates sessions; zero extra dependencies |
| HTTPS with a self-signed certificate generated on the Mac | Browsers expose the microphone only on secure origins; also protects passwords on the LAN |
| Recording-lab jobs reuse `kind='preview'` | Avoids a schema migration; the worker branches on `payload.recording_id` |
| Raw audio capture (no echo cancellation / noise suppression / AGC) in the lab | A voice clone should learn the real voice and room, not a processed one |
| Mock engine writes tones with a transcript sidecar; labelled everywhere | Lets the full pipeline be tested honestly on a machine without MLX |
| One reference clip per cloned narrator; extra takes are audition candidates | The Base model's API takes a single `ref_audio/ref_text`; mixing moods in one clip gave unpredictable blends in the experiment |

## Gotchas already hit (do not rediscover them)

**Environment and tooling**
- The Bash tool mangles inline heredocs with quotes/backslashes/backticks and corrupted files twice. Write patch scripts to files, or use Edit.
- `.gitattributes` forces LF. CR bytes inside embedded heredocs break the installer on macOS; the builder refuses them.
- Windows dev: no `pkill`; PowerShell `Get-NetTCPConnection ... Stop-Process` to free a port. A killed server's subprocess worker can keep log files
  locked, which makes deleting its scratch home fail.
- Python 3.13 on dev vs 3.12 target; the Mac's system Python is 3.9 and cannot run the studio (the venv uses Homebrew `python@3.12`).
- `shellcheck-py` (pip) gives a Windows shellcheck. The installer is `shellcheck -S style` clean; keep it that way.

**Web and browser**
- CSP forbids inline styles/scripts. Progress bars use `data-pct`; dynamic widths are set via the CSSOM.
- `[hidden]` loses to a `display` rule: add explicit `[hidden]{display:none}`.
- Browsers hide `navigator.mediaDevices` on `http://` (non-localhost). The lab's notice is the intended behaviour. Chrome/Edge also ignore
  `Cross-Origin-Opener-Policy` on untrusted HTTP origins (logs a console note), so that header is not sent.
- EventSource sends cookies same-origin; a 401 stream means "sign in again". The SPA falls back to polling `/api/worker`.
- An `<audio>` element must not be re-rendered while playing: views skip repaints during playback and while a form field is focused.
- The lab re-attaches the same DOM element after each Voices repaint; any state held in the DOM is lost if you recreate it.

**Audio and QA**
- The noise-floor estimate uses the quietest 100 ms windows (5th percentile). A 10th-percentile-of-frames estimate wrongly rejected takes read
  in one unbroken flow; "no quiet moment" is reported as a warning, not a failure.
- One misheard word in a 5-word heading is 20% WER; hence the rule "WER flag needs ≥ 10 reference tokens or ≥ 2 word errors".
- Whisper may mishear names/rare words (false flags) and may autocorrect mispronunciations (missed errors). It is evidence, not proof.
- Mock-engine fault keys match the *normalised spoken text*.
- `mlx-audio` 0.6.3 pins `mlx==0.32.4` and requires `transformers>=5.14`; Qwen3-TTS loads print a harmless "model type qwen3_tts ... ''" notice.
  Base models must never receive `voice`/`speaker`; `lang_code` is passed lowercase; `max_tokens` is capped from expected duration.
- Sample rate is read from the model (24000), not hardcoded; references are converted to 24 kHz mono.

**Auth and operations**
- Changing a password invalidates all sessions for that user (by design); long-running test scripts get signed out.
- Operators cannot see Settings/Storage (403). The About page degrades gracefully (environment check hidden).
- `./stop-studio` must stop the **service** (the LaunchAgent restarts a killed process); it does.
- `--stop-running` is required to reinstall over a running studio (the installer checks the pid files).
- A LaunchAgent requires a logged-in user session.

## Known limitations

- Recording with a real human voice in a browser is untested; Safari/Firefox untested.
- Voice quality and throughput over a full 50,000-word book are unmeasured (only ~500-word books were run on the GPU).
- Designed (VoiceDesign) voices drift across long books; use "promote to stable narrator".
- ETA on the queue appears only after 20 measured segments and is only an estimate.
- No data deletion: books are archived, files stay on disk. No uninstall script. No backup/restore UI.
- Audit entries for web actions record the actor as "operator" in several places instead of the signed-in user (release signoff uses the typed name).
- The LAN uses a self-signed certificate (browser warning until trusted). No HSTS, no proxy support.
- One reference clip per narrator; no per-segment voice switching (a book uses one narrator).
- Distributor (ACX/Audible) technical compliance is not verified; the studio produces local publication candidates only.
- Model licences must be re-verified before commercial use (`MODEL_LICENSES.md`).

## Backlog / ideas (none are commitments)

1. Attribute every audit/review action to the signed-in user.
2. Pronunciation dictionary (global and per book) feeding `override_text`; per-voice pace control.
3. Boot-time hosting (LaunchDaemon) and an "unattended" setup guide; optional reverse-proxy/HTTPS with a trusted certificate.
4. Delete/purge books and alternate takes (reclaim disk), backup and restore, a disk-usage dashboard.
5. Long-book validation run (50k words) with throughput, memory and false-positive statistics; tune thresholds from real data.
6. Per-chapter regeneration/export from the UI; chapter title editing; cover generation.
7. Multi-take reference comparison in the lab (side-by-side audition of several takes with the same passage and a score).
8. Test on Safari/Firefox; real-microphone validation; a CI pipeline.
9. Second deployment profile (M4 Max, 36 GB): confirm `max_warm_models=1` and memory headroom.

## Open questions for the owner

- When should the first MAJOR release (v1.0.0) be declared, and what acceptance criteria apply?
- Which licence terms (if any) should the product carry?
- Should operator accounts be allowed to edit voices or only use them?
