"""Normalize inline SDK attachments for the stand-in CLIs. Never fetch URLs or open caller-supplied paths."""
import base64
import binascii
import copy
import json
import mimetypes
from pathlib import Path
import re
import shutil
import subprocess

MAX_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 20
IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


class InputError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def decode(data, mime):
    if not isinstance(mime, str):
        raise InputError("attachment media type must be a string")
    if not isinstance(data, str):
        raise InputError("attachment needs inline base64 data")
    if data.startswith("data:"):
        header, sep, data = data.partition(",")
        if not sep or not header.endswith(";base64"):
            raise InputError("attachment data URLs must use base64")
        mime = header[5:-7]
    if len(data) > (MAX_BYTES + 2) // 3 * 4:
        raise InputError("attachment exceeds 20 MiB", 413)
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise InputError("attachment contains invalid base64") from None
    if not raw or len(raw) > MAX_BYTES:
        raise InputError("attachment must contain 1 byte to 20 MiB", 413)
    return raw, mime


def prepare(request, folder):
    """Return a copy with readable text/placeholders, plus ordered image paths (PDF pages included)."""
    request, images = copy.deepcopy(request), []

    def image(raw, mime):
        valid = ((mime == "image/png" and raw.startswith(b"\x89PNG\r\n\x1a\n")) or
                 (mime == "image/jpeg" and raw.startswith(b"\xff\xd8\xff")) or
                 (mime == "image/gif" and raw.startswith((b"GIF87a", b"GIF89a"))) or
                 (mime == "image/webp" and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"))
        if not valid:
            raise InputError("unsupported image type or image bytes do not match its media type")
        if len(images) >= MAX_IMAGES:
            raise InputError("at most 20 images/PDF pages are supported per request", 413)
        if len(raw) + sum(p.stat().st_size for p in images) > MAX_BYTES:
            raise InputError("combined image data exceeds 20 MiB", 413)
        path = folder / f"image-{len(images) + 1}{IMAGE_EXT[mime]}"
        path.write_bytes(raw)  # folder is a private TemporaryDirectory (0700)
        path.chmod(0o600)
        images.append(path)
        return f"[attached image {len(images)}]"

    def pdf(raw):
        if not raw.startswith(b"%PDF-"):
            raise InputError("PDF bytes do not match the media type")
        if not shutil.which("pdfinfo") or not shutil.which("pdftoppm"):
            raise InputError("PDF input requires Poppler (pdfinfo and pdftoppm); install poppler/poppler-utils", 501)
        path = folder / f"document-{len(images)}.pdf"
        path.write_bytes(raw)
        path.chmod(0o600)
        try:
            info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=30, check=True)
            count = re.search(r"^Pages:\s+(\d+)", info.stdout, re.M)
            if not count:
                raise InputError("could not determine PDF page count")
            pages = int(count.group(1))
            if pages < 1 or pages + len(images) > MAX_IMAGES:
                raise InputError("at most 20 images/PDF pages are supported per request", 413)
            prefix = folder / "page"
            subprocess.run(["pdftoppm", "-png", "-scale-to", "1568", str(path), str(prefix)],
                           capture_output=True, timeout=30, check=True)
            files = sorted(folder.glob("page-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
            if len(files) != pages or sum(p.stat().st_size for p in files) > MAX_BYTES:
                raise InputError("PDF rendering is incomplete or exceeds 20 MiB", 413)
            labels = [image(p.read_bytes(), "image/png") for p in files]
            for p in files:
                p.unlink()
            return "[PDF pages: " + ", ".join(labels) + "]"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise InputError("PDF could not be rendered (invalid, encrypted, or conversion timed out)") from e

    def blocks(content):
        if not isinstance(content, list):
            return content
        result = []
        for block in content:
            if not isinstance(block, dict):
                raise InputError("message content blocks must be objects")
            kind = block.get("type")
            if kind == "tool_result":
                block["content"] = blocks(block.get("content"))
            if kind not in ("image", "image_url", "document", "file"):
                result.append(block)
                continue
            if kind == "image_url":
                source = block.get("image_url", {})
                if not isinstance(source, (str, dict)):
                    raise InputError("image_url must be a URL string or object")
                source = source if isinstance(source, str) else source.get("url", "")
                if not isinstance(source, str):
                    raise InputError("image_url.url must be a string")
                if not source.startswith("data:"):
                    raise InputError("use inline base64/data URLs; remote image URLs are not fetched", 501)
                raw, mime = decode(source, "")
            elif kind == "file":
                source = block.get("file") or {}
                if not isinstance(source, dict):
                    raise InputError("file must be an object")
                if "file_id" in source:
                    raise InputError("provider file IDs are not available locally; send inline file_data instead", 501)
                filename = source.get("filename", "")
                if not isinstance(filename, str):
                    raise InputError("file filename must be a string")
                mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                raw, mime = decode(source.get("file_data"), mime)
            else:
                source = block.get("source") or {}
                if not isinstance(source, dict):
                    raise InputError("attachment source must be an object")
                if source.get("type") == "text":
                    if not isinstance(source.get("data"), str):
                        raise InputError("text document data must be a string")
                    if len(source["data"].encode()) > MAX_BYTES:
                        raise InputError("text document exceeds 20 MiB", 413)
                    result.append({"type": "text", "text": "[attached document]\n" + source["data"]})
                    continue
                if source.get("type") != "base64":
                    raise InputError("use inline base64/text sources; remote URLs and provider file IDs are unsupported", 501)
                raw, mime = decode(source.get("data"), source.get("media_type", ""))
            if mime in IMAGE_EXT:
                text = image(raw, mime)
            elif mime == "application/pdf":
                text = pdf(raw)
            elif mime.startswith("text/") or mime in ("application/json", "application/xml", "application/octet-stream"):
                try:
                    text = "[attached document]\n" + raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise InputError("file must be UTF-8 text, PDF, PNG, JPEG, GIF or WebP", 501) from None
                if "\x00" in text:
                    raise InputError("binary files are unsupported; send text, PDF or a supported image", 501)
            else:
                raise InputError(f"unsupported attachment media type: {mime}", 501)
            result.append({"type": "text", "text": text})
        return result

    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            raise InputError("messages must contain objects")
        message["content"] = blocks(message.get("content"))
    return request, images


def claude_input(prompt, images):
    content = [{"type": "text", "text": prompt}]
    for path in images:
        content.append({"type": "image", "source": {"type": "base64", "media_type": mimetypes.guess_type(path)[0],
                                                     "data": base64.b64encode(path.read_bytes()).decode()}})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}) + "\n"
