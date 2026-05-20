from __future__ import annotations

import logging
import math
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from pdf2image import pdfinfo_from_path
from typing import Any

from utils import clear_blocking_proxy_env, now_text
from image_deck_exporter import (
    create_editable_text,
    create_pdf,
    create_prompt_used_report,
    create_review_report,
    create_source_coverage_report,
    create_zip,
    save_slides_json,
)
from image_deck_prompts import MASTER_PROMPT_NAME, SLIDE_JSON_PROMPT, VISUAL_PLAN_PROMPT
from image_deck_renderer import render_slides


DEFAULT_DECK_TITLE = "आयुर्वेद स्लाइड डेक"
DEFAULT_DECK_MODE = "exact_document_slide_deck"
MAX_CHARS_PER_SLIDE = 650
ROMAN_TEXT_RE = re.compile(r"[A-Za-z]")
DEVANAGARI_DIGIT_RE = re.compile(r"[\u0966-\u096F]")


def normalize_source_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def write_deck_state(output_dir: Path, **updates: Any) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "deck_state.json"
    state = {
        "job_id": output_dir.name,
        "status": "not_started",
        "source_char_count": 0,
        "source_word_count": 0,
        "chunk_count": 0,
        "slide_count": 0,
        "rendered_slide_count": 0,
        "pdf_created": False,
        "zip_created": False,
        "coverage_status": "UNKNOWN",
        "prompt_profile": MASTER_PROMPT_NAME,
        "prompt_engine_status": "not_started",
        "latest_error": "",
        "started_at": "",
        "finished_at": "",
    }
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception:
            pass
    state.update(updates)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


@dataclass
class SourceBundle:
    text: str
    files: list[Path]
    checked_locations: list[Path] = field(default_factory=list)
    source_location: Path | None = None
    needs_review: bool = False
    warning: str = ""


def source_word_count(text: str) -> int:
    return len([word for word in re.split(r"\s+", text.strip()) if word])


def master_prompt_source_warnings(source_text: str) -> list[str]:
    warnings: list[str] = []
    if ROMAN_TEXT_RE.search(source_text):
        warnings.append(
            "Master prompt hard-lock conflict: the verified source contains Roman/English characters. "
            "The exact-content deck preserves them, so a no-English infographic version needs teacher review."
        )
    if DEVANAGARI_DIGIT_RE.search(source_text):
        warnings.append(
            "Master prompt hard-lock conflict: the verified source contains Devanagari numerals, "
            "but the prompt requires English numerals only."
        )
    return warnings


def prompt_engine_enabled() -> bool:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).with_name(".env"))
    except Exception:
        pass
    value = os.getenv("USE_PROMPT_ENGINE", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def load_prompt_api_key() -> str | None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).with_name(".env"))
    except Exception:
        pass
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("Prompt engine response did not contain a JSON object.")
    decoder = json.JSONDecoder(strict=False)
    data, _ = decoder.raw_decode(cleaned[start:])
    if not isinstance(data, dict):
        raise ValueError("Prompt engine response JSON was not an object.")
    return data


def build_prompt_engine_request(chunks: list[str], start_index: int = 1) -> str:
    chunk_payload = [
        {
            "slide_number": start_index + offset,
            "source_chunk_id": f"chunk_{start_index + offset:03d}",
            "exact_text": chunk,
        }
        for offset, chunk in enumerate(chunks)
    ]
    return (
        f"{VISUAL_PLAN_PROMPT}\n\n"
        "## APP OUTPUT FORMAT\n\n"
        "Return strict JSON only, with no Markdown fences.\n\n"
        "Required JSON shape:\n"
        "{\n"
        '  "prompt_profile": "FINAL_MASTER_PROMPT_AYURVEDA_INFOGRAPHIC_ENGINE",\n'
        '  "slides": [\n'
        "    {\n"
        '      "source_chunk_id": "chunk_001",\n'
        '      "slide_title": "Short Hindi title in Devanagari",\n'
        '      "visual_theme": "water_channel|thirst|edema|ascites|electrolyte|bone|srotas|dosha|general",\n'
        '      "visual_brief": "internal English description, not visible on slide",\n'
        '      "image_prompt": "detailed English prompt for text-free medical illustration",\n'
        '      "visual_clusters": ["Devanagari label from source only"],\n'
        '      "comparison_view": false,\n'
        '      "needs_teacher_review": false\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Do not rewrite, summarize, translate, or remove exact_text. "
        "The renderer will preserve exact_text separately. "
        "For visual_clusters, use only Devanagari words that already appear in exact_text; "
        "leave the list empty if unsure. "
        "For image_prompt, describe a TEXT-FREE medical illustration: anatomy, pathology, "
        "3D icons, composition. Always end with: Osmosis.org style, soft shadows, clinical "
        "pastel colors, white background. No text, no labels, no numbers. "
        "If the source chunk contains Roman/English text, set needs_teacher_review true.\n\n"
        "## SOURCE CHUNKS\n\n"
        f"{json.dumps(chunk_payload, ensure_ascii=False, indent=2)}"
    )


def generate_prompt_visual_plan(chunks: list[str]) -> tuple[dict[str, Any] | None, list[str], str]:
    if not prompt_engine_enabled():
        return None, [], "disabled"
    api_key = load_prompt_api_key()
    if not api_key:
        return None, ["Prompt engine was not used because GEMINI_API_KEY/GOOGLE_API_KEY is missing."], "missing_api_key"
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        return None, [f"Prompt engine was not used because google-genai is unavailable: {exc}"], "unavailable"

    model = os.getenv("SLIDE_PROMPT_MODEL", "gemini-2.5-flash")
    batch_size = max(1, int(os.getenv("PROMPT_ENGINE_BATCH_SIZE", "4")))
    try:
        clear_blocking_proxy_env()
        timeout_seconds = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "180"))
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
        )
        merged_slides: list[dict[str, Any]] = []
        batch_warnings: list[str] = []
        for start in range(0, len(chunks), batch_size):
            prompt = build_prompt_engine_request(chunks[start : start + batch_size], start_index=start + 1)
            try:
                response = client.models.generate_content(model=model, contents=prompt)
                plan = extract_json_object(response.text or "")
                plan_slides = plan.get("slides", [])
                if not isinstance(plan_slides, list):
                    raise ValueError(f"batch returned no valid slides list")
                merged_slides.extend(slide for slide in plan_slides if isinstance(slide, dict))
            except Exception as exc:
                batch_warnings.append(f"Prompt engine batch starting at slide {start + 1} failed: {exc}")
    except Exception as exc:
        return None, [f"Prompt engine failed and exact-content fallback was used: {exc}"], "failed"
    if not merged_slides:
        return None, batch_warnings or ["Prompt engine returned no visual plans."], "failed"
    status = "partial" if batch_warnings else "used"
    return {"prompt_profile": MASTER_PROMPT_NAME, "slides": merged_slides}, batch_warnings, status


def apply_prompt_visual_plan(deck: dict[str, Any], plan: dict[str, Any] | None) -> list[str]:
    if not plan:
        return []
    plan_slides = plan.get("slides", [])
    if not isinstance(plan_slides, list):
        return ["Prompt engine response did not include a valid slides list."]
    by_chunk_id = {
        str(slide.get("source_chunk_id", "")): slide
        for slide in plan_slides
        if isinstance(slide, dict)
    }
    warnings: list[str] = []
    for slide in deck.get("slides", []) or []:
        if not isinstance(slide, dict):
            continue
        source_chunk_id = str(slide.get("source_chunk_id", ""))
        plan_slide = by_chunk_id.get(source_chunk_id)
        if not plan_slide:
            warnings.append(f"Prompt engine did not return visual plan for {source_chunk_id}.")
            slide["needs_teacher_review"] = True
            continue
        for key in ["visual_theme", "visual_brief", "visual_clusters", "comparison_view", "image_prompt", "slide_title"]:
            if key in plan_slide:
                slide[key] = plan_slide[key]
        if plan_slide.get("needs_teacher_review"):
            slide["needs_teacher_review"] = True
    return warnings


def source_sort_key(path: Path) -> tuple[str, int, str]:
    match = re.search(r"(\d+)", path.stem)
    page_number = int(match.group(1)) if match else 999999
    return (str(path.parent).lower(), page_number, path.name.lower())


def read_text_files(directory: Path, patterns: list[str]) -> tuple[str, list[Path]]:
    if not directory.exists():
        return "", []
    files: list[Path] = []
    for pattern in patterns:
        files.extend(path for path in directory.rglob(pattern) if path.is_file())
    files = sorted(files, key=source_sort_key)
    pieces: list[str] = []
    used_files: list[Path] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
        except Exception as exc:
            logging.warning("Could not read source file %s: %s", path, exc)
            continue
        if text:
            used_files.append(path)
            pieces.append(text)
    return "\n\n".join(pieces).strip(), used_files


def read_docx_files(directory: Path, job_id: str) -> tuple[str, list[Path]]:
    if not directory.exists():
        return "", []
    try:
        from docx import Document
    except Exception as exc:
        logging.warning("python-docx is unavailable for slide source fallback: %s", exc)
        return "", []
    files = sorted(path for path in directory.glob(f"{job_id}_*.docx") if path.is_file())
    pieces: list[str] = []
    used_files: list[Path] = []
    for path in files:
        try:
            document = Document(str(path))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs).strip()
        except Exception as exc:
            logging.warning("Could not read DOCX source file %s: %s", path, exc)
            continue
        if text:
            used_files.append(path)
            pieces.append(text)
    return "\n\n".join(pieces).strip(), used_files


def load_verified_source(job_dir: Path, completed_docs_dir: Path | None = None) -> SourceBundle:
    """Load all source text for a completed job, preferring verified page files."""
    job_id = job_dir.name
    checked_locations = [
        job_dir / "verified_pages",
        job_dir / "extracted_pages",
        job_dir / "chapter_sources",
        job_dir / "output_notes",
    ]
    for directory in checked_locations:
        text, files = read_text_files(directory, ["*.md", "*.txt"])
        if text:
            needs_review = directory.name == "extracted_pages"
            warning = "Using extracted_pages because verified_pages was not available." if needs_review else ""
            return SourceBundle(
                text=text,
                files=files,
                checked_locations=checked_locations + ([completed_docs_dir] if completed_docs_dir else []),
                source_location=directory,
                needs_review=needs_review,
                warning=warning,
            )

    if completed_docs_dir:
        text, files = read_docx_files(completed_docs_dir, job_id)
        if text:
            return SourceBundle(
                text=text,
                files=files,
                checked_locations=checked_locations + [completed_docs_dir],
                source_location=completed_docs_dir,
                needs_review=True,
                warning="Using completed_docs fallback because job source folders had no text.",
            )

    return SourceBundle(
        text="",
        files=[],
        checked_locations=checked_locations + ([completed_docs_dir] if completed_docs_dir else []),
        source_location=None,
    )


def split_sentence_sized(text: str, max_chars: int) -> list[str]:
    return split_by_boundary(text, max_chars)


def split_by_boundary(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")

    def find_split_index(remaining: str) -> int:
        if len(remaining) <= max_chars:
            return len(remaining)
        window = remaining[: max_chars + 1]
        for boundary in ["\n\n", "\n", "॥ ", "। ", ". ", "? ", "! ", "; ", ", ", " "]:
            index = window.rfind(boundary)
            if index >= max_chars // 2:
                return index + len(boundary)
        return max_chars

    chunks: list[str] = []
    offset = 0
    while offset < len(text):
        split_at = find_split_index(text[offset:])
        if split_at <= 0:
            split_at = min(max_chars, len(text) - offset)
        chunks.append(text[offset : offset + split_at])
        offset += split_at
    return chunks


def split_source_into_slide_chunks(source_text: str, max_chars_per_slide: int = MAX_CHARS_PER_SLIDE) -> list[str]:
    """Split source into ordered slide chunks without dropping, summarizing, or paraphrasing."""
    normalized = normalize_source_text(source_text)
    if not normalized:
        return []
    return split_by_boundary(normalized, max_chars_per_slide)


def build_slides_from_chunks(chunks: list[str], source_char_count: int, source_words: int) -> dict[str, Any]:
    slides: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        slides.append(
            {
                "slide_number": index,
                "source_chunk_id": f"chunk_{index:03d}",
                "title": f"भाग {index}",
                "exact_text": chunk,
                "needs_teacher_review": False,
            }
        )
    return {
        "deck_title": DEFAULT_DECK_TITLE,
        "mode": DEFAULT_DECK_MODE,
        "prompt_profile": MASTER_PROMPT_NAME,
        "prompt_compliance": {
            "white_background": True,
            "exact_source_text_preserved": True,
            "source_conflicts_reported": True,
        },
        "source_char_count": source_char_count,
        "source_word_count": source_words,
        "chunk_count": len(chunks),
        "slide_count": len(slides),
        "slides": slides,
    }


def validate_exact_document_slide_deck(source_text: str, chunks: list[str], deck: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    source_char_count = len(source_text)
    expected_min_slides = math.ceil(source_char_count / MAX_CHARS_PER_SLIDE) if source_char_count else 0
    slides = deck.get("slides", []) or []
    slide_count = len(slides)

    if source_char_count == 0:
        errors.append("No verified source text found for this job. Complete PDF processing first.")
    if slide_count < expected_min_slides:
        errors.append(f"Slide count too low: expected at least {expected_min_slides}, generated {slide_count}.")
    if int(deck.get("slide_count") or 0) != slide_count:
        errors.append("slides.json slide_count does not match slides list length.")
    if int(deck.get("chunk_count") or 0) != len(chunks):
        errors.append("slides.json chunk_count does not match chunk list length.")
    for index, chunk in enumerate(chunks, start=1):
        if index > slide_count or str(slides[index - 1].get("exact_text", "")) != chunk:
            errors.append(f"chunk_{index:03d} is missing or changed in slides.json.")
    for index, slide in enumerate(slides, start=1):
        exact_text = str(slide.get("exact_text", ""))
        if "1, 2, 3" in exact_text and "1, 2, 3" not in source_text:
            errors.append(f"slide {index} contains dummy text: 1, 2, 3")
    if errors:
        for slide in slides:
            if isinstance(slide, dict):
                slide["needs_teacher_review"] = True
    return errors


def write_no_source_report(job_id: str, job_dir: Path, output_dir: Path, source: SourceBundle) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    errors = ["No verified source text found for this job. Complete PDF processing first."]
    empty_deck = {
        "deck_title": DEFAULT_DECK_TITLE,
        "mode": DEFAULT_DECK_MODE,
        "prompt_profile": MASTER_PROMPT_NAME,
        "source_char_count": 0,
        "source_word_count": 0,
        "chunk_count": 0,
        "slide_count": 0,
        "slides": [],
    }
    save_slides_json(empty_deck, output_dir)
    create_source_coverage_report(
        source_text="",
        chunks=[],
        deck=empty_deck,
        validation_errors=errors,
        output_dir=output_dir,
        png_count=0,
        pdf_page_count=0,
    )
    create_review_report(
        job_id=job_id,
        source_files=[],
        deck=empty_deck,
        validation_errors=errors,
        warnings=[],
        output_dir=output_dir,
        source_debug={
            "job_id": job_id,
            "job_dir": str(job_dir),
            "source_folders_checked": [str(path) for path in source.checked_locations if path],
            "files_loaded": [],
            "source_char_count": 0,
            "source_word_count": 0,
            "chunk_count": 0,
            "slide_count": 0,
            "png_count": 0,
            "pdf_page_count": 0,
            "zip_path": "",
            "coverage_status": "FAIL",
        },
    )
    write_deck_state(
        output_dir,
        job_id=job_id,
        status="failed",
        coverage_status="FAIL",
        latest_error="No verified source text found for this job. Complete PDF processing first.",
        finished_at=now_text(),
    )


def count_pdf_pages_from_pdf_structure(pdf_path: Path) -> int:
    try:
        data = pdf_path.read_bytes()
    except Exception as exc:
        logging.warning("Could not read generated PDF for page counting %s: %s", pdf_path, exc)
        return 0
    return len(re.findall(rb"/Type\s*/Page\b", data))


def count_pdf_pages(pdf_path: Path) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).with_name(".env"))
    except Exception:
        pass
    try:
        poppler_path = os.getenv("POPPLER_PATH")
        kwargs = {}
        if poppler_path and Path(poppler_path).exists():
            kwargs["poppler_path"] = poppler_path
        info = pdfinfo_from_path(str(pdf_path), **kwargs)
        return int(info.get("Pages", 0))
    except Exception as e:
        logging.warning("Failed to count pages in %s with pdfinfo: %s", pdf_path, e)
    return count_pdf_pages_from_pdf_structure(pdf_path)


def generate_image_slide_deck(
    job_id: str,
    job_dir: Path,
    output_root: Path,
    completed_docs_dir: Path | None = None,
) -> dict[str, Any]:
    output_dir = output_root / job_id
    zip_path = output_root / f"{job_id}_slide_deck.zip"
    if zip_path.exists():
        zip_path.unlink()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_deck_state(output_dir, job_id=job_id, status="generating", started_at=now_text(), latest_error="")

    try:
        source = load_verified_source(job_dir, completed_docs_dir=completed_docs_dir)
        if not source.text.strip():
            write_no_source_report(job_id, job_dir, output_dir, source)
            raise RuntimeError("No verified source text found for this job. Complete PDF processing first.")

        source_char_count = len(source.text)
        source_words = source_word_count(source.text)
        write_deck_state(output_dir, source_char_count=source_char_count, source_word_count=source_words)
        chunks = split_source_into_slide_chunks(source.text, max_chars_per_slide=MAX_CHARS_PER_SLIDE)
        write_deck_state(output_dir, chunk_count=len(chunks), slide_count=len(chunks))
        deck = build_slides_from_chunks(chunks, source_char_count, source_words)
        create_prompt_used_report(MASTER_PROMPT_NAME, build_prompt_engine_request(chunks), output_dir)

        prompt_plan, prompt_engine_warnings, prompt_engine_status = generate_prompt_visual_plan(chunks)
        deck["prompt_engine_status"] = prompt_engine_status
        write_deck_state(output_dir, prompt_engine_status=prompt_engine_status)
        prompt_engine_warnings.extend(apply_prompt_visual_plan(deck, prompt_plan))

        if source.needs_review:
            for slide in deck.get("slides", []) or []:
                slide["needs_teacher_review"] = True

        prompt_warnings = master_prompt_source_warnings(source.text)
        if prompt_warnings:
            for slide in deck.get("slides", []) or []:
                if isinstance(slide, dict):
                    slide["needs_teacher_review"] = True

        validation_errors = validate_exact_document_slide_deck(source.text, chunks, deck)
        save_slides_json(deck, output_dir)
        write_deck_state(output_dir, slide_count=len(deck.get("slides", [])))

        def on_slide_rendered(rendered_count: int, _: Path) -> None:
            write_deck_state(output_dir, rendered_slide_count=rendered_count)

        slide_paths, render_warnings = render_slides(deck, output_dir, progress_callback=on_slide_rendered)
        png_count = len(slide_paths)
        pdf_path = create_pdf(slide_paths, output_dir)
        write_deck_state(output_dir, pdf_created=True)
        pdf_page_count = count_pdf_pages(pdf_path)
        create_editable_text(deck, output_dir)
        _, coverage_status, coverage_errors = create_source_coverage_report(
            source.text,
            chunks,
            deck,
            validation_errors,
            output_dir,
            png_count=png_count,
            pdf_page_count=pdf_page_count,
        )
        validation_errors.extend(coverage_errors)
        write_deck_state(output_dir, coverage_status=coverage_status)
        warnings = [source.warning] if source.warning else []
        warnings.extend(prompt_engine_warnings)
        warnings.extend(prompt_warnings)
        warnings.extend(render_warnings)
        final_status = "failed" if validation_errors else ("completed_with_review_needed" if warnings else "completed")
        create_review_report(
            job_id,
            source.files,
            deck,
            validation_errors,
            warnings,
            output_dir,
            source_debug={
                "job_id": job_id,
                "job_dir": str(job_dir),
                "prompt_profile": MASTER_PROMPT_NAME,
                "prompt_engine_status": prompt_engine_status,
                "source_folders_checked": [str(path) for path in source.checked_locations if path],
                "source_location_used": str(source.source_location) if source.source_location else "",
                "files_loaded": [str(path) for path in source.files],
                "source_char_count": source_char_count,
                "source_word_count": source_words,
                "chunk_count": len(chunks),
                "slide_count": len(deck.get("slides", [])),
                "png_count": png_count,
                "pdf_page_count": pdf_page_count,
                "zip_path": str(zip_path),
                "coverage_status": coverage_status,
                "final_status": final_status,
                "slide_count_reason": f"ceil({source_char_count} / {MAX_CHARS_PER_SLIDE}) = {math.ceil(source_char_count / MAX_CHARS_PER_SLIDE)} minimum slides; generated {len(deck.get('slides', []))}.",
            },
        )
        create_zip(output_dir, zip_path)
        write_deck_state(
            output_dir,
            status=final_status,
            zip_created=True,
            coverage_status=coverage_status,
            finished_at=now_text(),
            latest_error="; ".join(validation_errors),
        )

        if validation_errors:
            raise RuntimeError("; ".join(validation_errors))
    except Exception as exc:
        write_deck_state(output_dir, status="failed", latest_error=str(exc), finished_at=now_text())
        raise

    return {
        "output_dir": output_dir,
        "zip_path": zip_path,
        "slide_count": len(deck.get("slides", [])),
        "source_char_count": source_char_count,
        "source_word_count": source_words,
        "chunk_count": len(chunks),
        "png_count": png_count,
        "pdf_page_count": pdf_page_count,
        "coverage_status": coverage_status,
        "final_status": final_status,
        "validation_errors": validation_errors,
        "warnings": warnings,
    }
