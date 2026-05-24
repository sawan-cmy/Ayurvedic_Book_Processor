from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from werkzeug.utils import secure_filename


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_pdf_name(filename: str) -> str:
    raw_name = Path(filename or "").name.strip() or "uploaded.pdf"
    name = secure_filename(raw_name)
    if not name or name.lower() == "pdf":
        name = f"uploaded_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    if len(name) > 180:
        stem = Path(name).stem[:176]
        name = f"{stem}.pdf"
    return name


def looks_like_pdf(pdf_file: Any) -> bool:
    try:
        position = pdf_file.stream.tell()
        header = pdf_file.stream.read(1024)
        pdf_file.stream.seek(position)
        return b"%PDF-" in header
    except Exception:
        return False


def clear_blocking_proxy_env() -> list[str]:
    """Remove proxy variables only when explicitly enabled or clearly local."""
    mode = os.getenv("CLEAR_PROXY_ENV", "local").strip().lower()
    if mode in {"0", "false", "no", "off", "never"}:
        return []

    clear_all = mode in {"1", "true", "yes", "on", "all"}
    local_markers = ("127.0.0.1", "localhost", "[::1]")
    removed: list[str] = []
    for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        value = os.environ.get(name, "")
        if value and (clear_all or any(marker in value.lower() for marker in local_markers)):
            os.environ.pop(name, None)
            removed.append(name)
    return removed
