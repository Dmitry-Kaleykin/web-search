from __future__ import annotations

import base64
import io
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from web_research.readers.documents import extract_binary


def text_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 14 Tf 50 720 Td (Product specifications verified by laboratory testing.) Tj "
        b"0 -30 Td (Model A                 49 USD) Tj "
        b"0 -30 Td (Model B                 99 USD) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def scanned_pdf() -> bytes:
    image = Image.new("RGB", (1400, 500), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=48)
    draw.text((60, 100), "Verified measurement: 42 units", fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="PDF")
    return output.getvalue()


async def test_pdf_layout_and_page_image_are_readable():
    result = await extract_binary(text_pdf(), content_type="application/pdf", page=1, visual=True)
    assert "## Page 1" in result["content"]
    assert "Model A" in result["content"] and "49 USD" in result["content"]
    image = Image.open(io.BytesIO(base64.b64decode(result["images"][0]["data"])))
    assert image.width > 500
    assert result["images"][0]["page"] == 1


async def test_out_of_range_pdf_page_is_reported():
    with pytest.raises(ValueError, match="PDF page must be between"):
        await extract_binary(text_pdf(), content_type="application/pdf", page=2, visual=True)


async def test_corrupt_pdf_is_reported():
    with pytest.raises(ValueError, match="Document extraction failed"):
        await extract_binary(
            b"%PDF-not-valid", content_type="application/pdf", page=1, visual=False
        )


@pytest.mark.skipif(
    not shutil.which("tesseract"), reason="Tesseract executable is required for OCR integration"
)
async def test_scanned_pdf_uses_real_local_ocr():
    result = await extract_binary(
        scanned_pdf(), content_type="application/pdf", page=1, visual=True
    )
    assert "42" in result["content"]
    assert any(warning.startswith("ocr_page:1") for warning in result["warnings"])
    assert result["images"]


async def test_missing_ocr_still_returns_visual_evidence(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    result = await extract_binary(
        scanned_pdf(), content_type="application/pdf", page=1, visual=True
    )
    assert any(warning.startswith("ocr_unavailable:") for warning in result["warnings"])
    assert result["images"]


async def test_pdf_routes_through_the_http_reader_without_browser():
    import httpx

    from web_research.readers.http import HTTPReader
    from web_research.readers.router import LayeredReader

    primary = HTTPReader(allow_private_urls=True)
    await primary._client.aclose()
    primary._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=text_pdf(), headers={"content-type": "application/octet-stream"}
            )
        )
    )
    reader = LayeredReader(primary)
    try:
        document = await reader.read("http://127.0.0.1/download?ref=report", visual=True)
        assert document.content_type == "application/pdf"
        assert "49 USD" in document.content
        assert document.images
    finally:
        await reader.close()


async def test_cancellation_stops_worker_and_ocr_process_group(monkeypatch):
    import asyncio
    import os
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    if os.name != "posix":
        pytest.skip("Process groups are POSIX-specific")
    started = asyncio.Event()

    async def communicate(_data):
        started.set()
        await asyncio.Event().wait()

    worker = SimpleNamespace(pid=12345, returncode=None, communicate=communicate, wait=AsyncMock())
    spawn = AsyncMock(return_value=worker)
    kill = Mock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(os, "killpg", kill)
    task = asyncio.create_task(
        extract_binary(b"pdf", content_type="application/pdf", page=1, visual=False)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spawn.call_args.kwargs["start_new_session"]
    assert kill.call_args.args[0] == 12345
    worker.wait.assert_awaited_once()
