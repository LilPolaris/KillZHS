"""Local data directory: atomic JSON files, plus loading of validated preferences."""

import copy
import json
import os
import threading
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..models import (
    AIConfig,
    InputError,
    NotificationConfig,
    Plan,
    RunSettings,
    parse_plan,
    parse_settings,
    unique_courses,
)

_JSON_LOCK = threading.RLock()


def load_json(path, fallback):
    with _JSON_LOCK:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return copy.deepcopy(fallback)


def atomic_json(path, value):
    """Replace a complete file, leaving its previous contents intact on failure.

    Each writer owns a temporary file in the destination directory. Readers see
    either complete version; independent writers cannot overwrite a shared .tmp.
    """
    path = Path(path)
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Windows cannot replace a destination while another thread is replacing or
    # reading it. The app's instance lock already excludes other app processes.
    with _JSON_LOCK:
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                    prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class Store:
    """Files under one data directory. A read-only store (demo mode) never writes."""

    def __init__(self, directory, *, writable=True):
        self.directory = Path(directory)
        self.writable = writable

    def path(self, name):
        return self.directory / name

    def load(self, name, fallback):
        return load_json(self.path(name), fallback)

    def save(self, name, value):
        if self.writable:
            atomic_json(self.path(name), value)

    def delete(self, name):
        if self.writable:
            self.path(name).unlink(missing_ok=True)

    def exists(self, name):
        return self.path(name).is_file()


class Preferences:
    """Run settings, AI, notifications and plan, kept together in ``config.json``.

    A damaged section falls back to its defaults without discarding the others.
    """

    FILE = "config.json"

    def __init__(self, store, course_keys):
        self.store = store
        raw = store.load(self.FILE, {})
        raw = raw if isinstance(raw, dict) else {}
        self.settings = self._section(lambda: parse_settings(raw["settings"]), RunSettings)
        self.ai = self._section(lambda: AIConfig.model_validate(raw["ai"]), AIConfig)
        self.notifications = self._section(lambda: NotificationConfig.model_validate(raw["notifications"]),
                                           NotificationConfig)
        self.plan = self._section(lambda: parse_plan(raw["plan"], course_keys), Plan)

    @staticmethod
    def _section(load, model):
        try:
            return load()
        except (KeyError, TypeError, ValueError, InputError):
            return model()

    def save(self):
        self.store.save(self.FILE, {"settings": self.settings.model_dump(), "ai": self.ai.model_dump(),
                                    "notifications": self.notifications.model_dump(), "plan": self.plan.model_dump()})


def load_courses(store):
    raw = store.load("courses.json", [])
    return unique_courses(raw)[0] if isinstance(raw, list) else []


def save_courses(store, courses):
    store.save("courses.json", [course.to_dict() for course in courses])
