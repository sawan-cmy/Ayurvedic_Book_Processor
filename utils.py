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
    return name


def looks_like_pdf(pdf_file: Any) -> bool:
    try:
        position = pdf_file.stream.tell()
        header = pdf_file.stream.read(5)
        pdf_file.stream.seek(position)
        return header == b"%PDF-"
    except AttributeError:
        return False


def clear_blocking_proxy_env() -> None:
    """Remove proxy variables that block Google client calls on private hosts."""
    for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        os.environ.pop(name, None)
