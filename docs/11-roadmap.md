# 11. Roadmap and branch/channel policy (draft for owner review)

Status: **proposal**. Nothing here is a commitment, and items marked *needs owner decision* wait for an answer. Sizes: **S** under half a day,
**M** one to two days, **L** several days. Priorities are suggestions: P1 do soon, P2 next, P3 when wanted.

## 0. Where we are (v0.5.3, 2026-10-29)

Verified on the production M3 Max: managed install and migration, per-user launchd service, real MLX narration, signed updates (local-directory test
**and** the real GitHub release through the Update Center API: check, download, verify, stage, build of a new private Python environment with the
app-owned `uv`, install, health check, 11 s end to end), rollback, drain during real narration. The repository is public, history was scrubbed of
local network details, and releases are signed in CI with a protected key. Unverified: real Safari / Add to Dock, a pristine Mac with no developer tools,
a disk unplugged mid-job.

## 1. Branches, tags and update channels (proposal)

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
   existing release. Protect `main` (no force-push, require the CI check) and protect `v*` tags in repository settings.
5. Versioning rules are unchanged (MAJOR needs your acceptance; I bump MINOR and PATCH; CHANGELOG and version-pinned tests move with every bump).

*Needs owner decision:* adopt this model? If yes, as a first step I fast-forward `main` to the current verified release (v0.5.3), create `dev`, and add the
workflow guard. Until then `main` stays where it is.

## 2. Roadmap

### A. Finish and prove what v0.5 built
| # | Item | Size | Pri | Notes |
|---|---|---|---|---|
| A1 | Real Safari check: Add to Dock, Updates page, microphone on 127.0.0.1 | S | P1 | needs a GUI session on the Mac; I supply the checklist |
| A2 | Clean-Mac proof (no Command Line Tools, no git, no Homebrew) | M | P1 | second Mac, a fresh macOS user is not enough (CLT is system-wide), or a macOS VM |
| A3 | Disk unplugged mid-job on the real Mac | S | P2 | verify pause with `storage:` and automatic resume; Updates stays usable |
| A4 | Manual rollback: `audiobook-studio update --rollback` and a button in Updates | S | P1 | the previous release is already kept; today rollback is automatic only on a failed update |
| A5 | Publish a beta channel end to end (`-beta.N` tag, Mac opts in) | S | P2 | proves section 1 |
| A6 | Housekeeping: delete the downloaded tool archives after install, prune old runtimes with no release, tidy `app/downloads` | S | P2 | |
| A7 | Retire the legacy install on the Mac: after a soak period remove `~/audiobook-studio-claude` and the pre-v0.5 backup | S | P3 | needs your go-ahead; about 4 GB plus models if you also move the model cache |
| A8 | Regenerate documentation screenshots from a demo environment with placeholder names | S | P3 | removed in the scrub; `tools/e2e/shots.py` exists |

### B. Repository and release hygiene
| # | Item | Size | Pri | Notes |
|---|---|---|---|---|
| B1 | Add a `LICENSE` | S | P1 | *needs owner decision* (the repo is public and has none, so by default all rights are reserved) |
| B2 | Ask GitHub Support to purge the old unreachable commits (`98117b6`, `64c1b8d`) | S | P1 | only Support can; hashes are in the sweep notes |
| B3 | Branch and tag protection, required reviewers on the `release` environment, pin Actions to commit SHAs | S | P1 | owner-side settings; I can write the exact steps |
| B4 | Key hygiene: offline backup of the signing key, rotation rehearsal (add a second key, sign with it, drop the first) | M | P2 | procedure is in doc 10; never rehearsed |
| B5 | Public-repo scrub rule in CI: fail on private IPs, `.lan` hostnames, home paths and key blocks | S | P2 | turns today's manual sweep into a check |
| B6 | Move the commit email to the GitHub no-reply address | S | P3 | optional; applies to future commits only |
| B7 | SBOM and dependency-update routine (re-run `lock_requirements.py`, wheel check, smoke test on the Mac) | M | P3 | the lock is deliberately frozen at the verified versions |

### C. Product features (carried over from the earlier backlog)
| # | Item | Size | Pri | Notes |
|---|---|---|---|---|
| C1 | Attribute every audit/review action to the signed-in user | S | P1 | accounts exist now; today actions are logged as "operator" |
| C2 | Pronunciation dictionary (global and per book) plus per-voice pace | M | P1 | feeds `override_text`; the biggest quality lever left |
| C3 | Disk management: delete/purge books and takes, backup and restore, disk-usage dashboard | L | P1 | matters more with external libraries |
| C4 | Per-chapter regenerate/export, chapter-title editing, cover generation | M | P2 | |
| C5 | 50k-word validation run: throughput, memory, false-positive statistics; tune QA thresholds | M | P2 | uses real hardware time |
| C6 | Real local LLM through the Narrative Director (for example `mlx_lm.server`): measure attribution and direction quality, listening study | L | P2 | `tools/e2e/narrative_real.py` is ready |
| C7 | Multi-take reference comparison in the lab | M | P3 | |
| C8 | Boot-time hosting (LaunchDaemon) and trusted HTTPS | M | P3 | needs an administrator password once; only if the Mac must serve after a reboot with nobody logged in |
| C9 | Second deployment profile (M4 Max, 36 GB), `max_warm_models=1` | S | P3 | needs the hardware |

### D. Platform work (designed for, not built)
| # | Item | Size | Pri |
|---|---|---|---|
| D1 | Linux service and installer (`ProcessPlatformService` is the seed; CUDA/CPU providers) | L | P3 |
| D2 | Windows service and installer | L | P3 |
| D3 | Container image with tag-based upgrades (no in-container self-update) | M | P3 |
| D4 | Remote HTTP TTS/ASR providers and a provider/model selector in the UI (shown only when a second usable provider exists) | L | P3 |
| D5 | First-party HTTPS release host (`HttpsRepository` already implements the contract) | M | P3 |

### E. Questions only the owner can answer
1. Adopt the branch/channel model in section 1?
2. Which licence (B1)?
3. When is v1.0.0, and what are the acceptance criteria? (I will not bump MAJOR myself.)
4. Should operator accounts be allowed to edit voices, or only use them?
5. Is the Mac meant to serve the LAN permanently (today: yes, HTTP 8080 and HTTPS 8443, sign-in required)? If not, loopback-only is the safer default.

## 3. Suggested order
1. Decide E1 and E2, then B1-B3 and A4 (small, remove the sharpest edges).
2. A1, A2, A3 and A5 (finish proving v0.5 and the channel model; A1 and A2 need you or hardware).
3. C1, then C2 and C3 as the next feature releases (each a beta first, then promoted).
4. C5 and C6 when you have time for long hardware runs; D-items only on demand.
