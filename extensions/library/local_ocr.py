"""Bounded local OCR. Image downloads use only a dedicated pinned CDN fetcher."""
from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from .model import LibraryError, normalized_text
from .sources import IMAGE_CDN, SafeFetcher

MAX_IMAGE = 4 * 1024 * 1024
MAX_OUTPUT = 200_000
MAX_ERROR = 4096


def _image_type(body: bytes, mime: str) -> str:
    mime = mime.split(";", 1)[0].strip().lower()
    signatures = {"image/png": body.startswith(b"\x89PNG\r\n\x1a\n"),
                  "image/jpeg": body.startswith(b"\xff\xd8\xff"),
                  "image/webp": body.startswith(b"RIFF") and body[8:12] == b"WEBP"}
    if not signatures.get(mime): raise LibraryError("image MIME or bytes are invalid", "image_blocked", 422)
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]


def _bounded_run(argv: list[str], folder: Path, timeout: float, env: dict[str, str] | None = None) -> str:
    """Read both pipes with hard caps; kill the process group on timeout/error."""
    try:
        process = subprocess.Popen(argv, cwd=folder, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError: raise LibraryError("local OCR engine is unavailable", "ocr_failed", 502) from None
    buffers = {"out": bytearray(), "err": bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "out")
    selector.register(process.stderr, selectors.EVENT_READ, "err")
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise LibraryError("local OCR timed out", "ocr_failed", 502)
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = buffers[key.data]
                target.extend(chunk)
                if len(target) > (MAX_OUTPUT if key.data == "out" else MAX_ERROR):
                    raise LibraryError("local OCR output exceeded limit", "ocr_failed", 502)
        if process.wait(timeout=max(0.1, deadline - time.monotonic())):
            fingerprint = buffers["err"].decode("ascii", "ignore").strip()
            if re.fullmatch(r"vision:[A-Za-z0-9.]+:-?[0-9]+", fingerprint):
                raise LibraryError("local OCR failed (" + fingerprint + ")", "ocr_failed", 502)
            raise LibraryError("local OCR failed", "ocr_failed", 502)
        return buffers["out"].decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError):
        raise LibraryError("local OCR failed", "ocr_failed", 502) from None
    finally:
        selector.close()
        if process.poll() is None:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait(timeout=2)
        process.stdout.close(); process.stderr.close()


def local_ocr_runner(body: bytes, suffix: str, *, swift_path: Path = Path("/usr/bin/swift"),
                     which=shutil.which, run=_bounded_run) -> tuple[str | None, str]:
    """Prefer installed Tesseract, then system Swift Vision; no installation."""
    swift = swift_path
    with tempfile.TemporaryDirectory(prefix="library-ocr-") as raw_folder:
        folder = Path(raw_folder); folder.chmod(0o700)
        image = folder / ("image" + suffix)
        image.write_bytes(body); image.chmod(0o600)
        tesseract = which("tesseract")
        if tesseract:
            try:
                return run([tesseract, str(image), "stdout", "-l", "chi_sim+eng"], folder, 25), "tesseract"
            except LibraryError:
                if not swift.is_file(): raise
        if swift.is_file():
            cache = folder / "cache"; cache.mkdir(mode=0o700)
            env = {**os.environ, "SWIFT_MODULE_CACHE_PATH": str(cache),
                   "CLANG_MODULE_CACHE_PATH": str(cache), "TMPDIR": str(folder)}
            script = Path(__file__).with_name("vision_ocr.swift")
            return run([str(swift), str(script), str(image)], folder, 60, env), "vision"
        return None, "unavailable"


class LocalOCR:
    def __init__(self, fetcher: SafeFetcher, runner=local_ocr_runner):
        if fetcher.guard.allowed_hosts != IMAGE_CDN or not fetcher.guard.secure_only:
            raise LibraryError("OCR fetcher must use the exact official image CDN allowlist", "config_invalid", 409)
        self.fetcher, self.runner = fetcher, runner

    def image(self, url: str) -> dict:
        if urlsplit(url).hostname not in IMAGE_CDN or urlsplit(url).scheme != "https":
            raise LibraryError("image host is not allowed", "image_blocked", 422)
        final, headers, body = self.fetcher.get(url, MAX_IMAGE)
        if urlsplit(final).hostname not in IMAGE_CDN or urlsplit(final).scheme != "https":
            raise LibraryError("image redirect escaped CDN", "image_blocked", 422)
        suffix = _image_type(body, headers.get("Content-Type") or headers.get("content-type") or "")
        result = self.runner(body, suffix)
        if isinstance(result, tuple) and len(result) == 2:
            text, method = result
        else:  # injected test runners from v0.2 adapters return only text or None
            text, method = result, "tesseract" if result is not None else "unavailable"
        if method not in {"vision", "tesseract", "unavailable"} or (text is not None and not isinstance(text, str)):
            raise LibraryError("local OCR result is invalid", "ocr_failed", 502)
        clean = normalized_text(text or "")[:MAX_OUTPUT]
        return {"text": clean, "method": method, "image_url": final,
                "status": "complete" if clean else "needs_ocr"}
