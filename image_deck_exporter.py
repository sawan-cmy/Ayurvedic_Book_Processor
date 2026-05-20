from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image


def normalize_source_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def save_slides_json(deck: dict[str, Any], output_dir: Path) -> Path:
    path = output_dir / "slides.json"
    path.write_text(json.dumps(deck, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def create_prompt_used_report(prompt_name: str, prompt_text: str, output_dir: Path) -> Path:
    path = output_dir / "prompt_used.md"
    lines = [
        "# Prompt Used",
        "",
        f"- prompt profile: {prompt_name}",
        "",
        "```text",
        prompt_text.strip(),
        "```",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def create_pdf(slide_paths: list[Path], output_dir: Path) -> Path:
    if not slide_paths:
        raise RuntimeError("No slide PNG files were created.")
    images = [Image.open(path).convert("RGB") for path in slide_paths]
    pdf_path = output_dir / "ayurveda_slide_deck.pdf"
    first, rest = images[0], images[1:]
    first.save(pdf_path, save_all=True, append_images=rest)
    for image in images:
        image.close()
    return pdf_path


def create_pdf_from_slides(slide_png_paths: list[Path], output_dir: Path) -> Path:
    return create_pdf(slide_png_paths, output_dir)


def create_editable_text(deck: dict[str, Any], output_dir: Path) -> Path:
    lines = ["# Editable Text For Canva", ""]
    for slide in deck.get("slides", []):
        lines.extend(
            [
                f"## Slide {slide.get('slide_number', '')}",
                "",
                f"Title: {slide.get('title', '')}",
                "",
            ]
        )
        exact_text = str(slide.get("exact_text", "") or "").strip()
        if exact_text:
            lines.extend(["Exact text:", exact_text, ""])
        shloka = str(slide.get("shloka", "") or "").strip()
        if shloka:
            lines.extend(["Shloka:", shloka, ""])
        if slide.get("main_points"):
            lines.append("Main points:")
            for point in slide.get("main_points", []) or []:
                lines.append(f"- {point}")
            lines.append("")
        lines.extend(["", "Labels:"])
        for label in slide.get("labels", []) or []:
            lines.append(f"- {label}")
        lines.extend(["", f"Teacher review: {bool(slide.get('needs_teacher_review', False))}", ""])
    path = output_dir / "editable_text_for_canva.md"
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path


def create_editable_text_for_canva(deck: dict[str, Any], output_dir: Path) -> Path:
    return create_editable_text(deck, output_dir)


def create_source_coverage_report(
    source_text: str,
    chunks: list[str],
    deck: dict[str, Any],
    validation_errors: list[str],
    output_dir: Path,
    png_count: int,
    pdf_page_count: int,
) -> tuple[Path, str, list[str]]:
    slides = deck.get("slides", []) or []
    slide_texts = [str(slide.get("exact_text", "") or "") for slide in slides]
    normalized_source_text = normalize_source_text(source_text)
    reconstructed_slide_text = "".join(slide_texts)
    coverage_errors: list[str] = []
    if reconstructed_slide_text != normalized_source_text:
        mismatch_at = next(
            (
                index
                for index, (source_char, slide_char) in enumerate(
                    zip(normalized_source_text, reconstructed_slide_text)
                )
                if source_char != slide_char
            ),
            min(len(normalized_source_text), len(reconstructed_slide_text)),
        )
        coverage_errors.append(
            "Reconstructed slide text does not exactly match source text "
            f"(source chars: {len(normalized_source_text)}, slide chars: {len(reconstructed_slide_text)}, "
            f"first mismatch at char {mismatch_at})."
        )
    missing_chunks: list[tuple[str, str]] = []
    for index, chunk in enumerate(chunks, start=1):
        if not any(chunk == slide_text for slide_text in slide_texts):
            preview = chunk[:160].replace("\n", " ")
            missing_chunks.append((f"chunk_{index:03d}", preview))
    dummy_errors: list[str] = []
    for index, text in enumerate(slide_texts, start=1):
        if "1, 2, 3" in text and "1, 2, 3" not in source_text:
            dummy_errors.append(f"slide {index} contains dummy text: 1, 2, 3")
    if len(chunks) > 1 and len(slides) <= 1:
        dummy_errors.append("Long/multi-chunk source generated only one slide.")
    if pdf_page_count != png_count:
        dummy_errors.append(f"PDF page count {pdf_page_count} does not match PNG count {png_count}.")
    coverage_errors.extend(error for error in dummy_errors if error not in coverage_errors)

    if missing_chunks:
        coverage_errors.extend(
            f"{chunk_id} is missing from slides.json: {preview}" for chunk_id, preview in missing_chunks
        )

    final_status = "FAIL" if validation_errors or coverage_errors else "PASS"
    lines = [
        "# Source Coverage Report",
        "",
        f"- source_char_count: {len(normalized_source_text)}",
        f"- slide_text_char_count: {len(reconstructed_slide_text)}",
        f"- chunk_count: {len(chunks)}",
        f"- slide_count: {len(slides)}",
        f"- PNG count: {png_count}",
        f"- PDF page count: {pdf_page_count}",
        f"- final status: {final_status}",
        "",
        "## Missing Chunks",
    ]
    if missing_chunks:
        lines.extend(f"- {chunk_id}: {preview}" for chunk_id, preview in missing_chunks)
    else:
        lines.append("- none")
    lines.extend(["", "## Coverage Errors"])
    if coverage_errors:
        lines.extend(f"- {error}" for error in coverage_errors)
    else:
        lines.append("- none")
    path = output_dir / "source_coverage_report.md"
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path, final_status, coverage_errors


def create_review_report(
    job_id: str,
    source_files: list[Path],
    deck: dict[str, Any],
    validation_errors: list[str],
    warnings: list[str],
    output_dir: Path,
    source_debug: dict[str, Any] | None = None,
) -> Path:
    review_slides = [
        str(slide.get("slide_number", index + 1))
        for index, slide in enumerate(deck.get("slides", []) or [])
        if slide.get("needs_teacher_review")
    ]
    final_status = "FAIL" if validation_errors else ("NEEDS_REVIEW" if warnings or review_slides else "PASS")
    lines = [
        "# Ayurveda Slide Deck Review Report",
        "",
        f"- job id: {job_id}",
        f"- generation date: {datetime.now().isoformat(timespec='seconds')}",
        f"- slide count: {len(deck.get('slides', []) or [])}",
        f"- final status: {final_status}",
        "",
        "## Source Files Used",
    ]
    if source_files:
        lines.extend(f"- {path}" for path in source_files)
    else:
        lines.append("- none")
    if source_debug:
        lines.extend(["", "## Debug Values"])
        for key in [
            "job_id",
            "job_dir",
            "prompt_profile",
            "prompt_engine_status",
            "source_location_used",
            "source_char_count",
            "source_word_count",
            "chunk_count",
            "slide_count",
            "png_count",
            "pdf_page_count",
            "zip_path",
            "coverage_status",
            "final_status",
            "slide_count_reason",
        ]:
            if key in source_debug:
                lines.append(f"- {key}: {source_debug.get(key)}")
        lines.extend(["", "## Source Folders Checked"])
        for folder in source_debug.get("source_folders_checked", []) or []:
            lines.append(f"- {folder}")
        lines.extend(["", "## Files Loaded"])
        loaded = source_debug.get("files_loaded", []) or []
        if loaded:
            lines.extend(f"- {path}" for path in loaded)
        else:
            lines.append("- none")
    lines.extend(["", "## Validation Errors"])
    if validation_errors:
        lines.extend(f"- {error}" for error in validation_errors)
    else:
        lines.append("- none")
    lines.extend(["", "## Warnings"])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- none")
    lines.extend(["", "## Slides Needing Teacher Review"])
    if review_slides:
        lines.extend(f"- slide {slide_number}" for slide_number in review_slides)
    else:
        lines.append("- none")
    path = output_dir / "review_report.md"
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path


def create_zip(output_dir: Path, zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output_dir.parent))
    return zip_path
