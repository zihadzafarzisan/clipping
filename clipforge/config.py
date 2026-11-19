from __future__ import annotations

import ctypes
import json
import os
import threading
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def protect(value: bytes, decrypt=False) -> bytes:
    """Windows DPAPI encrypts credentials for the current Windows user."""
    if os.name != "nt":
        return value
    buf = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), "ClipForge", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


class Settings:
    defaults = {
        "highlight_model": "gpt-6.2-sol", "whisper_model": "small",
        "language": "", "subtitle_prompt": "",
        "facebook_page_id": "", "facebook_api_version": "v27.0",
        "youtube_client_file": "", "youtube_privacy": "public",
        "tiktok_client_key": "", "tiktok_post_mode": "inbox", "tiktok_privacy": "SELF_ONLY",
        "sample_link": "https://www.youtube.com/live/irNhWMG560k",
    }

    def __init__(self, directory: Path = DATA):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.path = directory / "settings.json"
        self.vault_path = directory / "credentials.bin"
        self.values = dict(self.defaults)
        if self.path.exists():
            self.values.update(json.loads(self.path.read_text(encoding="utf-8")))
        self.secrets = {}
        if self.vault_path.exists():
            self.secrets = json.loads(protect(self.vault_path.read_bytes(), decrypt=True))

    def get(self, key, default=""):
        with self.lock:
            return self.values.get(key, default)

    def secret(self, key):
        with self.lock:
            return os.environ.get(key.upper()) or self.secrets.get(key, "")

    def save(self, values=None, secrets=None):
        with self.lock:
            self.values.update(values or {})
            self.secrets.update(secrets or {})
            atomic_json(self.path, self.values)
            temp = self.vault_path.with_suffix(".tmp")
            temp.write_bytes(protect(json.dumps(self.secrets).encode("utf-8")))
            temp.replace(self.vault_path)
            if os.name != "nt":
                self.vault_path.chmod(0o600)
