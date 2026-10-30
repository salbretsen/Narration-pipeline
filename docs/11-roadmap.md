# 11. Roadmap and branch/channel policy

Status: **section 1 adopted by the owner and implemented in v0.5.5**; the rest is a proposal. Nothing else here is a commitment, and items marked *needs owner decision* wait for an answer. Sizes: **S** under half a day,
**M** one to two days, **L** several days. Priorities are suggestions: P1 do soon, P2 next, P3 when wanted.

## 0. Where we are (v0.5.3, 2026-10-29)

Verified on the production M3 Max: managed install and migration, per-user launchd service, real MLX narration, signed updates (local-directory test
**and** the real GitHub release through the Update Center API: check, download, verify, stage, build of a new private Python environment with the
app-owned `uv`, install, health check, 11 s end to end), rollback, drain during real narration. The repository is public, history was scrubbed of
local network details, and releases are signed in CI with a protected key. Unverified: real Safari / Add to Dock, a pristine Mac with no developer tools,
a disk unplugged mid-job.

## 1. Branches, tags and update channels (adopted, v0.5.5)

**Today.** Each change set lives on a version branch (`v0.5.3`), is tagged, and the owner is meant to merge it. Nobody has merged, so the public default
branch `main` still shows **v0.4.0** code, and the `stable` channel is decided only by the tag name (a `-` suffix means beta). The release workflow
triggers on any `v*` tag from any branch and does not check that the commit has been through the hardware checks.

**Proposed model** (the update client already supports two channels, so this is mostly workflow and discipline):

| | Branch | Tags | Channel the Mac sees | Who may publish |
|---|---|---|---|---|
| Stable | `main` | `vX.Y.Z` | `stable` | only a commit already on `main` |
| Beta | `dev` | `vX.Y.Z-beta.N` | `beta` (beta users also see stable) | any commit on `dev` |

1. Day-to-day work happens on `dev` (or short topic branches merged into it). Per-version branches stop being the unit of work.
2. A beta tag (`v0.6.0-beta.1`) is cut from `dev`; the production Mac can opt into the beta channel in Updates to run it.
3. **Promotion** to stable is a fast-forward of `main` to the verified `dev` commit, followed by the plain tag `v0.6.0`. The gate is the checklist in doc 10
   (tests, installer shellcheck, signed update through the real path, smoke test on the Mac). `main` therefore always equals "what stable users run".
4. Workflow guard: the Release workflow refuses a plain `vX.Y.Z` tag unless its commit is an ancestor of `origin/main`, and refuses to move or re-create an
   existing release. No GitHub branch or tag protection rules: the owner chose to keep setup simple, so the guard is the only enforcement.
5. Versioning is X.Y.Z: X = Major (owner only), Y = Feature (any new feature), Z = Minor/Patch (bug fix, feature alteration, docs, housekeeping). I bump Y and Z; CHANGELOG and version-pinned tests move with every bump.

**Done in v0.5.5 (locally, nothing pushed):** `main` fast-forwarded to the verified release v0.5.3, `dev` created at v0.5.5, workflow guard added.
Nothing else is required of the owner; pushing is done only when asked.

## 2. Roadmap

### A. Finish and prove what v0.5 built
| # | Item | Size | Pri | Notes |
|---|---|---|---|---|
| A1 | Real Safari check: Add to Dock, Updates page, microphone on 127.0.0.1 | S | P1 | needs a GUI session on the Mac; I supply the checklist |
| A2 | Clean-Mac proof (no Command Line Tools, no git, no Homebrew) | M | P1 | second Mac, a fresh macOS user is not enough (CLT is system-wide), or a macOS VM |
| A3 | Disk unplugged mid-job on the real Mac | S | P2 | verify pause with `storage:` and automatic resume; Updates stays usable |
| A4 | ~~Manual rollback~~ done v0.6.0 (CLI + Updates button; untested on the Mac) | S | done | the previous release is already kept; today rollback is automatic only on a failed update |
| A5 | Publish a beta channel end to end (`-beta.N` tag, Mac opts in) | S | P2 | proves section 1 |
| A6 | ~~Housekeeping~~ done v0.6.0 in the updater (installer-time cleanup of archives still open) | S | done | |
| A7 | Retire the legacy install on the Mac: after a soak period remove `~/audiobook-studio-claude` and the pre-v0.5 backup | S | P3 | needs your go-ahead; about 4 GB plus models if you also move the model cache |
| A8 | Regenerate documentation screenshots from a demo environment with placeholder names | S | P3 | removed in the scrub; `tools/e2e/shots.py` exists |

### B. Repository and release hygiene
| # | Item | Size | Pri | Notes |
|---|---|---|---|---|
| B1 | ~~Add a `LICENSE`~~ | S | done v0.5.7 | MIT (use allowed with the copyright notice kept) |
| B2 | Ask GitHub Support to purge the old unreachable commits (`98117b6`, `64c1b8d`) | S | P1 | only Support can; hashes are in the sweep notes |
| B3 | ~~Branch and tag protection~~ dropped by the owner (too much setup for a simple tool). Optional later: pin Actions to commit SHAs | S | P3 | no owner work |
| B4 | Key hygiene: offline backup of the signing key, rotation rehearsal (add a second key, sign with it, drop the first) | M | P2 | procedure is in doc 10; never rehearsed |
| B5 | ~~Public-repo scrub rule in CI~~ done v0.6.0 (`tools/scrub_check.py`) | S | done | turns today's manual sweep into a check |
| B6 | Move the commit email to the GitHub no-reply address | S | P3 | optional; applies to future commits only |
| B7 | SBOM and dependency-update routine (re-run `lock_requirements.py`, wheel check, smoke test on the Mac) | M | P3 | the lock is deliberately frozen at the verified versions |

### C. Product features (carried over from the earlier backlog)
| # | Item | Size | Pri | Notes |
|---|---|---|---|---|
| C1 | ~~Attribute audit/review actions to the signed-in user~~ done v0.6.0 | S | done | accounts exist now; today actions are logged as "operator" |
| C2 | ~~Pronunciation dictionary plus per-voice pace~~ done v0.6.0 (real speech unverified) | M | done | feeds `override_text`; the biggest quality lever left |
| C3 | ~~Disk management~~ done v0.6.0 | L | done | matters more with external libraries |
| C4 | ~~Per-chapter regenerate/export, title editing~~ done v0.6.0; generated cover not built (no Pillow) | M | done | |
| C5 | 50k-word validation run: throughput, memory, false-positive statistics; tune QA thresholds | M | P2 | uses real hardware time |
| C6 | Real local LLM through the Narrative Director (for example `mlx_lm.server`): measure attribution and direction quality, listening study | L | P2 | `tools/e2e/narrative_real.py` is ready |
| C10 | ~~Narrator versions~~, ~~admin delete of a version/book~~, ~~import form and review scroll fixes~~ done v0.7.0 (mock engine and Edge only; not on the Mac) | M | done | one manuscript, several narrators, each with its own audio and release |
| C11 | ~~Delivery for clone voices: emotion reference bank, pace and pause hints from the direction~~ done v0.7.0 (mock engine only) | L | done | off by default; effect on real speech unmeasured |
| C12 | ~~Director redesign: chapter beat sheet, scene batches, direct the picked segment, optional LLM translator, extra LLM models, thinking-model handling~~ done v0.7.0 (stub LLMs only) | L | done | includes the former 0.6.3 and 0.6.4 fixes |
| C13 | ~~Blind A/B listening tool and apply-to-existing-takes~~ done v0.7.0 (mock engine only; UI panels driven in Edge via `delivery_ui.py` on the mock engine) | M | done | |
| C18 | ~~Polish batch~~ done v0.8.0 (mock engine, stubs and headless Edge only; nothing on the Mac): live-update storm fixed, live-region repainting, router loading state, faster `get_book` and Disk & Backups, regenerate with direction, one finished row per book and pagination, processing-time estimates, tabbed Settings and wide-screen layout, Narrator Studio duplicate & retune | L | done | owner's polish list |
| C19 | **Compare processing-time estimates with real timings on the Mac**: run a real book, check "about ... left", "starts about" and "processed in" against the clock, tune the Director term and the minimum sample counts if they are off | S | P1 | the estimates have only been exercised with the mock engine and synthetic rows |
| C20 | **Fix the Book page horizontal overflow at 360 px** (a `nowrap` table in `book.js`) and re-run `polish_book.py` at phone width | S | P2 | known, unfixed in v0.8.0 |
| C21 | Browser-test what v0.8.0 did not: Queue drag-to-reorder, the Settings folder browser and Finder picker, API key Save / Test connection | S | P3 | extend `polish_queue.py` / `polish_settings.py`; the Finder picker only works from the Mac itself |
| C22 | Real-Mac and real-Safari pass over the v0.8.0 pages (live regions, router loading state, tabs, Narrator Studio dialogs) together with A1 | S | P1 | needs a GUI session on the Mac |
| C14 | **Mac listening A/B session**: run the four comparisons on real output (real clone with generated or recorded bank clips, a real LLM for the Director), apply the 60% / 10% rule per feature, then decide which defaults to flip | M | P1 | needs the Mac, a real LLM and an owner who listens; first real use of the v0.7.0 features |
| C15 | **Scene-model benchmark**: compare `llm.scene_model` candidates (smaller vs the main model) and the chapter pass on a real thinking model for time, validity rate, fallback rate and `would_escalate`, using `tools/e2e/narrative_real.py` extended or a one-off script | M | P2 | the chapter pass token budget (16000) and timeout (900 s) are guesses |
| C16 | **Escalation tier**: ask a stronger model about the segments `would_escalate` flags (low confidence, review, cue disagreement) and keep the better direction | M | P3 | deferred until C14/C15 show it is worth the memory and complexity |
| C17 | Reference bank follow-ups: candidates for human-voice clones from a recording checklist, more buckets if listening shows the four are too coarse, a drift threshold if listening shows where "another narrator" starts | M | P3 | |
| C7 | ~~Multi-take reference comparison~~ done v0.6.0 | M | done | |
| C8 | Boot-time hosting (LaunchDaemon) and trusted HTTPS | M | P3 | needs an administrator password once; only if the Mac must serve after a reboot with nobody logged in |
| C9 | Second deployment profile (M4 Max, 36 GB), `max_warm_models=1` | S | P3 | needs the hardware |

### D. Platform work (designed for, not built)
| # | Item | Size | Pri |
|---|---|---|---|
| D1 | Linux service and installer (`ProcessPlatformService` is the seed; CUDA/CPU providers) | L | P3 |
| D2 | Windows service and installer | L | P3 |
| D3 | ~~Container image definition~~ written v0.6.0, not built or tested | M | partly |
| D4 | Remote HTTP TTS/ASR providers and a provider/model selector in the UI (shown only when a second usable provider exists) | L | P3 |
| D5 | First-party HTTPS release host (`HttpsRepository` already implements the contract) | M | P3 |

### E. Questions only the owner can answer
1. ~~Adopt the branch/channel model in section 1?~~ Adopted.
2. ~~Which licence (B1)?~~ MIT.
3. When is v1.0.0, and what are the acceptance criteria? (I will not bump MAJOR myself.)
4. Should operator accounts be allowed to edit voices, or only use them?
5. ~~LAN serving?~~ Yes, default for new installs (v0.5.7); `--local-only` opts out.

## 3. Suggested order
1. Decide E2, then B1, B2 and A4 (small, remove the sharpest edges).
2. A1, A2, A3 and A5 (finish proving v0.5 and the channel model; A1 and A2 need you or hardware).
3. C1, then C2 and C3 as the next feature releases (each a beta first, then promoted).
4. C19 and C22 (first run of v0.8.0 on the Mac: estimates against the clock, Safari), then C14 (the first real listening session for v0.7.0), then C5, C6 and C15 when you have time for long hardware runs; C16 only if C14/C15 justify it; D-items only on demand.
