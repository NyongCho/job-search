"""Extract conservative, source-pixel-addressable evidence from public hiring posters."""

import csv
import io
import os
import re
import subprocess
import time
from datetime import date
from itertools import groupby
from urllib.parse import unquote, urlsplit

from job_availability import DATE

MAX_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 24_000_000
TILE_HEIGHT = 2000
OVERLAP = 200

DECORATION = re.compile(
    r"(?:^|[^a-z])(?:logo|icon|favicon|avatar|pixel|tracker|tracking|button|spacer|emoji)s?(?:[^a-z]|$)"
    r"|로고|아이콘|공유하기|버튼", re.I
)
JOB_DETAILS = re.compile(
    r"업무|직무|경력|자격|필수|우대|담당|요건|responsibilit|requir|qualification|experience", re.I
)
TECHNICAL_TERM = re.compile(r"(?<![A-Za-z0-9])(?:B2B|B2C|HTML5)(?![A-Za-z0-9])", re.I)
LIST_PREFIX = re.compile(r"^\s*(?:\(\d{1,2}\)|\d{1,2}[.)])\s*(?=[가-힣A-Za-z])")
YEAR_TITLE = re.compile(r"^20\d{2}\s*년?\s*((?:(?:상반기|하반기|신입|경력|사원|공개|정기|정규직)\s*)*(?:채용|공채|모집))$")
SOURCE_METADATA = re.compile(
    r"(?:(?:문의|연락처|문의전화|전화|tel|phone|contact)[:：.]*)?(?:\+\d{1,3}[-.]?)?\(?0?\d{1,3}\)?[-.]\d{3,4}[-.]\d{4}"
    r"|(?:페이지|쪽|page|p\.)[:：]?\d+(?:/\d+)?|\d+(?:/\d+)?(?:쪽|페이지)"
    r"|(?:공고번호|사업자등록번호|설립연도|설립년도)[:：]?[-.\d]+년?"
    r"|(?:19|20)\d{2}년(?:법인|회사)?설립|(?:전세계)?\d+여?개국(?:에)?(?:서비스제공|진출|수출)", re.I
)
DATE_LABEL = re.compile(r"(?:지원|접수|모집)(?:마감일?|기간|기한)|마감일?|applicationdeadline|closingdate", re.I)
DATE_REMAINDER = re.compile(r"(?:(?:까지|부터|오전|오후)|\([월화수목금토일]\)|(?:[01]?\d|2[0-3]):[0-5]\d|[~～–—:：.,-])*")
MAX_NUMBER_CHECKS = 8
TABLE_HEADER = re.compile(r"(?:모집분야|모집부문|직무|position).*(?:수행업무|담당업무|responsibilities).*(?:자격|요건|qualification)", re.I)


def is_candidate(image):
    if not isinstance(image, dict) or not isinstance(image.get("url"), str):
        return False
    width, height = image.get("width"), image.get("height")
    if isinstance(width, int) and isinstance(height, int) and max(width, height) <= 96:
        return False
    try:
        url = urlsplit(image["url"])
        if url.scheme not in {"https", "http"} or not url.hostname:
            return False
    except ValueError:
        return False
    path = unquote(url.path)
    if path.lower().endswith((".svg", ".ico")):
        return False
    return not DECORATION.search(f"{path} {image.get('alt') or ''}")


def _result(status, reason, lines=(), metadata=()):
    result = {"status": status, "reason": reason, "lines": list(lines)}
    if metadata:
        result["metadata_lines"] = metadata
    return result


def _tsv_lines(raw, scale, offset, width):
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8")), delimiter="\t", quoting=csv.QUOTE_NONE)
    words = (row for row in rows if row["level"] == "5" and row["text"].strip())
    lines = []
    for _, group in groupby(words, lambda r: (r["block_num"], r["par_num"], r["line_num"])):
        # Sparse-text segmentation usually separates cells. Split again across wide gutters.
        cells = [[]]
        right = 0
        for row in group:
            left, top, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
            if cells[-1] and left - right > width * scale * 0.05:
                cells.append([])
            right = left + w
            cells[-1].append((row["text"].strip(), float(row["conf"]), left, top, right, top + h))
        for cell in cells:
            x = round(min(word[2] for word in cell) / scale)
            y = round(min(word[3] for word in cell) / scale)
            w = round(max(word[4] for word in cell) / scale) - x
            h = round(max(word[5] for word in cell) / scale) - y
            if w > 0 and h > 0:
                line = {"text": " ".join(word[0] for word in cell), "bbox": [x, y + offset, w, h]}
                scores = [word[1] for word in cell]
                number_score = min((word[1] for word in cell if re.search(r"\d", word[0])), default=100)
                lines.append((line, sum(scores) / len(scores), min(scores), number_score))
    return lines


def _has_columns(lines, width):
    # Repeated, horizontally separated cells on the same baseline have ambiguous ownership.
    first_pair = None
    for i, row in enumerate(lines):
        x, y, w, h = row["bbox"]
        center = y + h / 2
        for other in lines[i + 1:]:
            ox, oy, ow, oh = other["bbox"]
            if oy > y + h:
                break
            if abs(oy + oh / 2 - center) > min(h, oh) * 0.48:
                continue
            if max(ox - x - w, x - ox - ow) < width * 0.05:
                continue
            if first_pair is None:
                first_pair = center
            elif abs(center - first_pair) > h:
                return True
    return False


def _is_metadata(text):
    compact = re.sub(r"\s+", "", text)
    if SOURCE_METADATA.fullmatch(compact):
        return True
    # Keep dates here as source annotations. Only the availability pipeline establishes a deadline.
    label = DATE_LABEL.match(compact)
    if not label:
        return False
    details = compact[label.end():]
    matches = list(DATE.finditer(details))
    if not 1 <= len(matches) <= 2 or not DATE_REMAINDER.fullmatch(DATE.sub("", details)):
        return False
    try:
        for match in matches:
            year, month, day = match.groups()
            date(int(year or 2000), int(month), int(day))
    except ValueError:
        return False
    return True


def _judge(rows, width, confirm_number):
    rows.sort(key=lambda r: (r[0]["bbox"][1], r[0]["bbox"][0]))
    credible, metadata, numeric = [], [], []
    dropped_detail = False
    for raw, avg, worst, number_score in rows:
        text = LIST_PREFIX.sub("", raw["text"])
        year = YEAR_TITLE.fullmatch(text)
        if year:
            text = year[1]
        if _is_metadata(text):
            metadata.append(raw)
            continue
        line = {**raw, "text": text}
        if text != raw["text"]:
            line["source_text"] = raw["text"]
        legible = avg >= 80 and worst >= 45 and re.search(r"[가-힣A-Za-z]", text)
        if legible:
            credible.append(line)
        elif JOB_DETAILS.search(re.sub(r"\s+", "", text)):
            dropped_detail = True
        if re.search(r"\d", TECHNICAL_TERM.sub("", text) if legible else text):
            compact_text = re.sub(r"\s+", "", text)
            contextual = re.search(r"[가-힣A-Za-z]{2,}", compact_text)
            ambiguous_glyph = re.search(r"\d[OoIl](?![a-zA-Z])", compact_text)
            reliable = (contextual and not ambiguous_glyph and not DATE_LABEL.match(compact_text)
                        and avg >= 90 and worst >= 70 and number_score >= 90)
            numeric.append((line, raw, reliable))
    compact = [re.sub(r"\s+", "", line["text"]) for line, *_ in rows]
    if _has_columns(credible, width) or any(TABLE_HEADER.search(line) for line in compact):
        return _result("uncertain", "ambiguous_layout", credible, metadata)
    if len(numeric) > MAX_NUMBER_CHECKS or any(not reliable for _, _, reliable in numeric):
        return _result("uncertain", "uncertain_number", credible, metadata)
    for line, original, _ in numeric:
        if not confirm_number(original):
            return _result("uncertain", "uncertain_number", credible, metadata)
        line["number_check"] = "matching_crop"
    content = "".join(re.sub(r"\s+", "", line["text"]) for line in credible)
    if len(credible) < 3 or len(content) < 35 or not JOB_DETAILS.search(content):
        return _result("uncertain", "insufficient_text", credible, metadata)
    if dropped_detail or len(credible) < (len(rows) - len(metadata)) * 0.85:
        return _result("uncertain", "low_confidence", credible, metadata)
    return _result("ready", "ocr_ready", credible, metadata)


def _run_tesseract(tile, executable, mode, stop):
    data = io.BytesIO()
    tile.save(data, format="PPM")
    cmd = [executable, "stdin", "stdout", "-l", "kor+eng", "--psm", mode, "-c", "tessedit_create_tsv=1"]
    return subprocess.run(cmd, input=data.getvalue(), capture_output=True, check=True,
                          timeout=max(0.01, stop - time.monotonic()),
                          env={**os.environ, "OMP_THREAD_LIMIT": "1"}).stdout


def _confirm_number(im, line, Image, ImageOps, executable, stop):
    # Read the same original pixels with single-line segmentation and a different scale.
    x, y, w, h = line["bbox"]
    pad = max(3, min(12, h // 5))
    box = (max(0, x - pad), max(0, y - pad), min(im.width, x + w + pad), min(im.height, y + h + pad))
    crop = ImageOps.grayscale(im.crop(box))
    factor = min(2.5, 3200 / crop.width, 500 / crop.height)
    crop = crop.resize((max(1, round(crop.width * factor)), max(1, round(crop.height * factor))), Image.Resampling.LANCZOS)
    data = _run_tesseract(crop, executable, "7", stop)
    rows = _tsv_lines(data, 1, 0, crop.width)
    if not rows or any(avg < 85 or worst < 65 or number_score < 90 for _, avg, worst, number_score in rows):
        return False
    reread = "".join(row["text"] for row, *_ in rows)
    return re.sub(r"\s+", "", reread).casefold() == re.sub(r"\s+", "", line["text"]).casefold()


def _ocr_image(im, Image, ImageOps):
    executable = os.environ.get("POSTER_TESSERACT") or "tesseract"
    listed = subprocess.run([executable, "--list-langs"], capture_output=True, timeout=5, check=True)
    if not {b"kor", b"eng"}.issubset(line.strip() for line in listed.stdout.splitlines()):
        return _result("failed", "language_unavailable")
    rows = []
    width, height = im.size
    scale = min(2, 1600 / width)
    stop = time.monotonic() + 90
    for top in range(0, height, TILE_HEIGHT - OVERLAP):
        end = min(top + TILE_HEIGHT, height)
        tile = im.crop((0, top, width, end))
        if "A" in tile.getbands() or "transparency" in im.info:
            rgba = tile.convert("RGBA")
            white = Image.new("RGB", tile.size, "white")
            white.paste(rgba, mask=rgba.getchannel("A"))
            tile = white
        tile = ImageOps.autocontrast(tile.convert("L"))
        if scale != 1:
            tile = tile.resize((round(width * scale), max(1, round(tile.height * scale))), Image.Resampling.LANCZOS)
        data = _run_tesseract(tile, executable, "11", stop)
        for row in _tsv_lines(data, scale, top, width):
            _, y, _, h = row[0]["bbox"]
            center = y + h / 2
            if (top == 0 or center >= top + OVERLAP / 2) and (end == height or center < end - OVERLAP / 2):
                rows.append(row)
        if end == height:
            break
    return _judge(rows, width, lambda line: _confirm_number(im, line, Image, ImageOps, executable, stop))


def read_poster(data):
    """Return rows for inspection. Only a ready result is suitable for automatic evaluation."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return _result("failed", "pillow_unavailable")
    if len(data) > MAX_BYTES:
        return _result("failed", "image_limit")
    try:
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP", "GIF")) as im:
            width, height = im.size
            if width * height > MAX_PIXELS or max(im.size) > 24_000 or getattr(im, "is_animated", False):
                return _result("failed", "image_limit")
            if width < 240 or height < 150 or width > 7 * height:
                return _result("failed", "decorative_image")
            try:
                return _ocr_image(im, Image, ImageOps)
            except FileNotFoundError:
                return _result("failed", "tesseract_unavailable")
            except subprocess.TimeoutExpired:
                return _result("failed", "ocr_timeout")
            except (subprocess.CalledProcessError, PermissionError):
                return _result("failed", "ocr_failed")
    except Image.DecompressionBombError:
        return _result("failed", "image_limit")
    except (OSError, ValueError):
        return _result("failed", "invalid_image")
