"""Application context: resolved home directory, settings, database and logging helpers."""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

from .config import STORAGE_CATEGORIES, Settings, load_settings, save_settings
from .db import Database
from .secrets_store import SecretStore
from .util import dumps, now

DEFAULT_HOME = "~/audiobook-studio-claude"
log = logging.getLogger("studio")


def resolve_home() -> Path:
    env = os.environ.get("AUDIOBOOK_STUDIO_HOME")
    if env:
        return Path(env).expanduser().resolve()
    from .update.layout import Layout

    lay = Layout.discover()
    if lay.managed:  # managed installation: user data lives in <root>/state, apart from the replaceable releases
        return lay.state
    return Path(DEFAULT_HOME).expanduser().resolve()


class Context:
    def __init__(self, home: Path | None = None, migrate: bool = True):
        self.home = Path(home).expanduser().resolve() if home else resolve_home()
        self.config_dir = self.home / "config"
        self.data_dir = self.home / "data"
        self.logs_dir = Path(os.environ["AUDIOBOOK_STUDIO_LOGS"]).expanduser() if os.environ.get("AUDIOBOOK_STUDIO_LOGS") else self.home / "logs"
        self.run_dir = self.home / "run"
        self.cache_dir = self.home / "cache"
        self.backups_dir = self.home / "backups"
        self.app_dir = Path(__file__).resolve().parent.parent
        for d in (self.config_dir, self.data_dir, self.logs_dir, self.run_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.config_dir / "settings.json"
        self.db = Database(self.data_dir / "studio.db")
        self.secrets = SecretStore(self.config_dir / "secrets.json")
        self._settings: Settings | None = None
        self._settings_mtime: float | None = None
        self._lock = threading.Lock()
        if migrate:
            self.db.migrate(self.backups_dir / "db")
            if not self.settings_path.exists():
                save_settings(self.settings_path, Settings())
        try:  # every entry point (CLI, doctor, worker, server) must see the same configured model cache, before Hugging Face is imported
            self.apply_model_cache_env()
        except Exception:
            pass

    # ----------------------------------------------------------------- settings
    def apply_model_cache_env(self, force: bool = False) -> str | None:
        """Point Hugging Face at the user's model cache (settings.models.cache_dir) so weights live outside every release.

        The launcher (bin/audiobook-studio) sets HF_HOME *before* Python starts, which is the only reliable way: changing it after
        huggingface_hub has been imported leaves the library with two cache locations (weights from one, tokenizer from the other:
        "Tokenizer not loaded", found on the first Mac run). So this only acts while huggingface_hub is not imported yet, unless forced."""
        import sys

        d = self.settings().models.cache_dir
        if d and (force or "huggingface_hub" not in sys.modules):
            os.environ["HF_HOME"] = str(Path(d).expanduser())
        return os.environ.get("HF_HOME")

    def settings(self) -> Settings:
        with self._lock:
            try:
                mtime = self.settings_path.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            if self._settings is None or mtime != self._settings_mtime:
                self._settings = load_settings(self.settings_path)
                self._settings_mtime = mtime
            return self._settings

    def save_settings(self, settings: Settings) -> None:
        with self._lock:
            save_settings(self.settings_path, settings)
            self._settings = settings
            try:
                self._settings_mtime = self.settings_path.stat().st_mtime
            except FileNotFoundError:
                self._settings_mtime = None

    @property
    def engine_mode(self) -> str:
        env = os.environ.get("STUDIO_ENGINE", "").strip().lower()
        if env in ("mlx", "mock"):
            return env
        return self.settings().models.engine

    # ------------------------------------------------------------------ storage
    def library_root(self, settings: Settings | None = None) -> Path:
        s = settings or self.settings()
        raw = s.storage.library_root
        return Path(raw).expanduser() if raw else self.home / "library"

    def category_dir(self, category: str, settings: Settings | None = None) -> Path:
        if category not in STORAGE_CATEGORIES:
            raise ValueError(f"unknown storage category {category!r}")
        s = settings or self.settings()
        override = getattr(s.storage, f"{category}_dir")
        if override:
            return Path(override).expanduser()
        return self.library_root(s) / category

    def previews_dir(self) -> Path:
        d = self.data_dir / "previews"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def imports_dir(self) -> Path:
        d = self.data_dir / "imports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------ logging/audit
    def event(self, conn, kind: str, message: str, book_id: int | None = None, level: str = "info") -> None:
        conn.execute(
            "INSERT INTO events(kind, book_id, message, level, created_at) VALUES (?,?,?,?,?)",
            (kind, book_id, message[:2000], level, now()),
        )

    def audit(self, conn, entity: str, entity_id, action: str, detail: dict | None = None, actor: str = "operator") -> None:
        conn.execute(
            "INSERT INTO audit_log(entity, entity_id, action, detail_json, actor, created_at) VALUES (?,?,?,?,?,?)",
            (entity, None if entity_id is None else str(entity_id), action, dumps(detail or {}), actor, now()),
        )

    def job_log(self, conn, job_id: int | None, book_id: int | None, message: str, level: str = "info") -> None:
        conn.execute(
            "INSERT INTO job_logs(job_id, book_id, level, message, created_at) VALUES (?,?,?,?,?)",
            (job_id, book_id, level, message[:4000], now()),
        )
        log.log(logging.WARNING if level in ("warning", "error") else logging.INFO, "[job %s] %s", job_id, message)


def setup_logging(ctx: Context, name: str) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_studio", False) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler(ctx.logs_dir / f"{name}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    fh._studio = True  # type: ignore[attr-defined]
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh._studio = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    root.addHandler(sh)
