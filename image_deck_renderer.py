from __future__ import annotations

import io
import logging
import os
import hashlib
import shutil
import concurrent.futures
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from image_deck_prompts import build_image_gen_prompt
from utils import clear_blocking_proxy_env

SLIDE_SIZE = (1920, 1080)
REVIEW_BADGE = "शिक्षक समीक्षा आवश्यक"

THEME_ALIASES = {
    "water_channel": "water_channel",
    "fluid_homeostasis": "water_channel",
    "thirst": "thirst",
    "polydipsia": "thirst",
    "edema": "edema",
    "oedema": "edema",
    "ascites": "ascites",
    "electrolyte": "electrolyte",
    "electrolyte_imbalance": "electrolyte",
    "bone": "bone",
    "srotas": "srotas",
    "dosha": "dosha",
    "panchakarma": "panchakarma",
    "dravya": "dravya",
}


def normalize_theme_tokens(theme: str) -> list[str]:
    raw_tokens = str(theme or "").replace(",", "|").replace("/", "|").split("|")
    tokens: list[str] = []
    for raw_token in raw_tokens:
        token = raw_token.strip().lower().replace(" ", "_").replace("-", "_")
        mapped = THEME_ALIASES.get(token, token)
        if mapped and mapped not in tokens:
            tokens.append(mapped)
    return tokens or ["general"]


def visible_cluster_labels(slide: dict[str, Any], max_labels: int = 3) -> list[str]:
    labels: list[str] = []
    for value in slide.get("visual_clusters", []) or []:
        label = str(value or "").strip()
        if label and not any("A" <= char <= "Z" or "a" <= char <= "z" for char in label):
            labels.append(label)
    return labels[:max_labels]


def infer_visual_theme(text: str) -> str:
    if any(term in text for term in ["तृष्णा", "पिपासा", "मुखशोष", "कण्ठ"]):
        return "thirst"
    if any(term in text for term in ["शोथ", "सूजन", "ओडिमा"]):
        return "edema"
    if any(term in text for term in ["उदर", "जलोदर"]):
        return "ascites"
    if any(term in text for term in ["उदक", "जल", "अम्बु", "स्रोतस"]):
        return "water_channel"
    if any(term in text for term in ["लवण", "विद्युतांश"]):
        return "electrolyte"
    if any(term in text for term in ["अस्थि", "हड्डी"]):
        return "bone"
    return "general"


# ─── Font helpers ──────────────────────────────────────────────────

def find_devanagari_font() -> tuple[str | None, str | None]:
    preferred_names = [
        "NotoSansDevanagari-Regular.ttf",
        "Noto Sans Devanagari Regular.ttf",
        "Nirmala.ttc",
        "Nirmala.ttf",
        "Nirmala UI.ttf",
        "mangal.ttf",
        "Mangal.ttf",
    ]
    search_dirs = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ]
    for directory in search_dirs:
        if not directory.exists():
            continue
        for name in preferred_names:
            path = directory / name
            if path.exists():
                return str(path), None
        for pattern in ("*.ttf", "*.ttc", "*.otf"):
            for path in directory.rglob(pattern):
                if any(t.lower() in path.name.lower() for t in ["devanagari", "nirmala", "mangal"]):
                    return str(path), None
    return None, "No preferred Devanagari font was found. Pillow fallback font was used."


def load_font(font_path: str | None, size: int) -> ImageFont.ImageFont:
    if font_path:
        try:
            return ImageFont.truetype(font_path, size=size)
        except Exception as exc:
            logging.warning("Could not load font %s: %s", font_path, exc)
    return ImageFont.load_default()


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def wrap_preserved_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(wrap_text(draw, paragraph, font, max_width) or [""])
    return lines


def exact_text_layout(draw: ImageDraw.ImageDraw, text: str, font_path: str | None,
                      max_width: int, max_height: int) -> tuple[ImageFont.ImageFont, list[str], int]:
    for size in range(34, 15, -2):
        font = load_font(font_path, size)
        lines = wrap_preserved_lines(draw, text, font, max_width)
        line_step = max(28, int(size * 1.28))
        total_height = len(lines) * line_step
        if total_height <= max_height:
            return font, lines, line_step
    font = load_font(font_path, 16)
    return font, wrap_preserved_lines(draw, text, font, max_width), 21


# ─── Drawing helpers ───────────────────────────────────────────────

def draw_shadowed_round_rect(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int],
                             radius: int, fill: str, outline: str | None = None) -> None:
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle((x1 + 8, y1 + 10, x2 + 8, y2 + 10), radius=radius, fill="#e8edf3")
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline or fill, width=2)


def draw_arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int], fill: str) -> None:
    draw.line((start, end), fill=fill, width=8)
    x, y = end
    draw.polygon([(x, y), (x - 24, y - 14), (x - 24, y + 14)], fill=fill)


# ─── AI Image Generation ──────────────────────────────────────────

def _ai_images_enabled() -> bool:
    return os.getenv("USE_AI_IMAGES", "true").strip().lower() not in {"0", "false", "no", "off"}


def _load_api_key() -> str | None:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).with_name(".env"))
    except Exception:
        pass
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


CACHE_DIR = Path(__file__).parent / "image_deck_cache"

def generate_medical_illustration(image_prompt: str, output_path: Path) -> bool:
    """Generate a text-free medical illustration via Imagen 3. Returns True on success."""
    if not _ai_images_enabled():
        return False

    full_prompt = build_image_gen_prompt(image_prompt)
    model = os.getenv("IMAGE_GEN_MODEL", "imagen-3.0-generate-002")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    prompt_hash = hashlib.sha256(full_prompt.encode("utf-8")).hexdigest()
    cache_path = CACHE_DIR / f"{prompt_hash}.png"

    if cache_path.exists():
        shutil.copy2(cache_path, output_path)
        logging.info("AI illustration loaded from cache: %s", output_path)
        return True

    api_key = _load_api_key()
    if not api_key:
        logging.warning("AI image generation skipped: no API key.")
        return False
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logging.warning("AI image generation skipped: google-genai not installed.")
        return False

    try:
        clear_blocking_proxy_env()
        timeout = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "180"))
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout * 1000))

        response = client.models.generate_images(
            model=model,
            prompt=full_prompt,
            config=types.GenerateImagesConfig(number_of_images=1, output_mime_type="image/png"),
        )
        if response.generated_images:
            img_bytes = response.generated_images[0].image.image_bytes
            img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
            img = img.resize((780, 660), Image.LANCZOS)
            img.save(cache_path, format="PNG")
            shutil.copy2(cache_path, output_path)
            logging.info("AI illustration saved and cached: %s", output_path)
            return True
    except Exception as exc:
        logging.warning("AI image generation failed (will use fallback): %s", exc)
    return False


# ─── Fallback Pillow Medical Icons (existing) ─────────────────────

def draw_medical_icon(draw: ImageDraw.ImageDraw, theme: str) -> None:
    cx, cy = 1640, 245
    draw.ellipse((1510, 110, 1770, 370), fill="#e4f3ee", outline="#b9ddd3", width=4)
    draw.ellipse((1540, 140, 1740, 340), fill="#f8fffd", outline="#d2e8e0", width=3)
    if theme == "thirst":
        draw.ellipse((1602, 150, 1678, 245), fill="#9fd8f0", outline="#4ba3c7", width=4)
        draw.polygon([(1640, 282), (1594, 220), (1686, 220)], fill="#9fd8f0", outline="#4ba3c7")
        draw.arc((1582, 255, 1698, 325), 15, 165, fill="#86b8b3", width=8)
    elif theme == "edema":
        draw.rounded_rectangle((1575, 170, 1705, 320), radius=54, fill="#f2c8c4", outline="#d98984", width=4)
        draw.ellipse((1548, 230, 1732, 340), fill="#ffd9d5", outline="#d98984", width=4)
        draw.arc((1570, 210, 1710, 335), 210, 330, fill="#b75650", width=8)
    elif theme == "ascites":
        draw.ellipse((1555, 155, 1725, 335), fill="#f6d0b6", outline="#cb8c69", width=4)
        draw.arc((1580, 205, 1700, 325), 0, 180, fill="#55a9c8", width=10)
        draw.ellipse((1602, 220, 1678, 306), fill="#bce7f5", outline="#55a9c8", width=3)
    elif theme == "electrolyte":
        draw.ellipse((1570, 155, 1710, 335), fill="#d9ecff", outline="#74a5d8", width=4)
        draw.line((1625, 180, 1595, 255, 1648, 255, 1610, 325), fill="#e5a600", width=10)
        draw.line((1680, 190, 1660, 238), fill="#4f85c2", width=8)
        draw.line((1640, 212, 1688, 212), fill="#4f85c2", width=8)
    elif theme == "water_channel":
        draw.rounded_rectangle((1570, 160, 1710, 330), radius=42, fill="#dff5f0", outline="#73b6a7", width=4)
        draw.line((1640, 170, 1640, 320), fill="#5ba7cb", width=10)
        draw.arc((1575, 185, 1705, 300), 285, 75, fill="#5ba7cb", width=10)
        draw.arc((1575, 215, 1705, 330), 105, 255, fill="#5ba7cb", width=10)
    else:
        draw.ellipse((1575, 175, 1705, 305), fill="#cfe9e1", outline="#87bcad", width=3)
        draw.line((1640, 135, 1640, 345), fill="#87bcad", width=5)
        draw.line((1535, 240, 1745, 240), fill="#87bcad", width=5)


def draw_mini_medical_icon(draw: ImageDraw.ImageDraw, theme: str,
                           center: tuple[int, int], scale: float = 1.0) -> None:
    cx, cy = center
    r = int(62 * scale)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#f8fffd", outline="#d2e8e0", width=max(2, int(3 * scale)))
    if theme == "thirst":
        draw.ellipse((cx - 18, cy - 42, cx + 18, cy + 5), fill="#9fd8f0", outline="#4ba3c7", width=3)
        draw.polygon([(cx, cy + 42), (cx - 28, cy), (cx + 28, cy)], fill="#9fd8f0", outline="#4ba3c7")
    elif theme == "edema":
        draw.rounded_rectangle((cx - 35, cy - 38, cx + 35, cy + 35), radius=25, fill="#ffd9d5", outline="#d98984", width=3)
        draw.arc((cx - 48, cy - 5, cx + 48, cy + 52), 200, 340, fill="#b75650", width=5)
    elif theme == "ascites":
        draw.ellipse((cx - 45, cy - 45, cx + 45, cy + 45), fill="#f6d0b6", outline="#cb8c69", width=3)
        draw.ellipse((cx - 28, cy - 5, cx + 28, cy + 42), fill="#bce7f5", outline="#55a9c8", width=3)
    elif theme == "electrolyte":
        draw.ellipse((cx - 42, cy - 42, cx + 42, cy + 42), fill="#d9ecff", outline="#74a5d8", width=3)
        draw.line((cx - 8, cy - 38, cx - 25, cy + 5, cx + 8, cy + 5, cx - 12, cy + 42), fill="#e5a600", width=6)
        draw.line((cx + 22, cy - 20, cx + 50, cy - 20), fill="#4f85c2", width=5)
        draw.line((cx + 36, cy - 34, cx + 36, cy - 6), fill="#4f85c2", width=5)
    elif theme == "water_channel":
        draw.rounded_rectangle((cx - 42, cy - 45, cx + 42, cy + 45), radius=24, fill="#dff5f0", outline="#73b6a7", width=3)
        draw.line((cx, cy - 36, cx, cy + 36), fill="#5ba7cb", width=7)
        draw.arc((cx - 42, cy - 32, cx + 42, cy + 24), 285, 75, fill="#5ba7cb", width=6)
    else:
        draw.ellipse((cx - 35, cy - 35, cx + 35, cy + 35), fill="#cfe9e1", outline="#87bcad", width=3)
        draw.line((cx, cy - 48, cx, cy + 48), fill="#87bcad", width=4)


# ─── NotebookLM-style slide composer ──────────────────────────────

def draw_notebooklm_slide(slide: dict[str, Any], deck_title: str,
                          output_path: Path, font_path: str | None,
                          ai_image_path: Path | None = None) -> None:
    """Draw a premium NotebookLM-inspired slide with AI illustration + text overlay."""
    image = Image.new("RGB", SLIDE_SIZE, "white")
    draw = ImageDraw.Draw(image)

    title_font = load_font(font_path, 56)
    subtitle_font = load_font(font_path, 32)
    body_font = load_font(font_path, 28)
    small_font = load_font(font_path, 24)
    badge_font = load_font(font_path, 28)
    shloka_font = load_font(font_path, 26)

    slide_title = str(slide.get("slide_title") or slide.get("title") or deck_title or "")
    exact_text = str(slide.get("exact_text", "") or "").strip()
    labels = visible_cluster_labels(slide)
    theme = str(slide.get("visual_theme") or infer_visual_theme(exact_text))
    theme_tokens = normalize_theme_tokens(theme)

    # ── Title bar (green gradient strip) ──
    draw.rectangle((0, 0, 1920, 110), fill="#2d8a72")
    draw.rectangle((0, 108, 1920, 114), fill="#1f6b58")  # subtle bottom border
    for line in wrap_text(draw, slide_title, title_font, 1750)[:2]:
        draw.text((60, 22), line, fill="white", font=title_font)

    # ── Subtitle (first line of exact_text as description) ──
    first_line = ""
    if exact_text:
        parts = exact_text.split("\n", 1)
        first_line = parts[0][:120] if parts else ""
    if first_line:
        for line in wrap_text(draw, first_line, subtitle_font, 1800)[:1]:
            draw.text((60, 128), line, fill="#2d5a50", font=subtitle_font)

    # ── AI illustration (right side) or fallback icons ──
    img_area = (1060, 175, 1860, 835)
    has_ai_image = False
    if ai_image_path and ai_image_path.exists():
        try:
            ai_img = Image.open(ai_image_path).convert("RGBA")
            ai_img = ai_img.resize((img_area[2] - img_area[0], img_area[3] - img_area[1]), Image.LANCZOS)
            # Soft rounded corners
            mask = Image.new("L", ai_img.size, 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle((0, 0, ai_img.size[0], ai_img.size[1]), radius=28, fill=255)
            white_bg = Image.new("RGBA", ai_img.size, (255, 255, 255, 255))
            composited = Image.composite(ai_img, white_bg, mask)
            image.paste(composited.convert("RGB"), (img_area[0], img_area[1]))
            # Border
            draw.rounded_rectangle(img_area, radius=28, outline="#d0e0d8", width=3)
            has_ai_image = True
        except Exception as exc:
            logging.warning("Could not paste AI image: %s", exc)

    if not has_ai_image:
        # Fallback: draw Pillow medical icon
        draw_shadowed_round_rect(draw, img_area, 28, "#f7fbff", "#d8e7f5")
        icon_cx = (img_area[0] + img_area[2]) // 2
        icon_cy = (img_area[1] + img_area[3]) // 2 - 60
        draw_mini_medical_icon(draw, theme_tokens[0], (icon_cx, icon_cy), scale=2.5)
        # Draw labels under icon
        label_y = icon_cy + 180
        for label in labels[:3]:
            draw.rounded_rectangle((img_area[0] + 30, label_y, img_area[2] - 30, label_y + 48),
                                   radius=14, fill="#ffffff", outline="#dce8f2", width=2)
            for ln in wrap_text(draw, label, small_font, img_area[2] - img_area[0] - 80)[:1]:
                draw.text((img_area[0] + 50, label_y + 10), ln, fill="#243b53", font=small_font)
            label_y += 60

    # ── Text card (left side) ──
    text_panel = (50, 175, 1030, 835)
    draw_shadowed_round_rect(draw, text_panel, 24, "#fbfdff", "#d0dce8")

    remaining_text = exact_text
    if first_line and remaining_text.startswith(first_line):
        remaining_text = remaining_text[len(first_line):].strip()
    if remaining_text:
        text_font, lines, line_step = exact_text_layout(
            draw, remaining_text, font_path,
            max_width=text_panel[2] - text_panel[0] - 80,
            max_height=text_panel[3] - text_panel[1] - 50,
        )
        text_y = text_panel[1] + 25
        for line in lines:
            draw.text((text_panel[0] + 40, text_y), line, fill="#243b53", font=text_font)
            text_y += line_step

    # ── Shloka bar (bottom) ──
    shloka = str(slide.get("shloka", "") or "").strip()
    if shloka:
        shloka_box = (50, 855, 1860, 950)
        draw_shadowed_round_rect(draw, shloka_box, 18, "#fff8e7", "#ead7a5")
        sy = 870
        for line in wrap_text(draw, shloka, shloka_font, 1740)[:3]:
            draw.text((80, sy), line, fill="#5c4b24", font=shloka_font)
            sy += 34

    # ── Visual cluster labels (bottom row) ──
    if labels and has_ai_image:
        lx = 60
        for label in labels[:3]:
            tw = text_width(draw, label, small_font) + 40
            draw.rounded_rectangle((lx, 960, lx + tw, 1005), radius=12, fill="#e8f5f0", outline="#b3d9cc", width=2)
            draw.text((lx + 20, 968), label, fill="#1f6b58", font=small_font)
            lx += tw + 18

    # ── Review badge ──
    if slide.get("needs_teacher_review"):
        draw.rounded_rectangle((1340, 1015, 1870, 1065), radius=16, fill="#fff0f0", outline="#f3b5b5", width=3)
        draw.text((1365, 1025), REVIEW_BADGE, fill="#a52020", font=badge_font)

    # ── Bottom accent line ──
    draw.rectangle((0, 1070, 1920, 1080), fill="#2d8a72")

    image.save(output_path, format="PNG")


# ─── Legacy slide renderer (fallback) ─────────────────────────────

def draw_prompt_visual_panel(draw: ImageDraw.ImageDraw, slide: dict[str, Any],
                             font_path: str | None, xy: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = xy
    draw_shadowed_round_rect(draw, xy, 24, "#f7fbff", "#d8e7f5")
    themes = normalize_theme_tokens(str(slide.get("visual_theme") or ""))
    labels = visible_cluster_labels(slide)
    if not labels:
        labels = [str(slide.get("title", ""))]
    label_font = load_font(font_path, 24)
    small_font = load_font(font_path, 21)
    comparison = bool(slide.get("comparison_view"))

    if comparison and len(themes) >= 2:
        cols = min(2, len(themes))
        col_w = (x2 - x1 - 70) // cols
        for index, theme in enumerate(themes[:cols]):
            left = x1 + 28 + index * (col_w + 18)
            draw.rounded_rectangle((left, y1 + 30, left + col_w, y2 - 30), radius=18, fill="#ffffff", outline="#dbe6ef", width=2)
            draw_mini_medical_icon(draw, theme, (left + col_w // 2, y1 + 135), scale=0.9)
            label = labels[index] if index < len(labels) else labels[0]
            ly = y1 + 235
            for line in wrap_text(draw, label, small_font, col_w - 35)[:3]:
                draw.text((left + 18, ly), line, fill="#284257", font=small_font)
                ly += 30
        return

    center_x = (x1 + x2) // 2
    positions = [(x1 + 105, y1 + 112), (center_x, y1 + 112), (x2 - 105, y1 + 112)]
    for index, theme in enumerate(themes[:3]):
        draw_mini_medical_icon(draw, theme, positions[index], scale=0.95)
        if index < min(len(themes), 3) - 1:
            draw_arrow(draw, (positions[index][0] + 72, positions[index][1]),
                       (positions[index + 1][0] - 72, positions[index + 1][1]), "#9db7b1")

    label_y = y1 + 245
    for label in labels[:3]:
        draw.rounded_rectangle((x1 + 36, label_y, x2 - 36, label_y + 48), radius=14, fill="#ffffff", outline="#dce8f2", width=2)
        for line in wrap_text(draw, label, label_font, x2 - x1 - 100)[:1]:
            draw.text((x1 + 58, label_y + 9), line, fill="#243b53", font=label_font)
        label_y += 62


def draw_slide(slide: dict[str, Any], deck_title: str, output_path: Path,
               font_path: str | None, ai_image_path: Path | None = None) -> None:
    """Draw a single slide. Uses NotebookLM-style layout."""
    # Always use the new NotebookLM-style layout
    draw_notebooklm_slide(slide, deck_title, output_path, font_path, ai_image_path)


# ─── Main render entry point ──────────────────────────────────────

def render_slides(deck: dict[str, Any], output_dir: Path,
                  progress_callback: Any | None = None) -> tuple[list[Path], list[str]]:
    slides_dir = output_dir / "slides"
    ai_images_dir = output_dir / "ai_images"
    slides_dir.mkdir(parents=True, exist_ok=True)
    ai_images_dir.mkdir(parents=True, exist_ok=True)

    font_path, font_warning = find_devanagari_font()
    warnings = [font_warning] if font_warning else []
    slide_paths: list[Path] = []
    
    slides = deck.get("slides", []) or []
    ai_ok_map = {}
    
    def _gen_image(idx: int, sl: dict[str, Any], img_path: Path) -> tuple[int, bool]:
        prompt = str(sl.get("image_prompt", "") or "").strip()
        if not prompt:
            prompt = str(sl.get("visual_brief", "") or "").strip()
        if prompt:
            return idx, generate_medical_illustration(prompt, img_path)
        return idx, False

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = []
        for index, slide in enumerate(slides, start=1):
            ai_image_path = ai_images_dir / f"ai_{index:02d}.png"
            futures.append(executor.submit(_gen_image, index, slide, ai_image_path))
            
        for future in concurrent.futures.as_completed(futures):
            idx, success = future.result()
            ai_ok_map[idx] = success

    for index, slide in enumerate(slides, start=1):
        output_path = slides_dir / f"slide_{index:02d}.png"
        ai_image_path = ai_images_dir / f"ai_{index:02d}.png"

        ai_ok = ai_ok_map.get(index, False)
        prompt = str(slide.get("image_prompt", "") or "").strip() or str(slide.get("visual_brief", "") or "").strip()
        if prompt and not ai_ok:
            warnings.append(f"Slide {index}: AI image generation failed, using fallback icons.")

        draw_slide(slide, str(deck.get("deck_title", "")), output_path, font_path,
                   ai_image_path if ai_ok else None)
        slide_paths.append(output_path)

        if progress_callback:
            progress_callback(index, output_path)

    return slide_paths, warnings


def render_all_slides(deck: dict[str, Any], output_dir: Path) -> tuple[list[Path], list[str]]:
    return render_slides(deck, output_dir)
