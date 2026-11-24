"""Recover line-addressable hiring evidence and share readings between job workers."""

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .poster_model import read_poster_model
from .poster_ocr import MAX_BYTES, is_candidate, read_poster
from .public_fetch import fetch_public

VERSION = 1
MAX_IMAGES = 4
RETRY_SECONDS = 600
REASONS = {
    "ocr_ready": "채용 포스터의 직무와 요건을 읽었습니다.",
    "ambiguous_layout": "표에서 직무와 요건이 연결되는 행을 확인해야 합니다.",
    "uncertain_number": "포스터의 수치 또는 날짜를 원본과 대조해야 합니다.",
    "low_confidence": "글자가 흐려 채용 요건을 정확히 확인하기 어렵습니다.",
    "insufficient_text": "포스터에서 직무와 요건을 충분히 읽지 못했습니다.",
    "image_limit": "원본 이미지가 로컬 판독 한도를 초과합니다.",
    "too_many_images": "한 공고의 포스터 처리 한도를 넘어 전체 요건을 확인하지 못했습니다.",
    "decorative_image": "채용 본문으로 읽을 수 있는 이미지가 없습니다.",
    "pillow_unavailable": "OCR 이미지 라이브러리를 사용할 수 없습니다.",
    "tesseract_unavailable": "OCR 실행 파일을 사용할 수 없습니다.",
    "language_unavailable": "OCR에 필요한 한국어·영어 언어 자료를 사용할 수 없습니다.",
    "ocr_timeout": "포스터의 로컬 판독 시간이 초과되었습니다.",
    "ocr_failed": "포스터의 로컬 OCR을 완료하지 못했습니다.",
    "invalid_image": "원본이 지원되는 정지 이미지인지 확인해야 합니다.",
    "download_failed": "공개 원본 이미지를 가져오지 못했습니다.",
    "model_read": "모델이 채용 포스터의 직무와 요건을 판독했습니다.",
    "model_uncertain": "모델 판독 후에도 직무별 행이나 요건이 불확실합니다.",
    "model_image_limit": "원본 이미지가 모델의 단건 판독 한도를 초과합니다.",
    "model_invalid_image": "모델이 원본 이미지 형식을 읽지 못했습니다.",
    "model_unavailable": "포스터 판독에 사용할 모델 환경을 확인해야 합니다.",
    "model_failed": "모델 판독을 완료하지 못했습니다.",
    "model_interrupted": "모델 판독이 중단되어 결과를 확인해야 합니다.",
}
TRANSIENT = {"pillow_unavailable", "tesseract_unavailable", "language_unavailable", "ocr_timeout", "ocr_failed"}
_DETAILS = re.compile(r"담당\s*업무|주요\s*업무|자격\s*(?:요건|조건)|지원\s*자격|우대\s*사항|"
                      r"필수\s*(?:자격|요건)|responsibilities|requirements|qualifications", re.I)
_ROLE_TEXT = re.compile(r"업무|이런 일을|하게 될 일|you will|responsibilit", re.I)
_REQUIREMENT_TEXT = re.compile(r"경험|경력|학력|학위|자격|보유한 분|qualification|experience", re.I)
_IMAGE_REFERENCE = re.compile(r"(?:이미지|포스터|첨부|아래|하단|다음\s*표).{0,24}(?:참조|참고|확인)|see (?:the )?(?:poster|image|attachment)", re.I)


def poster_candidates(doc):
    images = [item for item in doc.get("images", []) if is_candidate(item)]
    if not images:
        return []
    source = doc.get("poster_evidence") or {}
    labels = {item.get("text") for item in doc.get("links", []) if isinstance(item, dict)}
    labels.update(item.get("alt") for item in images)
    labels.add(doc.get("title"))
    text = " ".join(line.strip() for line in source.get("source_lines", doc.get("lines", []))
                    if isinstance(line, str) and line.strip() and line not in labels)
    if not _IMAGE_REFERENCE.search(text):
        if len(text) >= 80 and _ROLE_TEXT.search(text) and _REQUIREMENT_TEXT.search(text):
            return []
        headings = _DETAILS.findall(text)
        if headings and (len(text) >= 80 or (len(text) >= 24 and len(headings) >= 2)):
            return []
        if doc.get("jd_source") and headings and len(_DETAILS.sub("", text).strip()) >= 12:
            return []
    seen = set()
    unique = []
    for image in images:
        if image["url"] not in seen:
            seen.add(image["url"])
            unique.append(image)
    return unique


def _save(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temp, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temp.unlink(missing_ok=True)


def _save_json(path, value):
    _save(path, json.dumps(value, ensure_ascii=False).encode())


def _load(path):
    return json.loads(path.read_bytes()) if path.is_file() else {}


@contextmanager
def _lock(path):
    # Locks span the external call. The OS releases them if the worker exits.
    with path.open("ab") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _failure(reason):
    return {"status": "failed", "reason": reason, "lines": []}


def _source(cache, url, revision):
    key = hashlib.sha256(json.dumps([url, revision]).encode()).hexdigest()
    path = cache / ("url-" + key + ".json")
    with _lock(path.with_suffix(".lock")):
        saved = _load(path)
        if saved.get("sha256") or saved.get("retry_after", 0) > time.time():
            return saved
        try:
            final_url, data, _ = fetch_public(url, deadline=time.monotonic() + 12, limit=MAX_BYTES)
        except (ValueError, OSError):
            saved = {"retry_after": time.time() + RETRY_SECONDS}
        else:
            digest = hashlib.sha256(data).hexdigest()
            original = cache / (digest + ".image")
            if not original.exists():
                _save(original, data)
            saved = {"sha256": digest, "final_url": final_url}
        _save_json(path, saved)
        return saved


def _read_image(cache, url, revision, model_budget):
    source = _source(cache, url, revision)
    digest = source.get("sha256")
    if not digest:
        return {"url": url, "method": "ocr", "retry_after": source["retry_after"], **_failure("download_failed")}
    original = cache / (digest + ".image")
    key = cache / f"{digest}-v{VERSION}"
    with _lock(original.with_suffix(".lock")):
        local_path = key.with_suffix(".ocr.json")
        saved = _load(local_path)
        if not saved or (saved["reason"] in TRANSIENT and time.time() >= local_path.stat().st_mtime + RETRY_SECONDS):
            saved = read_poster(original.read_bytes())
            _save_json(local_path, saved)
        method = "ocr"
        if saved["reason"] in TRANSIENT:
            saved["retry_after"] = local_path.stat().st_mtime + RETRY_SECONDS
        if saved["status"] == "uncertain":
            model_path = original.with_suffix(".model.json")
            model = _load(model_path)
            if model_budget and (not model or (model["reason"] == "model_unavailable"
                    and time.time() >= model_path.stat().st_mtime + RETRY_SECONDS)):
                model_budget.pop()
                # Commit the reservation before submitting. A lost response must not trigger another charge.
                _save_json(model_path, _failure("model_interrupted"))
                model = read_poster_model(original.read_bytes())
                _save_json(model_path, model)
            if model:
                saved = {**model, "ocr_reason": saved["reason"]}
                method = "model"
    return {"url": url, "final_url": source["final_url"], "sha256": digest, "method": method, **saved}


def enrich_document(jobs_path, doc, *, allow_model=False):
    candidates = poster_candidates(doc)
    if not candidates:
        return doc
    cache = Path(str(Path(jobs_path).resolve()) + ".posters")
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    revision = [doc.get("modified_gmt"), doc.get("jd_modified_gmt")]
    model_budget = [1] if allow_model else []
    images = [_read_image(cache, candidate["url"], revision, model_budget) for candidate in candidates[:MAX_IMAGES]]
    for im in images:
        im["reason_code"] = im["reason"]
        im["reason"] = REASONS[im["reason_code"]]
    # Small decorative rasters are only recognizable after decoding their dimensions.
    substantive = [im for im in images if im["reason_code"] != "decorative_image"]
    issue = next((im for im in substantive if im["status"] != "ready"), None)
    if len(candidates) > MAX_IMAGES:
        status, reason = "uncertain", REASONS["too_many_images"]
    elif not substantive:
        status, reason = "uncertain", REASONS["decorative_image"]
    elif issue:
        status, reason = issue["status"], issue["reason"]
    else:
        status, reason = "ready", substantive[0]["reason"]
    previous = doc.get("poster_evidence") or {}
    source_lines = previous.get("source_lines", doc.get("lines", []))
    content_status = previous.get("source_content_status", doc.get("content_status", "needs_review"))
    lines = list(source_lines)
    if status == "ready":
        for im in substantive:
            for row in im["lines"]:
                lines.append(row["text"])
                row["line_number"] = len(lines)
    return {**doc, "lines": lines, "content_status": "text_available" if status == "ready" else "needs_review",
            "poster_evidence": {"version": VERSION, "status": status, "reason": reason, "images": images,
                                "source_lines": source_lines, "source_content_status": content_status}}
