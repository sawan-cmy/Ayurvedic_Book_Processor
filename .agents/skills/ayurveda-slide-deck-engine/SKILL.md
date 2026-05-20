---
name: ayurveda-slide-deck-engine
description: Use this skill when working on the Ayurvedic Book Processor slide deck and infographic feature.
---

# Ayurveda Slide Deck Engine Skill

## Project Context

This project is the Ayurvedic Book Processor.

Existing workflow:
- Staff upload scanned Ayurvedic textbook PDFs.
- Gemini extracts page text.
- Gemini verifies extracted text.
- Verified pages are saved.
- DOCX files are generated.
- Failed jobs can resume.

New goal:
Create a direct Ayurveda infographic slide deck generator from verified source text.

## Correct Output

Generate:
- slides.json
- PNG slide images
- PDF slide deck
- editable_text_for_canva.md
- review_report.md
- ZIP download

Output folder:
image_slide_deck_outputs/<job_id>/

Do not depend on NotebookLM.
Do not depend on Canva import.
Do not use PPTX as main output.
Do not use HTML as main output.

Canva is optional only:
Staff may upload PNG slides into Canva and manually decorate/edit if needed.

## Critical Rules

Do NOT:
- automate NotebookLM UI
- use unofficial NotebookLM APIs
- automate Canva UI
- replace Gemini
- rewrite the existing PDF processor
- add SQLite unless explicitly asked
- add worker.py unless explicitly asked
- break existing PDF to DOCX workflow

## Core Flow

Verified Ayurveda source text
→ Gemini creates strict slide JSON
→ Python validates visible text
→ Python renders PNG slides
→ Python combines PNG slides into PDF
→ System creates editable text file
→ System creates review report
→ System creates ZIP

## Required Files

Prefer adding:
- image_deck_generator.py
- image_deck_prompts.py
- image_deck_renderer.py
- image_deck_exporter.py

## Flask Routes

Add only when implementing:
- POST /jobs/<job_id>/generate-image-slide-deck
- GET /jobs/<job_id>/download-image-slide-deck

UI buttons:
- Generate Ayurveda Infographic Deck
- Download Image Slide Deck

## Source Priority

Use source text in this order:
1. verified_pages for the job
2. chapter_sources if available
3. output_notes if available
4. DOCX text only if needed

Never generate slides from unverified raw OCR unless explicitly asked.

## Slide JSON Rules

Gemini should return strict JSON with:
- deck_title
- slides
- slide_number
- slide_type
- title
- shloka
- main_points
- labels
- layout
- visual_brief
- needs_teacher_review

Visible fields:
- deck_title
- title
- shloka
- main_points
- labels

Visible text rules:
- Hindi/Sanskrit must be Devanagari.
- No Roman letters.
- No English words.
- English numerals 1,2,3 are allowed.
- Devanagari numerals are not allowed.
- Preserve Sanskrit/Hindi terms exactly.
- Do not invent content.
- If unclear, write: शिक्षक समीक्षा आवश्यक.

visual_brief may use English because it is not visible on slides.

## Rendering Rules

Use Python rendering.

Recommended:
- Pillow for PNG
- Pillow PDF or reportlab for PDF
- zipfile for ZIP

Slide size:
1920x1080, 16:9

Mandatory style:
- white background
- clean medical infographic look
- clinical pastel colors
- large readable Devanagari title
- Devanagari-capable font
- soft shadows
- rounded cards
- arrows and flow lines
- no visible English text
- English numerals only
- teacher review badge when needed: शिक्षक समीक्षा आवश्यक

Font preference:
- Noto Sans Devanagari
- Nirmala UI
- Mangal

## Image Generation Rule

Do not rely on AI-generated images to render Devanagari text.

If images are generated:
- text-free only
- no labels inside images
- no Hindi/Sanskrit inside images
- visuals only for anatomy/process/icon area

All slide text must be rendered by Python using real fonts.

## Staff Workflow

1. Process PDF normally.
2. Wait for verified pages/DOCX.
3. Click Generate Ayurveda Infographic Deck.
4. Download ZIP.
5. Open PDF deck or PNG slides.
6. Optional: upload PNG slides into Canva.
7. Use editable_text_for_canva.md if manual text editing is needed.
8. Teacher verifies final output using review_report.md.

## Error Handling

Handle:
- no verified source found
- Gemini JSON invalid
- visible text validation failed
- font missing
- image rendering failed
- PDF creation failed
- ZIP creation failed

Show friendly UI messages.
Log developer details.