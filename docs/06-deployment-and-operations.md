# 6. Deployment and operations

## The production Mac

| | |
|---|---|
| Host | `<mac-host>` (LAN IP **<mac-lan-ip>**), MacBook Pro **M3 Max, 48 GB**, macOS 27 (arm64) |
| Login user | `<user>` |
| SSH | `ssh -i ~/.ssh/<ssh-key> -o IdentitiesOnly=yes -o BatchMode=yes <user>@<mac-host>` (key-based, non-interactive; the key stays in the owner's `~/.ssh`, never in the repo) |
| Install dir | `/Users/<user>/audiobook-studio-claude` (`AUDIOBOOK_STUDIO_HOME` default) |
| URLs | `http://<mac-host>:8080` · `https://<mac-host>:8443` (self-signed; needed for the microphone) · also by IP |
| Bind | `0.0.0.0` (`server.allow_remote=true`, `https_enabled=true`, `https_port=8443`); sign-in is mandatory |
| Toolchain | Homebrew in `/opt/homebrew` (**installed and managed by the owner**), `python@3.12` (venv Python 3.12.16), FFmpeg 9.0.2, `mlx 0.32.4`, `mlx-audio 0.6.3`, `mlx-whisper 0.4.3`, `transformers 5.21` |
| Models | cached under `<home>/cache/huggingface` (about 13.8 GB: CustomVoice, Base, VoiceDesign 1.7B bf16 + whisper-large-v3-turbo) |
| Firewall | Not managed by this project: do not change the firewall, Homebrew or unrelated software |
| Accounts | the owner's `admin` (password known only to the owner). Create temporary accounts for testing and remove them (below) |

## Redeploying (the standard procedure)

From the dev machine, after building the installer (doc 5):

```bash
K="$HOME/.ssh/<ssh-key>"; H=<user>@<mac-host>
scp -q -i $K -o IdentitiesOnly=yes -o BatchMode=yes install_audiobook_studio_claude.sh $H:~/install_audiobook_studio_claude.sh
ssh -i $K -o IdentitiesOnly=yes -o BatchMode=yes $H \
  '/bin/bash ~/install_audiobook_studio_claude.sh --stop-running --lan --install-service --no-models --skip-tests; rm -f ~/install_audiobook_studio_claude.sh'
```

Then verify: `./studio-service status` on the Mac, and from the dev machine
`curl http://<mac-host>:8080/api/auth/status` (version, engine `mlx`) and `curl -k https://...:8443/api/auth/status`;
an unauthenticated `GET /api/overview` must return `401`.

What a re-run does: stops the service, unpacks the new code to a staging directory and verifies `MANIFEST.sha256`, reuses the venv
(rebuilt only if the Python version changed), reinstalls requirements, moves the old `app/` to `backups/app-<timestamp>/` (keeps 5),
swaps in the new code and rolls back on failure, runs `studio migrate` (DB backup first), merges settings (existing values kept),
keeps existing accounts (creates the first admin only when no user exists), reinstalls the LaunchAgent and starts it. It never touches
`config/`, `data/`, `library/`, `cache/` or exports. Do **not** pass `--no-models` on a first-ever install if you want the weights
prefetched (about 6 GB; Base and VoiceDesign are fetched lazily or with `--prefetch-all`).

Installer options: `--no-models --prefetch-all --skip-tests --port N --stop-running --lan --no-https --host ADDR --admin-user NAME
--install-service --extract-only DIR -h`. Environment: `AUDIOBOOK_STUDIO_HOME`, `STUDIO_PYTHON`, `STUDIO_MLX_AUDIO_SPEC`,
`AUDIOBOOK_STUDIO_ALLOW_NON_MAC=1` (CI mock install on Linux). `--lan` = bind 0.0.0.0, port 8080, HTTPS on 8443, create the first admin
with a random password printed once and saved to `config/initial-admin-password.txt` (mode 0600; delete after storing the password).

## The service

LaunchAgent `com.audiobookstudio.claude`, plist `~/Library/LaunchAgents/com.audiobookstudio.claude.plist`, launched in domain
`gui/<uid>` (falls back to `user/<uid>`). It runs `venv/bin/python -m studio serve` with `RunAtLoad` and `KeepAlive` (10 s throttle,
45 s exit timeout), the venv environment, `HF_HOME`, telemetry off, and a PATH including `/opt/homebrew/bin`. Logs: `logs/launchd.out.log`
(stdout/stderr), `logs/server.log`, `logs/worker.log`, `logs/worker.out.log`.

```bash
cd ~/audiobook-studio-claude
./studio-service status | start | stop | restart | logs | install | uninstall
./stop-studio                 # stops the service (so KeepAlive does not restart it) and any foreground processes
./studio doctor               # environment check
```

- It keeps running after the SSH session ends. **A LaunchAgent only runs while the user is logged in** (GUI login or auto-login).
  For hosting that must survive a reboot with nobody logged in, enable automatic login for `<user>`, or install a
  LaunchDaemon: `./studio-service daemon-plist` prints the plist and the `sudo` commands (needs an administrator password; **not done**).
- macOS privacy (TCC) can block a background service from reading `~/Documents`, `~/Desktop` or external volumes until allowed in
  System Settings > Privacy & Security. The folder browser reports "macOS denied access ..." in that case.
- `./studio run` (foreground queue) refuses to start while the web worker holds the lease.

## Accounts and TLS

```bash
./studio auth list
./studio auth add-user NAME --role operator --generate     # prints a random password once; tell the owner
./studio auth set-password NAME --role admin               # prompts twice
./studio auth remove-user NAME                             # also ends their sessions; the last admin cannot be removed
```
`config/auth.json` (0600) holds salted PBKDF2 hashes and the session-signing secret. The certificate lives in `config/tls/`
(regenerated automatically if the host name/IP set changes or it nears expiry; fingerprint shown by `/api/auth/status`). Sign-in cookies
are `Secure` only over HTTPS; over the plain HTTP address passwords cross the LAN unencrypted (use the HTTPS address).

## Data, backups, storage

- Everything of value: `config/`, `data/studio.db` (+ `-wal`/`-shm`), `library/` (or wherever the library root points), `cache/` (re-downloadable).
  Back up `config/`, `data/`, and the library together. DB backups before migrations are in `backups/db/`.
- Library root is changed in the UI (Settings → Storage → Browse…). New books use the new root; old books keep their recorded paths
  (move them explicitly). Volumes seen on the Mac include external disks under `/Volumes` (for example an SSD).
  A disconnected drive pauses its books (reason `storage:`) and they resume automatically.
- Test artefacts must be cleaned: archive test books and narrators (API, admin), delete recordings (`DELETE /api/recordings/{id}`),
  remove temporary users. Archived books keep their audio on disk (there is no delete-data feature yet).

## Real-engine behaviour on this hardware (measured)

CustomVoice preset RTF about 0.33-0.64; VoiceDesign about 0.35; clone (Base) about 0.4 warm (about 1.2 on the first call that includes
load); a model loads in about 30-35 s; peak GPU memory 8-15 GB (two warm models by default). A 500-word, 2-chapter manuscript renders
in about 2 minutes. First installs download about 6 GB (default) to 14 GB (all models).

## Troubleshooting

| Symptom | Check |
|---|---|
| UI says "GPU worker offline" | `./studio-service status`; `tail logs/launchd.out.log logs/worker.log`; Settings/About → Restart worker |
| Service not running after reboot | not logged in (LaunchAgent), see above |
| `421 Invalid Host` | the name used is not in the certificate/allow-list: add it to `server.allowed_hosts` in `config/settings.json`, restart |
| Microphone unavailable in the lab | page is plain http on the network: use the https URL (or localhost on the Mac) |
| Browser certificate warning | expected for the self-signed cert; trust it (download from the lab notice) |
| `503 ... without any user account` | no users exist while bound to the network: `./studio auth add-user NAME --role admin --generate` |
| Locked out | on the Mac: `./studio auth set-password admin --role admin` |
| Models "not downloaded" in doctor | they download on first use unless `models.allow_downloads=false`; re-run with `--prefetch-all` |
| Job paused with `storage:` | the book's directory/volume is unavailable; fix and it resumes by itself |
| A flagged segment keeps flagging | by design: review it (override spoken text, regenerate, pick a take, or approve) |
