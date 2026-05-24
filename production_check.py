from __future__ import annotations

import shutil
import sys

from app import (
    COMPLETED_DOCS_DIR,
    JOBS_DB,
    ROOT,
    auth_enabled,
    ensure_project_files,
    max_parallel_jobs,
    production_warnings,
    read_env,
)


def main() -> int:
    ensure_project_files()
    env = read_env()
    warnings = production_warnings(env)
    disk = shutil.disk_usage(ROOT)
    page_workers = env.get("PAGE_WORKERS_PER_JOB", "2")
    test_max_pages = env.get("TEST_MAX_PAGES", "0")

    print("Ayurvedic Book Processor production check")
    print(f"Root: {ROOT}")
    print(f"Jobs DB: {JOBS_DB} ({'ok' if JOBS_DB.exists() else 'missing'})")
    print(f"Completed docs: {COMPLETED_DOCS_DIR}")
    print(f"Free disk: {disk.free // (1024 * 1024)} MB")
    print(f"Login: {'on' if auth_enabled() else 'off'}")
    print(f"Mode: {'full PDF' if test_max_pages == '0' else test_max_pages + ' page limit'}")
    print(f"Parallel jobs: {max_parallel_jobs()}")
    print(f"Page workers per job: {page_workers}")
    print(f"Gemini key: {'set' if env.get('GEMINI_API_KEY') else 'missing'}")

    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"- {warning}")
        return 1

    print("\nProduction preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
