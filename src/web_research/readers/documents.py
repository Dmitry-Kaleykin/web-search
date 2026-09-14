"""Bounded PDF/image extraction in a killable worker; no external inference service."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import signal
import sys
from contextlib import suppress
from typing import Any


async def extract_binary(
    data: bytes, *, content_type: str, page: int, visual: bool
) -> dict[str, Any]:
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "web_research.readers.documents",
        content_type,
        str(page),
        str(int(visual)),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    try:
        output, error = await asyncio.wait_for(worker.communicate(data), timeout=60)
        if worker.returncode:
            raise ValueError(f"Document extraction failed: {error.decode(errors='replace')[-500:]}")
        if len(output) > 8_000_000:
            raise ValueError("Document extraction exceeds output budget")
        return json.loads(output)
    finally:
        if worker.returncode is None:
            with suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(worker.pid, signal.SIGKILL)
                else:
                    worker.kill()
            await worker.wait()


def image_block(image, page: int) -> dict[str, Any]:
    image = image.convert("RGB")
    image.thumbnail((1600, 1600))
    stream = io.BytesIO()
    image.save(stream, format="JPEG", quality=75)
    return {
        "page": page,
        "mime_type": "image/jpeg",
        "data": base64.b64encode(stream.getvalue()).decode(),
    }


def ocr(image) -> tuple[str, str | None]:
    import pytesseract

    try:
        return pytesseract.image_to_string(image, timeout=15).strip(), None
    except pytesseract.TesseractNotFoundError:
        return "", "ocr_unavailable:install_tesseract; use visual=true for main-model inspection"
    except (RuntimeError, pytesseract.TesseractError) as exc:
        return "", f"ocr_failed:{type(exc).__name__}"


def extract(data: bytes, content_type: str, page: int, visual: bool) -> dict[str, Any]:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 16_000_000
    warnings: list[str] = []
    images = []
    if content_type.startswith("image/"):
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            image.thumbnail((2400, 2400))
            text, warning = ocr(image)
            if warning:
                warnings.append(warning)
            if visual or not text:
                images.append(image_block(image, 1))
        return {
            "content": text,
            "title": "Image",
            "warnings": warnings,
            "images": images,
            "method": "http+image+ocr",
        }

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("Encrypted PDF requires a password")
    if page < 1 or page > len(reader.pages):
        raise ValueError(f"PDF page must be between 1 and {len(reader.pages)}")
    count = min(len(reader.pages), 100)
    if count < len(reader.pages):
        warnings.append(f"document_pages_truncated:{count}/{len(reader.pages)}")
    text_parts = []
    total = 0
    render_doc = None
    ocr_pages = 0
    try:
        for index in range(count):
            pdf_page = reader.pages[index]
            text = pdf_page.extract_text(extraction_mode="layout") or ""
            needs_ocr = len(text.strip()) < 40
            render = (visual and index + 1 == page) or (needs_ocr and ocr_pages < 3)
            if render:
                import pypdfium2 as pdfium

                if render_doc is None:
                    render_doc = pdfium.PdfDocument(data)
                raster_page = render_doc[index]
                width, height = raster_page.get_size()
                scale = min(2, 2400 / max(width, height))
                bitmap = raster_page.render(scale=scale)
                image = bitmap.to_pil().copy()
                bitmap.close()
                raster_page.close()
                if needs_ocr:
                    ocr_pages += 1
                    recognized, warning = ocr(image)
                    if warning:
                        warnings.append(warning)
                    if recognized:
                        text = recognized
                        warnings.append(f"ocr_page:{index + 1}; verify numbers against the visual")
                if visual and index + 1 == page:
                    images.append(image_block(image, index + 1))
                image.close()
            if needs_ocr and not text.strip():
                warnings.append(
                    f"document_page_unreadable:{index + 1}; request visual=true,page={index + 1}"
                )
            section = f"## Page {index + 1}\n\n{text.strip()}"
            remaining = 1_000_000 - total
            text_parts.append(section[:remaining])
            total += len(section)
            if total >= 1_000_000:
                warnings.append("content_truncated:pdf")
                break
    finally:
        if render_doc is not None:
            render_doc.close()
    return {
        "content": "\n\n".join(text_parts),
        "title": str((reader.metadata or {}).get("/Title") or "PDF document"),
        "warnings": list(dict.fromkeys(warnings)),
        "images": images,
        "method": "http+pdf+layout",
    }


if __name__ == "__main__":
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
        payload = sys.stdin.buffer.read(5_000_001)
        if len(payload) > 5_000_000:
            raise ValueError("Document exceeds 5 MB worker input limit")
        result = extract(payload, sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1")
        print(json.dumps(result))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
