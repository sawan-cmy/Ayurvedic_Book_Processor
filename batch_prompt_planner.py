from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai

from image_deck_generator import build_prompt_engine_request, clear_blocking_proxy_env


ROOT = Path(__file__).resolve().parent


def load_deck_chunks(job_id: str) -> list[str]:
    slides_path = ROOT / "slide_deck_outputs" / job_id / "slides.json"
    data = json.loads(slides_path.read_text(encoding="utf-8"))
    slides = data.get("slides", [])
    if not isinstance(slides, list) or not slides:
        raise RuntimeError(f"No slides found in {slides_path}")
    return [str(slide.get("exact_text", "")) for slide in slides if isinstance(slide, dict)]


def batch_requests(chunks: list[str], batch_size: int) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for start in range(0, len(chunks), batch_size):
        prompt = build_prompt_engine_request(chunks[start : start + batch_size], start_index=start + 1)
        requests.append(
            {
                "contents": [
                    {
                        "parts": [{"text": prompt}],
                        "role": "user",
                    }
                ]
            }
        )
    return requests


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)

    parser = argparse.ArgumentParser(description="Submit non-urgent slide prompt planning to Gemini Batch API.")
    parser.add_argument("job_id")
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("PROMPT_ENGINE_BATCH_SIZE", "4")))
    parser.add_argument("--model", default=os.getenv("SLIDE_PROMPT_MODEL", "gemini-2.5-flash"))
    args = parser.parse_args()

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY or GOOGLE_API_KEY.")

    chunks = load_deck_chunks(args.job_id)
    requests = batch_requests(chunks, max(1, args.batch_size))
    clear_blocking_proxy_env()
    client = genai.Client(api_key=api_key)
    job = client.batches.create(
        model=args.model.removeprefix("models/"),
        src=requests,
        config={"display_name": f"slide-prompt-plan-{args.job_id}"},
    )

    output_dir = ROOT / "slide_deck_outputs" / args.job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "batch_prompt_job.json"
    state_path.write_text(
        json.dumps(
            {
                "job_id": args.job_id,
                "batch_job_name": job.name,
                "model": args.model,
                "request_count": len(requests),
                "submitted_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Submitted batch prompt job: {job.name}")
    print(f"Saved state: {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
