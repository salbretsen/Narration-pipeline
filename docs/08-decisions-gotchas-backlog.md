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

| Narrative layer is opt-in and offset based (v0.3) | Existing books and tests stay compatible; stable ids, exact slices and reconciliation keep edits local |
| `jobs` rebuilt for `analyze`; migrations run with foreign keys off | SQLite cannot widen a CHECK; deferred foreign keys do not survive dropping the parent (tried and failed), so `foreign_key_check` runs before commit |
| Unknown speaker = flagged and voiced by the narrator | A confident wrong voice is worse than a visible flag |
| API keys saved from the web UI go to `config/secrets.json` (0600), write-only, in named slots (`secrets_store.py`) | Keeping them out of `settings.json` (returned by the API, copied around), the database and the audit log; one mechanism for every future OpenAI-compatible endpoint. Plaintext at rest, protected by file mode only, the same trust level as `auth.json` and the TLS key; a saved key is sent to whatever `llm.base_url` is effective, which only admins can change |
| `api_key_env` limited to `STUDIO_LLM_*` | Otherwise a settings admin could send any environment secret to the configured URL |
| Segments are synthesised model-first, assembled by index | Avoids a 30 s model reload per voice switch with three model families and two warm slots |

**v0.7.0 decisions**

| Decision | Reason |
|---|---|
| Clone voices get an **emotion reference bank** instead of an instruction | The Base model has no instruction input (the cloning path requires `instruct is None`), so the Director never reached clones: all 370 takes of the owner's clone narrator had `delivery_applied = 0`. The one lever the model has is its reference clip (the reference-emotion experiment, doc 7), so the Director now picks among a few clips of the same voice. Off by default, adopted clips only |
| Bank clips: Whisper WER <= 8%, level-matched, drift shown but not thresholded, consent per source | A clip that does not say the words poisons every take that uses it; there is no honest threshold for "still the same voice" without listening, so the numbers are advice; a synthetic clone may only receive clips from its own designed voice, a human clone only attested human recordings |
| **Scene-batched** Director (up to 16 segments per request) | One request per segment sent the same context again and again and made a 370-segment book a 370-request job. A window shares the scene summary, beats and surrounding text, results are validated item by item, and only failing items are asked again. Cache keys exclude the previous delivery so one edit does not invalidate the book |
| Chapter **beat sheet** before the scene pass, with thinking on and no JSON mode | A single reasoning pass over a whole chapter gives the emotional arc that per-segment prompts cannot see. Thinking plus JSON-constrained decoding degenerated on the owner's server, so this pass asks for JSON in plain text and tolerates failure (no plan = scene pass without it) |
| Beat sheets key on **paragraph numbers**, not offsets | Paragraphs survive re-segmentation, voice assignment and boundary edits; offsets do not. The cache key uses the chapter text hash, cast and facts |
| The LLM **translator is off by default** (`delivery.translator = template`) | It adds a second model, cost and a failure mode, and it is unproven that its wording sounds better than the template. The A/B tool (`llm_vs_template_translator`) exists to decide that. It runs ahead of time and `plan_for_segment` only reads the cache, so turning it on never makes planning slower or impure |
| Pace and pause hints default **off** (`pace_from_direction`, `pauses_from_direction`) | Existing takes must never change silently; they are in the generation key and digests, and *Apply new delivery settings to existing takes* is the explicit way to re-render |
| Pace factors are small (0.92 to 1.08) | The stretch is a pitch-preserving WSOLA that is only transparent for modest factors; larger factors would turn pacing into artefacts |
| **Escalation to a stronger model is deferred** | `would_escalate` is only counted. Without a real model and listening there is no evidence about which segments benefit, and a second model doubles the memory and failure surface on the Mac |
| Blind A/B instead of automatic scoring | Whisper and acoustic metrics say nothing about emotion; the only judge is a person who does not know which clip is which. Rounds use the same seed and explicit settings patches, so only the setting differs. The adoption guidance (60% of non-tie votes, other-narrator flags <= 10%) is advice shown to the owner, not enforced |
| Optional per-pass models (`chapter_model`, `scene_model`, `translator_model`), blank = main model | Lets the owner try a stronger model for the one slow pass and a small one for the many fast ones without a new provider concept. Whether a smaller scene model is good enough is unmeasured |

## Gotchas already hit (do not rediscover them)

- (v0.3) Patch scripts with backslashes passed through a shell heredoc corrupted regexes (`\b` became a backspace character). Use the Edit tool, a script file, or `chr(92)`.
- (v0.3) `promote` only accepts previews of **designed** voices; archive needs a JSON body (`{"archived": true}`); a book cannot be archived while any job (including `analyze`) is active.
- (v0.3) Mock fault keys match the spoken (normalised) text ("Mrs." is spoken "Missus").
- (v0.3) The first real-hardware run found that `redirect` set `stale` but the job never consumed it. `narrative_real.py` exists to catch what unit tests miss.

**LLM and Director (v0.6.3, v0.6.4, v0.7.0)**
- **Never hold the database write lock across an LLM request.** The Director once kept one transaction open for minutes; the worker heartbeat then hit `database is locked`, the lease went stale after 60 s,
  the web server killed and restarted the worker, and the same job repeated forever ("GPU worker offline"). Read in short sessions, call the model with no connection open, then write in one short transaction.
  The cache accessors are their own transactions for this reason. There is a regression test that fails if a lock is held during a request; keep new passes (chapter, scene, translator) under it.
- **Thinking + JSON mode degenerates on oMLX with Gemma-4-class models.** With `response_format: json_object` the model emitted `{"segment_` and ran to `max_tokens`; with it off it reasoned into
  `reasoning_content` until the budget was gone and `content` was empty. Ordinary requests therefore send `enable_thinking: false` with JSON mode; only the chapter pass thinks, and without JSON mode. A server that
  answers 400 to an optional field has it dropped and remembered; empty/cut-off/reasoning-only replies produce explicit errors, and Test connection fails for a model that only thinks.
- **`plan_for_segment` must stay pure and cheap.** It runs inside write transactions, web requests (inspector, A/B, apply-delivery dry run) and `invalidate_changed` over every rendered segment. It reads the translator
  cache with a plain SELECT, never calls an LLM, never writes, and never bumps a cache hit counter. Anything slow belongs in analysis ahead of time.
- A/B previews must use the same plan code as production (`plan_for_segment(..., ns_patch=...)` and the same time stretch), otherwise the clips would not represent real takes. `ns_patch` is whitelisted to
  `director_enabled` and the delivery keys.
- Bank row ids are never reused (`adopt_ref` takes max of `voice_refs.id` and `attempts.ref_id_used`, plus one), so `ref_id_used` always names one clip even after replacement or removal.
- `calibrated_wpm` reads stored durations that are already time-stretched; divide by `pace_used` (fixed in v0.7.0, it used to count the stretch twice).
- Scene cache keys must not include the previous delivery (it chains every scene to the one before); beat plans are cached only when valid, and a failure marker stops the same chapter being retried for each window.

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

## Narrative layer (v0.3) known limitations

- Speaker attribution is rules-based plus an optional LLM and is **not validated on real novels**. Weak spots: unattributed lines, pronoun-only tags with two same-gender characters (both flagged), and single-quote (British) dialogue (not detected).
- The rules Director reads lexical cues only and cannot understand context, irony or subtext. It is deterministic and labelled (`source: rules`). The LLM path is tested against stubs only.
- Emotion control exists only for CustomVoice 1.7B (and is appended to VoiceDesign descriptions). Base cloning cannot be directed. The audible effect of an instruction was not evaluated by listening.
- Very short segments (1-2 s) can come back truncated from the model and get flagged; separate voices make them more likely (`tags_with_speaker` and `min_segment_chars` reduce them).
- VoiceDesign voices are re-created on every call and may drift. Pitch consistency was measured on tiny samples only. Whisper pass/fail says nothing about emotion or speaker identity.
- A legacy book can be converted (it re-cuts every segment); semantic segmentation cannot be switched off afterwards. The import preview's segment count still uses the legacy chunker estimate.
- Summaries are extractive; an optional LLM summary stays unconfirmed. A downstream cascade never resets approved segments.
- `lan_e2e.py` and `lab_local.py` were not re-run for v0.3.0 (the smoke test and `narrative_real.py` were).

## v0.7.0 known limitations

- Everything new is tested with the mock engine and stub LLM servers only. Whether the reference bank, pace, pause hints, beat sheets, scene batching or the LLM translator improve narration is unmeasured (doc 7, "Verification status of v0.7.0").
- A clone without bank clips, a clone of a human voice (no way to generate candidates), and any segment below `bank_min_intensity` still use the single base reference and receive no emotion control.
- A clone's pace changes are the only direction effect that reaches it besides the bank; timbre can drift with a bad clip (use the A/B "another narrator" flag).
- Chapter beat sheets cost one long thinking request per chapter; time and token budget on a real model are unknown. A chapter with a single paragraph gets no plan.
- Escalation (asking a stronger model about low-confidence segments) is counted as `would_escalate` and not implemented.
- A/B votes are per book and per comparison, a small sample by nature; they are guidance for the owner, not a statistical test, and votes cast on mock tones are meaningless.
- Pause hints only lengthen gaps after sentence/paragraph segments; they never shorten configured pauses.

## Backlog / ideas (none are commitments)

0. (v0.7.0 follow-ups are in doc 11 section C.) Run a real local LLM (for example `mlx_lm.server`) through `narrative_real.py` and measure attribution and direction quality; a listening study of directed versus undirected output; single-quote dialogue; a CLI `analyze` command; ship the narrative metadata inside exports.

1. Attribute every audit/review action to the signed-in user.
2. Pronunciation dictionary (global and per book) feeding `override_text`; per-voice pace control.
3. Boot-time hosting (LaunchDaemon) and an "unattended" setup guide; optional reverse-proxy/HTTPS with a trusted certificate.
4. Delete/purge books and alternate takes (reclaim disk), backup and restore, a disk-usage dashboard.
5. Long-book validation run (50k words) with throughput, memory and false-positive statistics; tune thresholds from real data.
6. Per-chapter regeneration/export from the UI; chapter title editing; cover generation.
7. Multi-take reference comparison in the lab (side-by-side audition of several takes with the same passage and a score).
8. Test on Safari/Firefox; real-microphone validation; a CI pipeline.
9. Second deployment profile (M4 Max, 36 GB): confirm `max_warm_models=1` and memory headroom.

## v0.5 decisions, gotchas and backlog (installation and updates; details in doc 10)

- **Decisions.** Web app + private Python (uv, pinned and hash-checked) rather than a native app; Ed25519-signed index pinned in the installed app rather than
  trusting a host or a checksum; the updater is a separate launchd job that only the user can trigger; `state/` is the old "home" (so no data code changed);
  the library and model cache are pinned to their old absolute paths on migration (no copying 14 GB); network binding is turned OFF on migration unless asked.
- **Gotchas.** (1) Backslash escapes in Bash-tool heredocs corrupt source files (NUL and newline bytes): use Write/Edit. (2) `uv pip compile` for
  `aarch64-apple-darwin` assumes macOS 13 and rejects MLX, so the lock is `--universal`. (3) `httpx.Response(content=bytes)` is already "consumed";
  the fetcher serves such in-memory responses in slices (test doubles only). (4) `iter_bytes` re-chunks and loses data on a dropped connection; use `iter_raw`.
  (5) A missing `request.json` means "cancelled" only for runs that started with one. (6) Playwright's WebKit reports CSP stylesheet notices for `<select>`
  controls on the pre-existing Settings page too: not ours, but confirm in real Safari. (7) The worker only reports `idle` from `run_forever`; tests that
  drive `step()` by hand must set it (`drained()` reads it).
- **Open work.** Generate and pin the production key (nothing is signed yet); run doc 10's manual checks M1-M10 on a Mac; first signed release; optional
  `launchctl`-less restart for Linux; a Settings control for `updates.repository` is deliberately absent (edit `settings.json`; it is validated);
  provider selectors only when a second provider exists; Windows/Linux/containers are designed for, not built.

## Open questions for the owner

- When should the first MAJOR release (v1.0.0) be declared, and what acceptance criteria apply?
- Which licence terms (if any) should the product carry?
- Should operator accounts be allowed to edit voices or only use them?
