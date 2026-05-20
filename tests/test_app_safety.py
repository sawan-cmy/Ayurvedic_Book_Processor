from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from collections.abc import Iterator
import shutil
import tempfile

import pytest
from flask import Flask
from werkzeug.datastructures import FileStorage

import app as app_module
import image_deck_generator
from utils import safe_pdf_name


@pytest.fixture()
def temp_workspace() -> Iterator[Path]:
    root = Path("C:/tmp")
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="ayurvedic-book-tests-", dir=root))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def isolated_app(temp_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Flask:
    monkeypatch.setenv("DISABLE_INLINE_WORKERS", "true")
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setattr(app_module, "ROOT", temp_workspace)
    monkeypatch.setattr(app_module, "JOBS_DIR", temp_workspace / "jobs")
    monkeypatch.setattr(app_module, "COMPLETED_DOCS_DIR", temp_workspace / "completed_docs")
    monkeypatch.setattr(app_module, "SLIDE_DECK_OUTPUTS_DIR", temp_workspace / "slide_deck_outputs")
    monkeypatch.setattr(app_module, "LOG_DIR", temp_workspace / "logs")
    monkeypatch.setattr(app_module, "ENV_FILE", temp_workspace / ".env")
    monkeypatch.setattr(app_module, "ENV_EXAMPLE", temp_workspace / ".env.example")
    monkeypatch.setattr(app_module, "JOBS_DB", temp_workspace / "jobs" / "jobs.db")
    monkeypatch.setattr(app_module, "JOBS_LOCK_FILE", temp_workspace / "jobs" / "jobs.db.lock")
    monkeypatch.setattr(app_module, "PROCESSOR", temp_workspace / "ultimate_book_processor.py")
    monkeypatch.setattr(app_module, "_stale_jobs_recovered", False)
    app_module.running_processes.clear()
    app_module.running_job_ids.clear()
    app_module.running_deck_generations.clear()
    app_module.dispatcher_started = False
    (temp_workspace / ".env").write_text("MAX_PARALLEL_JOBS=1\n", encoding="utf-8")
    app_module.ensure_project_files()
    return app_module.app


def pdf_upload(filename: str) -> FileStorage:
    return FileStorage(stream=BytesIO(b"%PDF-1.4\n%%EOF\n"), filename=filename, content_type="application/pdf")


def test_valid_job_id_rejects_malformed_prefix() -> None:
    assert app_module.valid_job_id("20260519_102030_abcdef12")
    assert not app_module.valid_job_id("_______________abcdef12")
    assert not app_module.valid_job_id("../20260519_102030_abcdef12")


def test_create_job_sanitizes_upload_path(isolated_app: Flask) -> None:
    job_id = app_module.create_job(pdf_upload(r"..\..\unsafe book.pdf"), uploaded_by="alice")
    job = app_module.job_by_id(job_id)
    assert job is not None
    assert job["filename"] == safe_pdf_name(r"..\..\unsafe book.pdf")
    saved_pdf = app_module.JOBS_DIR / job_id / "pdfs" / job["filename"]
    assert saved_pdf.exists()
    assert app_module.JOBS_DIR.resolve() in saved_pdf.resolve().parents


def test_delete_running_job_is_refused(isolated_app: Flask) -> None:
    job_id = app_module.create_job(pdf_upload("running.pdf"), uploaded_by="alice")
    app_module.update_job(job_id, status="running")

    deleted, message = app_module.delete_job(job_id)

    assert not deleted
    assert "Running jobs cannot be deleted" in message
    assert app_module.job_by_id(job_id)["status"] == "running"
    assert (app_module.JOBS_DIR / job_id).exists()


def test_concurrent_job_creation_keeps_all_rows(isolated_app: Flask) -> None:
    def create(index: int) -> str:
        return app_module.create_job(pdf_upload(f"book-{index}.pdf"), uploaded_by="alice")

    with ThreadPoolExecutor(max_workers=5) as executor:
        job_ids = list(executor.map(create, range(5)))

    jobs = app_module.load_jobs()
    assert len(jobs) == 5
    assert {job["id"] for job in jobs} == set(job_ids)


def test_upload_limits_queued_files_per_user(isolated_app: Flask) -> None:
    client = isolated_app.test_client()
    data = {
        "pdfs": [
            (BytesIO(b"%PDF-1.4\n%%EOF\n"), "one.pdf"),
            (BytesIO(b"%PDF-1.4\n%%EOF\n"), "two.pdf"),
            (BytesIO(b"%PDF-1.4\n%%EOF\n"), "three.pdf"),
            (BytesIO(b"%PDF-1.4\n%%EOF\n"), "four.pdf"),
        ]
    }

    response = client.post("/upload", data=data, content_type="multipart/form-data")

    assert response.status_code == 302
    assert len(app_module.load_jobs()) == app_module.MAX_QUEUED_PER_USER


def test_index_includes_sse_and_dark_mode(isolated_app: Flask) -> None:
    client = isolated_app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "EventSource('/events')" in html
    assert "themeToggle" in html


def test_count_pdf_pages_uses_pdfinfo_first(monkeypatch: pytest.MonkeyPatch, temp_workspace: Path) -> None:
    pdf_path = temp_workspace / "deck.pdf"
    pdf_path.write_bytes(b"not a real pdf")

    def fake_pdfinfo(path: str, **_: object) -> dict[str, int]:
        assert path == str(pdf_path)
        return {"Pages": 7}

    monkeypatch.setattr(image_deck_generator, "pdfinfo_from_path", fake_pdfinfo)

    assert image_deck_generator.count_pdf_pages(pdf_path) == 7
