#!/usr/bin/env python3
"""
Rich Notes Bridge for macOS Apple Notes
---------------------------------------
Enables external agents (e.g. Muse) to create rich notes in Apple Notes
with true native attachments (images, PDFs, documents), rich formatting
(tables, checklists, code blocks, typography), and folder placement — and to
export the full body of an existing note (plaintext + HTML) back out, for
connectors whose own Notes read path truncates large notes.

Stock macOS tools only (Python 3 standard library + native osascript AppleScript).
Zero network, zero third-party dependencies, completely local and private.
"""

import argparse
import base64
import datetime
import hashlib
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Default Paths
DEFAULT_BASE_DIR = Path(os.environ.get("RICH_NOTES_BRIDGE_DIR", os.path.expanduser("~/.rich-notes-bridge")))
LAUNCH_AGENT_LABEL = "com.rich-notes-bridge.agent"
LAUNCH_AGENT_PATH = Path(os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCH_AGENT_LABEL}.plist"))

logger = logging.getLogger("rich_notes_bridge")


class BridgeConfig:
    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = (base_dir or DEFAULT_BASE_DIR).resolve()
        self.inbox_dir = self.base_dir / "inbox"
        self.outbox_dir = self.base_dir / "outbox"
        self.archive_dir = self.base_dir / "archive"
        self.state_dir = self.base_dir / "state"
        self.logs_dir = self.base_dir / "logs"
        self.dedup_file = self.state_dir / "dedup.json"
        self.log_file = self.logs_dir / "bridge.log"

    def ensure_directories(self):
        for d in [self.base_dir, self.inbox_dir, self.outbox_dir, self.archive_dir, self.state_dir, self.logs_dir]:
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except Exception:
                pass


def setup_logging(config: BridgeConfig, verbose: bool = False):
    config.ensure_directories()
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)
    logger.handlers = []

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File handler
    try:
        fh = logging.FileHandler(config.log_file, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception as e:
        logger.warning(f"Could not initialize file handler at {config.log_file}: {e}")


# ----------------------------------------------------------------------
# Deduplication State Management
# ----------------------------------------------------------------------
class DedupManager:
    def __init__(self, dedup_path: Path):
        self.path = dedup_path
        self.data = {"by_id": {}, "by_content_hash": {}}
        self._last_mtime = 0
        self._load()

    def _ensure_latest(self):
        try:
            if self.path.exists():
                mtime = self.path.stat().st_mtime
                if mtime > self._last_mtime:
                    self._load()
        except Exception:
            pass

    def _load(self):
        if self.path.exists():
            try:
                self._last_mtime = self.path.stat().st_mtime
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.data["by_id"] = loaded.get("by_id", {})
                    self.data["by_content_hash"] = loaded.get("by_content_hash", {})
            except Exception as e:
                logger.error(f"Error loading dedup state from {self.path}: {e}")

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.path)
            self._last_mtime = self.path.stat().st_mtime
        except Exception as e:
            logger.error(f"Error saving dedup state to {self.path}: {e}")

    def check_duplicate(self, request_id: Optional[str], content_hash: str) -> Optional[Dict[str, Any]]:
        """Returns existing record if duplicate, else None."""
        self._ensure_latest()
        if request_id and request_id in self.data["by_id"]:
            return self.data["by_id"][request_id]
        if content_hash in self.data["by_content_hash"]:
            return self.data["by_content_hash"][content_hash]
        return None

    def record(self, request_id: Optional[str], content_hash: str, note_record: Dict[str, Any]):
        self._ensure_latest()
        if request_id:
            self.data["by_id"][request_id] = note_record
        if content_hash:
            self.data["by_content_hash"][content_hash] = note_record
        self.save()


# ----------------------------------------------------------------------
# Image & Attachment Utilities
# ----------------------------------------------------------------------
# ISO Base Media File Format (ISO/IEC 14496-12) brand -> extension. Covers the
# MPEG-4 family (QuickTime, MP4, M4A/M4B audio, HEIF/HEIC). Checked against the
# major brand first, then the compatible-brands list (some muxers write a
# generic major brand like "isom" with the specific one only in the compat list).
_FTYP_BRANDS = {
    b"M4A ": ".m4a",
    b"M4B ": ".m4a",
    b"qt  ": ".mov",
    b"heic": ".heic",
    b"heix": ".heic",
    b"mif1": ".heic",
    b"msf1": ".heic",
    b"isom": ".mp4",
    b"iso2": ".mp4",
    b"mp41": ".mp4",
    b"mp42": ".mp4",
    b"avc1": ".mp4",
    b"dash": ".mp4",
    b"M4V ": ".mp4",
}


def _detect_ftyp_extension(data: bytes) -> Optional[str]:
    if len(data) < 12 or data[4:8] != b"ftyp":
        return None
    ext = _FTYP_BRANDS.get(data[8:12])
    if ext:
        return ext
    # Compatible brands: 4-byte entries from offset 16 to the end of the ftyp box.
    box_size = int.from_bytes(data[0:4], "big")
    end = min(box_size if box_size >= 16 else 16, len(data))
    for off in range(16, end - 3, 4):
        ext = _FTYP_BRANDS.get(data[off:off + 4])
        if ext:
            return ext
    return None


def detect_file_extension(data: bytes, fallback: str = ".bin") -> str:
    """
    Detect file extension from magic bytes for common media and docs.

    Unknown types fall back to ".bin" (previously ".png", which turned any
    unnamed audio/video blob into a corrupt image attachment). ZIP-container
    formats (.docx/.xlsx/.pages/...) are indistinguishable from .zip by magic
    bytes alone; a real filename from the manifest always wins upstream, so
    detection only runs for nameless / extensionless entries.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"RIFF") and len(data) >= 12:
        riff_type = data[8:12]
        if riff_type == b"WEBP":
            return ".webp"
        if riff_type == b"WAVE":
            return ".wav"
        if riff_type == b"AVI ":
            return ".avi"
    ftyp_ext = _detect_ftyp_extension(data)
    if ftyp_ext:
        return ftyp_ext
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"PK\x03\x04"):
        return ".zip"
    if data.startswith(b"MM\x00*") or data.startswith(b"II*\x00"):
        return ".tiff"
    if data.startswith(b"fLaC"):
        return ".flac"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"ID3"):
        return ".mp3"
    # MPEG audio frame sync: 11 set bits. Checked after JPEG (FF D8 does not
    # satisfy the 0xE0 mask, so there is no overlap either way).
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # Layer bits == 00 with a 12-bit sync is AAC ADTS, not MPEG layer I-III.
        if (data[1] & 0xF6) == 0xF0:
            return ".aac"
        return ".mp3"
    if data.startswith(b"BM"):
        return ".bmp"
    return fallback


def decode_base64_string(raw: str) -> bytes:
    """
    Clean and decode base64 string, handling data URIs and whitespace.

    Strict: any character outside the base64 alphabet (after whitespace is
    removed) raises ValueError. The stdlib default (validate=False) silently
    discards such characters, which let a nonexistent file path like
    "clips/stoats.m4a" decode into a few bytes of garbage that were then
    attached as if they were the real file. URL-safe alphabet (-_) and missing
    padding are accepted.
    """
    s = raw.strip()
    if s.startswith("data:") and ";base64," in s:
        s = s.split(";base64,", 1)[1]
    s = re.sub(r"\s+", "", s)
    if "-" in s or "_" in s:
        s = s.replace("-", "+").replace("_", "/")
    s = s.rstrip("=")
    if len(s) % 4 == 1:
        raise ValueError("invalid base64 length")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s, validate=True)
    except Exception as e:
        raise ValueError(f"invalid base64: {e}") from e


class AttachmentLoadError(ValueError):
    """A manifest referenced an attachment that could not be loaded (missing
    file, undecodable base64, empty value). Always a hard failure: the request
    errors and no note is created/modified (never a silent partial success)."""


# ----------------------------------------------------------------------
# Markdown to HTML Formatter (Stock macOS compliant)
# ----------------------------------------------------------------------
def markdown_to_html(md_text: str) -> str:
    """
    Lightweight, dependency-free Markdown to Notes-compatible HTML converter.
    Handles headings, bold, italic, strikethrough, checklists, tables,
    blockquotes, code blocks, lists, links, and paragraphs.
    """
    lines = md_text.splitlines()
    html_parts = []
    in_code_block = False
    code_block_lines = []
    in_ul = False
    in_ol = False
    in_table = False
    table_rows = []

    def close_lists():
        nonlocal in_ul, in_ol
        res = ""
        if in_ul:
            res += "</ul>"
            in_ul = False
        if in_ol:
            res += "</ol>"
            in_ol = False
        return res

    def flush_table():
        nonlocal in_table, table_rows
        if not table_rows:
            return ""
        res = ['<table border="1" cellpadding="6" style="border-collapse: collapse; border: 1px solid #ddd; margin: 10px 0; width: 100%;">']
        for i, row in enumerate(table_rows):
            tag = "th" if i == 0 else "td"
            bg = ' style="background-color: #f7f7f8; text-align: left;"' if i == 0 else ""
            cells_html = "".join(f"<{tag}>{apply_inline_formatting(c)}</{tag}>" for c in row)
            res.append(f"<tr{bg}>{cells_html}</tr>")
        res.append("</table>")
        in_table = False
        table_rows = []
        return "\n".join(res)

    for line in lines:
        stripped = line.strip()

        # Code blocks
        if stripped.startswith("```"):
            if in_code_block:
                code_content = html.escape("\n".join(code_block_lines))
                html_parts.append(
                    f'<pre style="background:#f4f4f4;padding:8px;border-radius:4px;font-family:monospace;font-size:13px;"><code>{code_content}</code></pre>'
                )
                code_block_lines = []
                in_code_block = False
            else:
                html_parts.append(close_lists())
                if in_table:
                    html_parts.append(flush_table())
                in_code_block = True
            continue

        if in_code_block:
            code_block_lines.append(line)
            continue

        # Tables: | Col 1 | Col 2 |
        if stripped.startswith("|") and stripped.endswith("|"):
            html_parts.append(close_lists())
            raw_cells = [c.strip() for c in stripped.strip("|").split("|")]
            # Divider row check: |---|---|
            if all(re.match(r"^:?-+:?$", c) for c in raw_cells):
                continue
            table_rows.append(raw_cells)
            in_table = True
            continue
        elif in_table:
            html_parts.append(flush_table())

        # Checklists: - [ ] Task or - [x] Done
        chk_match = re.match(r"^[\*\-\+]\s+\[([ xX])\]\s+(.*)$", stripped)
        if chk_match:
            html_parts.append(close_lists())
            is_checked = chk_match.group(1).lower() == "x"
            box = "☑" if is_checked else "☐"
            task_text = apply_inline_formatting(chk_match.group(2))
            if is_checked:
                html_parts.append(f"<div>{box} <s>{task_text}</s></div>")
            else:
                html_parts.append(f"<div>{box} {task_text}</div>")
            continue

        # Blank lines
        if not stripped:
            html_parts.append(close_lists())
            html_parts.append("<div><br></div>")
            continue

        # Horizontal rule
        if stripped in ["---", "***", "___"]:
            html_parts.append(close_lists())
            html_parts.append('<hr style="border:0;border-top:1px solid #ddd;margin:12px 0;">')
            continue

        # Blockquote
        if stripped.startswith(">"):
            html_parts.append(close_lists())
            quote_text = apply_inline_formatting(stripped.lstrip("> ").strip())
            html_parts.append(
                f'<blockquote style="border-left:3px solid #ccc;padding-left:10px;color:#555;margin:6px 0;">{quote_text}</blockquote>'
            )
            continue

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            html_parts.append(close_lists())
            level = len(heading_match.group(1))
            heading_text = apply_inline_formatting(heading_match.group(2))
            sizes = {1: "24px", 2: "20px", 3: "17px", 4: "15px", 5: "14px", 6: "13px"}
            font_size = sizes.get(level, "16px")
            html_parts.append(f'<div><b><span style="font-size: {font_size};">{heading_text}</span></b></div>')
            continue

        # Unordered list
        ul_match = re.match(r"^[\*\-\+]\s+(.*)$", stripped)
        if ul_match:
            if not in_ul:
                html_parts.append(close_lists())
                html_parts.append("<ul>")
                in_ul = True
            item_text = apply_inline_formatting(ul_match.group(1))
            html_parts.append(f"<li>{item_text}</li>")
            continue

        # Ordered list
        ol_match = re.match(r"^\d+\.\s+(.*)$", stripped)
        if ol_match:
            if not in_ol:
                html_parts.append(close_lists())
                html_parts.append("<ol>")
                in_ol = True
            item_text = apply_inline_formatting(ol_match.group(1))
            html_parts.append(f"<li>{item_text}</li>")
            continue

        # Regular line
        html_parts.append(close_lists())
        line_content = apply_inline_formatting(stripped)
        html_parts.append(f"<div>{line_content}</div>")

    if in_code_block:
        code_content = html.escape("\n".join(code_block_lines))
        html_parts.append(f"<pre><code>{code_content}</code></pre>")

    if in_table:
        html_parts.append(flush_table())

    html_parts.append(close_lists())
    return "\n".join(p for p in html_parts if p)


def apply_inline_formatting(text: str) -> str:
    """Applies inline bold, italic, strikethrough, code, links, and markdown image captions."""
    # Strikethrough: ~~text~~
    text = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", text)

    # Markdown images: ![alt](url) -> formatted image placeholder caption
    text = re.sub(r"!\[(.*?)\]\([^\)]+\)", r'<div style="color:#666;font-size:13px;margin:4px 0;">📷 <b>\1</b></div>', text)

    # Markdown links: [text](url) -> <a href="url">text</a>
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r'<a href="\2">\1</a>', text)

    # Inline code: `code`
    text = re.sub(r"`([^`]+)`", r'<code style="background:#f4f4f4;padding:2px 4px;border-radius:3px;font-family:monospace;">\1</code>', text)

    # Bold: **bold** or __bold__
    text = re.sub(r"\*\*([^\*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^_]+)__", r"<b>\1</b>", text)

    # Italic: *italic* or _italic_
    text = re.sub(r"\*([^\*]+)\*", r"<i>\1</i>", text)
    text = re.sub(r"_([^_]+)_", r"<i>\1</i>", text)

    return text


# ----------------------------------------------------------------------
# Request Parsing
# ----------------------------------------------------------------------
class RichNoteRequest:
    def __init__(
        self,
        request_id: str,
        title: str,
        folder: str,
        body: str,
        format_type: str,
        attachments: Dict[str, bytes],
        raw_source_path: Path,
    ):
        self.request_id = request_id
        self.title = title or "Untitled Note"
        self.folder = folder or "Notes"
        self.body = body or ""
        self.format_type = format_type.lower() if format_type else "auto"
        self.attachments = attachments  # filename -> decoded bytes
        self.raw_source_path = raw_source_path
        # Keys of `attachments` whose name was synthesized by the parser (not
        # sent), mapped to a staging-filename hint or None. Not part of the
        # content hash. See _extract_attachments.
        self.auto_named: Dict[str, Optional[str]] = {}

    def compute_content_hash(self) -> str:
        """Deterministic SHA-256 hash of content for deduplication."""
        h = hashlib.sha256()
        h.update(self.folder.strip().encode("utf-8"))
        h.update(b"|")
        h.update(self.title.strip().encode("utf-8"))
        h.update(b"|")
        h.update(self.body.strip().encode("utf-8"))
        for name in sorted(self.attachments.keys()):
            h.update(b"|att:")
            h.update(name.encode("utf-8"))
            h.update(b":")
            h.update(hashlib.sha256(self.attachments[name]).digest())
        return h.hexdigest()


class ExportRequest:
    """A read-only request to export an existing note's full body. Deliberately
    does not share RichNoteRequest's shape (no folder/body/format/attachments
    defaults make sense here) — export has its own request and receipt schema."""

    def __init__(self, request_id: str, note_id: Optional[str], title: Optional[str], raw_source_path: Path):
        self.request_id = request_id
        self.note_id = note_id or None
        self.title = title or None
        self.raw_source_path = raw_source_path


class AttachRequest:
    """
    "op": "attach" — append attachments to an EXISTING note. Target by
    `note_id` (x-coredata://... id, or the note's UUID identifier, mapped via
    the Notes SQLite store), or fall back to exact `title` (+ optional
    `folder`) that must match exactly one note.
    """

    def __init__(
        self,
        request_id: str,
        note_id: Optional[str],
        title: Optional[str],
        folder: Optional[str],
        attachments: Dict[str, bytes],
        auto_named: Dict[str, Optional[str]],
        force: bool,
        raw_source_path: Path,
    ):
        self.request_id = request_id
        self.note_id = (note_id or "").strip() or None
        self.title = title or None
        self.folder = folder or None
        self.attachments = attachments
        self.auto_named = auto_named
        self.force = force
        self.raw_source_path = raw_source_path

    def compute_content_hash(self) -> str:
        """Hash of target + attachment bytes. Prefixed "attach:" when stored so
        it can never collide with a create request's content hash."""
        h = hashlib.sha256()
        h.update(b"attach|")
        h.update((self.note_id or "").encode("utf-8"))
        h.update(b"|")
        h.update((self.folder or "").strip().encode("utf-8"))
        h.update(b"|")
        h.update((self.title or "").strip().encode("utf-8"))
        for name in sorted(self.attachments.keys()):
            h.update(b"|att:")
            h.update(name.encode("utf-8"))
            h.update(b":")
            h.update(hashlib.sha256(self.attachments[name]).digest())
        return h.hexdigest()


def _manifest_path_for_dir(dir_path: Path) -> Optional[Path]:
    for cand in ("manifest.json", "request.json"):
        p = dir_path / cand
        if p.is_file():
            return p
    return None


def _detect_op(item_path: Path, orig_name: str) -> str:
    """Peeks at a single-JSON-file request's "op" field without fully parsing it
    as a create request. Returns "create" (the default) for directories,
    non-JSON files, missing/null "op", or any JSON that fails to parse — in
    every one of those cases the existing create pipeline's own error handling
    is what should run, unchanged, exactly as before this field existed."""
    if item_path.is_dir():
        # Directory drops: only "attach" is recognized from manifest.json; every
        # other value keeps the historical behavior (directory = create).
        mp = _manifest_path_for_dir(item_path)
        if not mp:
            return "create"
        try:
            with open(mp, "r", encoding="utf-8") as f:
                meta = json.load(f)
            op = str(meta.get("op") or "create").lower() if isinstance(meta, dict) else "create"
            return "attach" if op == "attach" else "create"
        except Exception:
            return "create"
    if Path(orig_name).suffix.lower() != ".json":
        return "create"
    try:
        with open(item_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        return str(meta.get("op") or "create").lower()
    except Exception:
        return "create"


def _peek_request_id(item_path: Path, default_id: str) -> Optional[str]:
    """Best-effort read of a create request's "id" without loading attachments."""
    try:
        if item_path.is_dir():
            mp = _manifest_path_for_dir(item_path)
            if mp:
                with open(mp, "r", encoding="utf-8") as f:
                    return json.load(f).get("id") or default_id
            return default_id
        if item_path.name.replace(".processing", "").lower().endswith(".json"):
            with open(item_path, "r", encoding="utf-8") as f:
                return json.load(f).get("id") or default_id
        if item_path.name.replace(".processing", "").lower().endswith((".txt", ".md")):
            with open(item_path, "r", encoding="utf-8") as f:
                return _parse_frontmatter_text(f.read()).get("id") or default_id
    except Exception:
        pass
    return default_id


def _parse_attach_request(item_path: Path) -> AttachRequest:
    if item_path.is_dir():
        manifest = _manifest_path_for_dir(item_path)
        if not manifest:
            raise ValueError("attach directory has no manifest.json")
        base_dir = item_path
        orig_name = item_path.name
    else:
        manifest = item_path
        base_dir = item_path.parent
        orig_name = item_path.name
    if orig_name.endswith(".processing"):
        orig_name = orig_name[:-len(".processing")]

    with open(manifest, "r", encoding="utf-8") as f:
        meta = json.load(f)

    req_id = meta.get("id") or (orig_name if item_path.is_dir() else Path(orig_name).stem)
    attachments: Dict[str, bytes] = {}
    auto_named: Dict[str, Optional[str]] = {}
    for k in ["images", "attachments"]:
        if k in meta:
            _extract_attachments(meta[k], attachments, base_dir, key=k, auto_named=auto_named)

    return AttachRequest(
        request_id=req_id,
        note_id=meta.get("note_id"),
        title=meta.get("title"),
        folder=meta.get("folder"),
        attachments=attachments,
        auto_named=auto_named,
        force=bool(meta.get("force", False)),
        raw_source_path=item_path,
    )


def _parse_export_request(json_path: Path) -> ExportRequest:
    with open(json_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    orig_name = json_path.name
    if orig_name.endswith(".processing"):
        orig_name = orig_name[:-len(".processing")]

    req_id = meta.get("id") or Path(orig_name).stem
    note_id = meta.get("note_id")
    title = meta.get("title")

    return ExportRequest(request_id=req_id, note_id=note_id, title=title, raw_source_path=json_path)


def parse_request_from_path(item_path: Path) -> Optional[RichNoteRequest]:
    """
    Parses a request from:
    1. A directory containing manifest.json (or manifest.txt) + companion files
    2. A single .json file with embedded base64 attachments/images
    3. A single .txt / .md file with frontmatter
    """
    try:
        if item_path.is_dir():
            return _parse_directory_request(item_path)

        orig_name = item_path.name
        if orig_name.endswith(".processing"):
            orig_name = orig_name[:-len(".processing")]
        orig_suffix = Path(orig_name).suffix.lower()

        if orig_suffix == ".json":
            return _parse_json_file_request(item_path)
        elif orig_suffix in [".txt", ".md"]:
            return _parse_text_file_request(item_path)
        else:
            logger.debug(f"Ignoring unrecognized file in inbox: {item_path.name}")
            return None
    except Exception as e:
        logger.error(f"Error parsing request from {item_path}: {e}", exc_info=not isinstance(e, AttachmentLoadError))
        raise


def _parse_directory_request(dir_path: Path) -> RichNoteRequest:
    req_id = dir_path.name
    if req_id.endswith(".processing"):
        req_id = req_id[:-len(".processing")]

    title = ""
    folder = "Notes"
    body = ""
    format_type = "auto"
    attachments: Dict[str, bytes] = {}
    auto_named: Dict[str, Optional[str]] = {}

    manifest_candidates = [
        dir_path / "manifest.json",
        dir_path / "request.json",
        dir_path / "manifest.yaml",
        dir_path / "manifest.yml",
        dir_path / "manifest.txt",
        dir_path / "manifest.md",
    ]

    manifest_path = None
    for cand in manifest_candidates:
        if cand.exists():
            manifest_path = cand
            break

    if manifest_path and manifest_path.suffix == ".json":
        with open(manifest_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        req_id = meta.get("id") or req_id
        title = meta.get("title", "")
        folder = meta.get("folder") or folder
        body = meta.get("body", "")
        format_type = meta.get("format", "auto")

        for k in ["images", "attachments"]:
            if k in meta:
                _extract_attachments(meta[k], attachments, dir_path, key=k, auto_named=auto_named)

    elif manifest_path:
        with open(manifest_path, "r", encoding="utf-8") as f:
            content = f.read()
        parsed = _parse_frontmatter_text(content)
        req_id = parsed.get("id") or req_id
        title = parsed.get("title") or title
        folder = parsed.get("folder") or folder
        body = parsed.get("body") or body
        format_type = parsed.get("format") or format_type

    # Scan directory for companion files
    for f in dir_path.iterdir():
        if f.is_file() and f != manifest_path and not f.name.startswith("."):
            name_lower = f.name.lower()
            if name_lower.endswith((".b64", ".base64")):
                clean_name = f.stem
                try:
                    with open(f, "r", encoding="utf-8") as bf:
                        attachments[clean_name] = decode_base64_string(bf.read())
                except Exception as e:
                    # Previously a logged warning + silent drop; now a hard failure.
                    raise AttachmentLoadError(f"Companion base64 file {f.name!r} failed to decode: {e}") from e
            else:
                try:
                    with open(f, "rb") as bf:
                        raw_bytes = bf.read()
                    if raw_bytes.startswith(b"data:") or (len(raw_bytes) > 0 and raw_bytes[:10].isalnum() and not raw_bytes.startswith((b"\x89PNG", b"\xff\xd8", b"%PDF"))):
                        try:
                            attachments[f.name] = decode_base64_string(raw_bytes.decode("utf-8", errors="ignore"))
                        except Exception:
                            attachments[f.name] = raw_bytes
                    else:
                        attachments[f.name] = raw_bytes
                except OSError as e:
                    raise AttachmentLoadError(f"Companion file {f.name!r} could not be read: {e}") from e

    req = RichNoteRequest(
        request_id=req_id,
        title=title,
        folder=folder,
        body=body,
        format_type=format_type,
        attachments=attachments,
        raw_source_path=dir_path,
    )
    req.auto_named = auto_named
    return req


def _parse_json_file_request(json_path: Path) -> RichNoteRequest:
    with open(json_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    orig_name = json_path.name
    if orig_name.endswith(".processing"):
        orig_name = orig_name[:-len(".processing")]

    req_id = meta.get("id") or Path(orig_name).stem
    title = meta.get("title", "")
    folder = meta.get("folder", "Notes")
    body = meta.get("body", "")
    format_type = meta.get("format", "auto")
    attachments: Dict[str, bytes] = {}
    auto_named: Dict[str, Optional[str]] = {}

    for k in ["images", "attachments"]:
        if k in meta:
            _extract_attachments(meta[k], attachments, json_path.parent, key=k, auto_named=auto_named)

    req = RichNoteRequest(
        request_id=req_id,
        title=title,
        folder=folder,
        body=body,
        format_type=format_type,
        attachments=attachments,
        raw_source_path=json_path,
    )
    req.auto_named = auto_named
    return req


def _parse_text_file_request(txt_path: Path) -> RichNoteRequest:
    with open(txt_path, "r", encoding="utf-8") as f:
        content = f.read()

    orig_name = txt_path.name
    if orig_name.endswith(".processing"):
        orig_name = orig_name[:-len(".processing")]

    parsed = _parse_frontmatter_text(content)
    req_id = parsed.get("id") or Path(orig_name).stem
    title = parsed.get("title", "")
    folder = parsed.get("folder", "Notes")
    body = parsed.get("body", "")
    format_type = parsed.get("format", "markdown")
    attachments: Dict[str, bytes] = {}

    parent = txt_path.parent
    prefix = f"{Path(orig_name).stem}_"
    for cand in parent.glob(f"{prefix}*"):
        if cand.is_file() and not cand.name.startswith("."):
            clean_name = cand.name[len(prefix) :]
            if clean_name.lower().endswith((".b64", ".base64")):
                clean_name = Path(clean_name).stem
            try:
                with open(cand, "rb") as bf:
                    raw = bf.read()
                try:
                    attachments[clean_name] = decode_base64_string(raw.decode("utf-8"))
                except Exception:
                    attachments[clean_name] = raw
            except OSError as e:
                raise AttachmentLoadError(f"Companion file {cand.name!r} could not be read: {e}") from e

    return RichNoteRequest(
        request_id=req_id,
        title=title,
        folder=folder,
        body=body,
        format_type=format_type,
        attachments=attachments,
        raw_source_path=txt_path,
    )


# A string that is a path reference rather than base64. The base64 alphabet
# (standard or URL-safe) never contains ".", "~" or "\\", so any of
# those means "this was meant as a filename". Note "/" alone proves nothing:
# JPEG base64 begins "/9j/".
_PATHLIKE_RE = re.compile(r"[.~\\]")
# Inline base64 shorter than this cannot be a real attachment (a 1x1 PNG is ~90
# chars). Without this floor a dotless missing path like "clips/audio" is
# valid base64 and would decode into 8 bytes of garbage.
_MIN_INLINE_B64_CHARS = 24


def _load_attachment_value(name: str, val: Any, base_dir: Path, key: str) -> bytes:
    """
    Resolves one manifest attachment value to bytes: an existing file path
    (relative to the manifest; the file itself may contain base64 text or raw
    bytes) or an inline base64 / data-URI string. Raises AttachmentLoadError
    naming the entry on anything that can't be loaded — never returns empty or
    garbage bytes for a reference that didn't resolve.
    """
    label = f"{key}[{name!r}]"
    if not isinstance(val, str) or not val.strip():
        raise AttachmentLoadError(f"Attachment {label} has no data/path value")
    try:
        possible_file = (base_dir / os.path.expanduser(val.strip()))
        is_file = possible_file.is_file()
    except (OSError, ValueError):
        # e.g. a multi-KB base64 string exceeds the OS path-length limit.
        is_file = False
    if is_file:
        try:
            with open(possible_file, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise AttachmentLoadError(f"Attachment {label}: cannot read file {val!r}: {e}") from e
        if not raw:
            raise AttachmentLoadError(f"Attachment {label}: file {val!r} is empty")
        try:
            decoded = decode_base64_string(raw.decode("utf-8"))
            return decoded if decoded else raw
        except Exception:
            return raw
    stripped = val.strip()
    compact = re.sub(r"\s+", "", stripped)
    if not stripped.startswith("data:") and (
        (_PATHLIKE_RE.search(compact) and len(compact) < 4096)
        or len(compact) < _MIN_INLINE_B64_CHARS
    ):
        raise AttachmentLoadError(
            f"Attachment {label}: file not found: {val!r} (resolved relative to {base_dir})"
        )
    try:
        data = decode_base64_string(val)
    except Exception as e:
        raise AttachmentLoadError(
            f"Attachment {label}: value is neither an existing file path nor valid base64 ({e})"
        ) from e
    if not data:
        raise AttachmentLoadError(f"Attachment {label}: base64 decoded to zero bytes")
    return data


def _extract_attachments(
    obj: Any,
    out_dict: Dict[str, bytes],
    base_dir: Path,
    key: str = "attachments",
    auto_named: Optional[Dict[str, Optional[str]]] = None,
):
    """
    Loads every entry of a manifest's images/attachments value into out_dict.
    Any entry that cannot be loaded raises AttachmentLoadError (Fix 2: never
    silently drop).

    `auto_named` maps each key whose filename was synthesized here (rather
    than supplied by the sender) to a better staging filename hint (the
    basename of its "path", or None). The synthesized key itself keeps its
    historical "attachment_N.png" form so content hashes — and therefore dedup
    — of pre-existing manifests are unchanged; staging uses the hint or
    re-detects the real type instead of trusting the placeholder ".png".
    """
    if obj is None:
        return
    if isinstance(obj, dict):
        for name, val in obj.items():
            out_dict[name] = _load_attachment_value(name, val, base_dir, key)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            item_key = f"{key}[{idx}]"
            if isinstance(item, str):
                # Bare string entries were previously ignored outright (a silent
                # drop). Now: a path (named by its basename) or inline base64.
                s_item = re.sub(r"\s+", "", item)
                if _PATHLIKE_RE.search(s_item) and len(s_item) < 4096:
                    name = Path(item.strip()).name
                else:
                    name = f"attachment_{idx+1}.png"
                    if auto_named is not None:
                        auto_named[name] = None
                out_dict[name] = _load_attachment_value(name, item, base_dir, item_key)
                continue
            if not isinstance(item, dict):
                raise AttachmentLoadError(f"{item_key} is not an object or string: {type(item).__name__}")
            val = item.get("data") or item.get("base64") or item.get("path")
            name = item.get("name") or item.get("filename")
            if not name:
                name = f"attachment_{idx+1}.png"
                if auto_named is not None:
                    p = item.get("path")
                    auto_named[name] = Path(p.strip()).name if isinstance(p, str) and p.strip() else None
            out_dict[name] = _load_attachment_value(name, val, base_dir, item_key)
    else:
        raise AttachmentLoadError(f"{key} must be an object or a list, got {type(obj).__name__}")


def _parse_frontmatter_text(raw_text: str) -> Dict[str, str]:
    meta = {}
    body = raw_text

    if raw_text.startswith("---"):
        parts = raw_text.split("---", 2)
        if len(parts) >= 3:
            header_text = parts[1]
            body = parts[2].strip()
            for line in header_text.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
            meta["body"] = body
            return meta

    lines = raw_text.splitlines()
    header_lines = []
    body_idx = 0
    for idx, line in enumerate(lines):
        if not line.strip():
            body_idx = idx + 1
            break
        if ":" in line:
            header_lines.append(line)
        else:
            break

    if header_lines and body_idx > 0:
        for line in header_lines:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
        meta["body"] = "\n".join(lines[body_idx:]).strip()
        return meta

    meta["body"] = raw_text.strip()
    return meta


# ----------------------------------------------------------------------
# HTML Synthesis & Attachment Staging
# ----------------------------------------------------------------------
def staged_attachment_name(orig_name: str, file_bytes: bytes, hint: Optional[str] = None, auto: bool = False) -> str:
    """
    Filename an attachment is staged (and therefore shown in Notes) under.
    A sender-supplied name with a real extension always wins — magic-byte
    detection only fills in missing/placeholder extensions, so e.g. a .docx
    (ZIP container) named correctly is never relabeled .zip.
    """
    if auto:
        if hint and Path(hint).suffix and Path(hint).suffix.lower() not in (".b64", ".base64", ".txt"):
            return Path(hint).name
        base = Path(hint).stem if hint else Path(orig_name).stem
        return f"{base}{detect_file_extension(file_bytes)}"
    safe_name = Path(orig_name).name
    ext = Path(safe_name).suffix.lower()
    if ext in [".b64", ".base64", ".txt", ""]:
        safe_name = f"{Path(safe_name).stem}{detect_file_extension(file_bytes)}"
    return safe_name


def stage_attachments(attachments: Dict[str, bytes], scratch_dir: Path, auto_named: Optional[Dict[str, Optional[str]]] = None) -> List[Path]:
    """
    Writes each attachment into scratch_dir. Two entries that would stage to
    the same filename (e.g. "a/x.png" and "b/x.png") are disambiguated with a
    " (2)" suffix — previously the second silently overwrote the first, so
    one attachment vanished while the request still reported success.
    """
    auto_named = auto_named or {}
    staged_paths: List[Path] = []
    used: set = set()
    for orig_name, file_bytes in attachments.items():
        safe_name = staged_attachment_name(
            orig_name, file_bytes, hint=auto_named.get(orig_name), auto=orig_name in auto_named
        )
        if not safe_name or safe_name in (".", ".."):
            safe_name = f"attachment{detect_file_extension(file_bytes)}"
        candidate = safe_name
        n = 2
        while candidate.lower() in used or candidate == "note_body.html":
            candidate = f"{Path(safe_name).stem} ({n}){Path(safe_name).suffix}"
            n += 1
        used.add(candidate.lower())
        out_path = scratch_dir / candidate
        with open(out_path, "wb") as f:
            f.write(file_bytes)
        staged_paths.append(out_path)
    if len(staged_paths) != len(attachments):
        raise AttachmentLoadError(
            f"Staged {len(staged_paths)} of {len(attachments)} attachments — refusing a partial note"
        )
    return staged_paths


def prepare_note_html_and_attachments(
    req: RichNoteRequest, scratch_dir: Path
) -> Tuple[str, List[Path]]:
    """
    Stages all attachments into scratch_dir (/tmp) and prepares the rich
    HTML body for Apple Notes. Returns (html_body, list_of_staged_file_paths).
    """
    # 1. Save all decoded attachments into scratch_dir (/tmp)
    staged_paths = stage_attachments(req.attachments, scratch_dir, getattr(req, "auto_named", None))

    # 2. Convert body to HTML
    body_str = req.body or ""
    is_html = (
        req.format_type == "html"
        or "<div>" in body_str
        or "<p>" in body_str
        or "<h1>" in body_str
        or "<html>" in body_str
    )

    if is_html:
        html_content = body_str
    else:
        html_content = markdown_to_html(body_str)

    # Remove any stray <img src="..."> tags from HTML body so Notes doesn't fail URL loading.
    #
    # LIMITATION (investigated; inline image placement is not possible here):
    # Notes' AppleScript dictionary exposes exactly one way to add a real
    # attachment — `make new attachment at <note> with data <file>` — and it
    # always appends at the end of the note; there is no insertion-location
    # parameter, and `attachment` objects have no settable position. Setting
    # `body` to HTML containing <img src="file://..."> or a data: URI does not
    # create an attachment (Notes drops or fails the external load), and any
    # later write to `body` on a note that already has attachments rewrites
    # the HTML and can detach/duplicate them. "Segmented" construction (write
    # body part 1, attach, append body part 2) therefore cannot work: the
    # second body write replaces the whole body, it does not append after the
    # attachment. So images stay as end-of-note attachments and each <img>
    # becomes a "📷 <alt>" placeholder line marking where it was referenced.
    html_content = re.sub(r'<img[^>]*alt=["\'](.*?)["\'][^>]*>', r'<div><i>📷 \1</i></div>', html_content)
    html_content = re.sub(r'<img[^>]*>', r'', html_content)

    # 3. Ensure title is rendered as the first element if not already present
    title_escaped = html.escape(req.title)
    if not (f"<h1>{title_escaped}</h1>" in html_content or title_escaped in html_content[:150]):
        title_block = f'<div><b><span style="font-size: 24px;">{title_escaped}</span></b></div><div><br></div>\n'
        html_content = title_block + html_content

    return html_content, staged_paths


# ----------------------------------------------------------------------
# Apple Notes Automation via AppleScript
# ----------------------------------------------------------------------
def _escape_applescript_string(s: str) -> str:
    """Escapes a string for safe interpolation into a double-quoted AppleScript literal."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def execute_applescript_create_note(
    folder: str,
    title: str,
    html_body: str,
    attachment_paths: List[Path],
    scratch_dir: Path,
) -> Dict[str, Any]:
    """
    Creates note in Apple Notes and attaches files via AppleScript data handler:
    'make new attachment at theNote with data (POSIX file ...)'
    This provides true native attachment and image embedding that renders
    in Notes.app, Quick Look, and iCloud sync.
    """
    # Ensure Notes app is awake
    try:
        subprocess.run(["open", "-g", "-a", "Notes"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    # Write HTML body to file in scratch_dir so AppleScript can read it via (read POSIX file ... as «class utf8»)
    body_file = scratch_dir / "note_body.html"
    with open(body_file, "w", encoding="utf-8") as f:
        f.write(html_body)

    safe_folder = _escape_applescript_string((folder or "Notes").strip())

    script_lines = [
        'tell application "Notes"',
        f'    set folderName to "{safe_folder}"',
        '    if folderName is "" or folderName is "Notes" then',
        '        try',
        '            set theFolder to folder "Notes"',
        '        on error',
        '            set theFolder to default folder of default account',
        '        end try',
        '    else',
        '        try',
        '            set theFolder to folder folderName',
        '        on error',
        '            set theFolder to make new folder at default account with properties {name:folderName}',
        '        end try',
        '    end if',
        f'    set bodyText to (read POSIX file "{body_file.resolve()}" as «class utf8»)',
        '    set theNote to make new note at theFolder with properties {body:bodyText}',
    ]

    # Each attachment is added inside a try: if Notes refuses any one of them,
    # the half-built note is deleted and the script reports which file failed,
    # so a request never ends as a note silently missing some attachments.
    for att_path in attachment_paths:
        safe_att = _escape_applescript_string(str(att_path.resolve()))
        script_lines.extend([
            '    try',
            f'        make new attachment at theNote with data (POSIX file "{safe_att}")',
            '    on error errMsg',
            '        try',
            '            delete theNote',
            '        end try',
            f'        return "ATTACH_ERROR" & tab & "{_escape_applescript_string(att_path.name)}" & tab & errMsg',
            '    end try',
        ])

    script_lines.extend([
        '    set noteId to id of theNote',
        '    set noteName to name of theNote',
        '    set attCount to 0',
        '    try',
        '        set attCount to count of attachments of theNote',
        '    end try',
        '    return noteId & "\\t" & noteName & "\\t" & (attCount as string)',
        'end tell',
    ])

    script_content = "\n".join(script_lines)

    res = subprocess.run(["osascript", "-"], input=script_content, capture_output=True, text=True)

    if res.returncode != 0:
        error_msg = res.stderr.strip() or f"AppleScript failed with return code {res.returncode}"
        raise RuntimeError(f"Notes.app automation error: {error_msg}")

    output_str = res.stdout.strip()
    if not output_str:
        raise RuntimeError("Notes.app automation returned empty response")

    parts = output_str.split("\t")
    if parts[0] == "ATTACH_ERROR":
        failed = parts[1] if len(parts) > 1 else "?"
        detail = parts[2] if len(parts) > 2 else ""
        raise RuntimeError(f"Notes.app refused attachment {failed!r}: {detail} (note was rolled back)")
    note_id = parts[0]
    note_name = parts[1] if len(parts) > 1 else title
    notes_count: Optional[int] = None
    if len(parts) > 2:
        try:
            notes_count = int(parts[2])
        except ValueError:
            notes_count = None

    if notes_count is not None and notes_count < len(attachment_paths):
        # Notes accepted every `make new attachment` yet reports fewer: treat
        # as failure rather than report a success the sender can't trust.
        subprocess.run(
            ["osascript", "-e", f'tell application "Notes" to delete note id "{_escape_applescript_string(note_id)}"'],
            capture_output=True, text=True,
        )
        raise RuntimeError(
            f"Notes.app reports {notes_count} attachment(s) after adding {len(attachment_paths)} "
            "(note was rolled back)"
        )

    return {
        "id": note_id,
        "name": note_name,
        "attachments_count": len(attachment_paths),
        "notes_attachments_count": notes_count,
        "folder": folder,
    }


# ----------------------------------------------------------------------
# Apple Notes Automation via AppleScript — Export (read-only)
# ----------------------------------------------------------------------
# Delimiter for AppleScript-side list joins (attachment names, candidate note IDs).
# A control character virtually never present in a real filename or note title —
# safer than comma/tab, which legitimately appear in both.
_AS_LIST_SEP = "\x1f"


def resolve_note_id_by_title(title: str) -> Dict[str, Any]:
    """
    Finds note(s) by exact title match. Returns one of:
      {"status": "OK", "note_id": "..."}                       — exactly one match
      {"status": "NOTFOUND"}                                    — zero matches
      {"status": "MULTI", "candidates": ["id1", "id2", ...]}    — ambiguous
      {"status": "ERROR", "error": "..."}                       — AppleScript failure

    NOTE: Notes.app's `every note whose name is X` has been observed (verified
    empirically against a real Notes.app, 2026-11-15) to enumerate each matching
    note TWICE with byte-identical IDs — there is only one iCloud account on this
    machine, so this is not a multi-account artifact, just a quirk of the flat
    `notes` collection. Candidates are deduplicated by exact ID string before
    deciding 0/1/multi, or every single-match title lookup would falsely report
    "multiple notes found".
    """
    safe_title = _escape_applescript_string(title)
    script = f'''
    tell application "Notes"
        try
            set matchingNotes to every note whose name is "{safe_title}"
        on error errMsg
            return "ERROR" & tab & errMsg
        end try
        set idList to {{}}
        repeat with aNote in matchingNotes
            set thisId to (id of aNote)
            if thisId is not in idList then
                set end of idList to thisId
            end if
        end repeat
        set n to count of idList
        if n is 0 then
            return "NOTFOUND"
        else if n is 1 then
            return "OK" & tab & (item 1 of idList)
        else
            set AppleScript's text item delimiters to "{_AS_LIST_SEP}"
            set idJoined to idList as string
            set AppleScript's text item delimiters to ""
            return "MULTI" & tab & idJoined
        end if
    end tell
    '''
    res = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True)
    if res.returncode != 0:
        err = res.stderr.strip() or f"osascript failed with return code {res.returncode}"
        return {"status": "ERROR", "error": err}

    out = res.stdout.strip()
    if not out:
        return {"status": "ERROR", "error": "Notes.app automation returned empty response"}

    parts = out.split("\t", 1)
    status = parts[0]
    if status == "OK":
        return {"status": "OK", "note_id": parts[1] if len(parts) > 1 else ""}
    if status == "NOTFOUND":
        return {"status": "NOTFOUND"}
    if status == "MULTI":
        candidates = parts[1].split(_AS_LIST_SEP) if len(parts) > 1 else []
        return {"status": "MULTI", "candidates": candidates}
    if status == "ERROR":
        return {"status": "ERROR", "error": parts[1] if len(parts) > 1 else "unknown error"}
    return {"status": "ERROR", "error": f"Unrecognized automation response: {out!r}"}


# ----------------------------------------------------------------------
# Apple Notes Automation — Attach to an existing note
# ----------------------------------------------------------------------
NOTES_GROUP_CONTAINER = Path(os.path.expanduser("~/Library/Group Containers/group.com.apple.notes"))
NOTES_SQLITE_PATH = NOTES_GROUP_CONTAINER / "NoteStore.sqlite"
_UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def _coredata_id_from_note_uuid(note_uuid: str, db_path: Path = NOTES_SQLITE_PATH) -> Optional[str]:
    """
    Maps a note's UUID (ZICCLOUDSYNCINGOBJECT.ZIDENTIFIER — the form seen in
    applenotes:/notes:// share links) to the x-coredata://<store-uuid>/ICNote/p<pk>
    id AppleScript understands. Read-only SQLite access (stdlib sqlite3,
    mode=ro). Returns None when the store is unreadable (no Full Disk Access
    for the launchd python, schema change) or the UUID isn't an ICNote; the
    caller then falls back to title/folder resolution.
    """
    import sqlite3

    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except Exception as e:
        logger.debug(f"Notes store not readable ({e}); UUID mapping unavailable")
        return None
    try:
        store_uuid_row = con.execute("SELECT Z_UUID FROM Z_METADATA LIMIT 1").fetchone()
        ent_row = con.execute("SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'ICNote'").fetchone()
        if not store_uuid_row or not ent_row:
            return None
        pk_row = con.execute(
            "SELECT Z_PK FROM ZICCLOUDSYNCINGOBJECT WHERE ZIDENTIFIER = ? AND Z_ENT = ?",
            (note_uuid.upper(), ent_row[0]),
        ).fetchone()
        if not pk_row:
            pk_row = con.execute(
                "SELECT Z_PK FROM ZICCLOUDSYNCINGOBJECT WHERE ZIDENTIFIER = ? AND Z_ENT = ?",
                (note_uuid, ent_row[0]),
            ).fetchone()
        if not pk_row:
            return None
        return f"x-coredata://{store_uuid_row[0]}/ICNote/p{pk_row[0]}"
    except Exception as e:
        logger.debug(f"Notes store UUID lookup failed: {e}")
        return None
    finally:
        con.close()


def _note_id_exists(note_id: str) -> bool:
    safe = _escape_applescript_string(note_id)
    script = f'tell application "Notes"\n try\n  set n to note id "{safe}"\n  return id of n\n on error\n  return "NOTFOUND"\n end try\nend tell'
    res = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True)
    return res.returncode == 0 and res.stdout.strip() not in ("", "NOTFOUND")


def resolve_note_in_folder_by_title(title: str, folder: str) -> Dict[str, Any]:
    """Exact-title match restricted to folders named `folder` (in any account).
    Same return contract as resolve_note_id_by_title (OK/NOTFOUND/MULTI/ERROR),
    with the same id de-duplication."""
    safe_title = _escape_applescript_string(title)
    safe_folder = _escape_applescript_string(folder)
    script = f"""
    tell application "Notes"
        set idList to {{}}
        try
            set theFolders to every folder whose name is "{safe_folder}"
        on error errMsg
            return "ERROR" & tab & errMsg
        end try
        if (count of theFolders) is 0 then return "NOFOLDER"
        repeat with f in theFolders
            try
                set matchingNotes to (every note of f whose name is "{safe_title}")
                repeat with aNote in matchingNotes
                    set thisId to (id of aNote)
                    if thisId is not in idList then set end of idList to thisId
                end repeat
            end try
        end repeat
        set n to count of idList
        if n is 0 then
            return "NOTFOUND"
        else if n is 1 then
            return "OK" & tab & (item 1 of idList)
        else
            set AppleScript's text item delimiters to "{_AS_LIST_SEP}"
            set idJoined to idList as string
            set AppleScript's text item delimiters to ""
            return "MULTI" & tab & idJoined
        end if
    end tell
    """
    res = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True)
    if res.returncode != 0:
        return {"status": "ERROR", "error": res.stderr.strip() or f"osascript rc={res.returncode}"}
    out = res.stdout.strip()
    parts = out.split("\t", 1)
    if parts[0] == "OK":
        return {"status": "OK", "note_id": parts[1] if len(parts) > 1 else ""}
    if parts[0] == "NOTFOUND":
        return {"status": "NOTFOUND"}
    if parts[0] == "NOFOLDER":
        return {"status": "NOTFOUND", "error": f"no folder named {folder!r}"}
    if parts[0] == "MULTI":
        return {"status": "MULTI", "candidates": parts[1].split(_AS_LIST_SEP) if len(parts) > 1 else []}
    if parts[0] == "ERROR":
        return {"status": "ERROR", "error": parts[1] if len(parts) > 1 else "unknown error"}
    return {"status": "ERROR", "error": f"Unrecognized automation response: {out!r}"}


def resolve_attach_target(note_id: Optional[str], title: Optional[str], folder: Optional[str]) -> Dict[str, Any]:
    """
    Resolution order:
      1. note_id "x-coredata://..." → used directly if Notes can open it.
      2. note_id UUID → mapped through NoteStore.sqlite, then verified.
      3. title (+ folder when given) → must match exactly one note.
    Returns {"status": "OK", "note_id", "resolved_by"} or an error dict with
    status NOTFOUND / MULTI / ERROR (+ "error", "candidates").
    """
    tried: List[str] = []
    if note_id:
        if note_id.startswith("x-coredata://"):
            if _note_id_exists(note_id):
                return {"status": "OK", "note_id": note_id, "resolved_by": "note_id"}
            tried.append(f"note id {note_id!r} not found in Notes.app")
        else:
            bare = note_id
            for prefix in ("applenotes:note/", "notes://showNote?identifier=", "notes://"):
                if bare.startswith(prefix):
                    bare = bare[len(prefix):]
            bare = bare.split("?", 1)[0].strip("/")
            if _UUID_RE.match(bare):
                mapped = _coredata_id_from_note_uuid(bare)
                if mapped and _note_id_exists(mapped):
                    return {"status": "OK", "note_id": mapped, "resolved_by": "note_uuid"}
                tried.append(
                    f"note UUID {bare!r} could not be mapped via the Notes store"
                    + (" (store unreadable — grant Full Disk Access to the bridge's python)" if not mapped else "")
                )
            elif _note_id_exists(note_id):
                return {"status": "OK", "note_id": note_id, "resolved_by": "note_id"}
            else:
                tried.append(f"note id {note_id!r} not recognized")
    if title:
        lookup = resolve_note_in_folder_by_title(title, folder) if folder else resolve_note_id_by_title(title)
        where = f" in folder {folder!r}" if folder else ""
        if lookup["status"] == "OK":
            return {"status": "OK", "note_id": lookup["note_id"], "resolved_by": "title_folder" if folder else "title"}
        if lookup["status"] == "MULTI":
            cands = lookup.get("candidates", [])
            return {
                "status": "MULTI",
                "candidates": cands,
                "error": f"{len(cands)} notes match title {title!r}{where} — provide note_id to disambiguate",
            }
        if lookup["status"] == "NOTFOUND":
            tried.append(f"no note titled {title!r}{where}" + (f" ({lookup['error']})" if lookup.get("error") else ""))
        else:
            tried.append(f"title lookup failed: {lookup.get('error')}")
    if not note_id and not title:
        return {"status": "ERROR", "error": "attach request has neither note_id nor title — cannot identify the note"}
    return {"status": "NOTFOUND", "error": "; ".join(tried) or "note not found"}


def execute_applescript_attach_to_note(note_id: str, attachment_paths: List[Path]) -> Dict[str, Any]:
    """
    Appends files to an existing note with the same native handler the create
    path uses ('make new attachment at theNote with data (POSIX file ...)').
    Attachments land at the end of the note. Returns before/after counts and
    the note's full attachment-name list. If Notes refuses a file midway,
    raises with the names already added (those cannot be rolled back safely,
    so the error says exactly what is now on the note).
    """
    safe_id = _escape_applescript_string(note_id)
    lines = [
        'tell application "Notes"',
        '    try',
        f'        set theNote to note id "{safe_id}"',
        '    on error errMsg',
        '        return "ERROR" & tab & "note not found: " & errMsg',
        '    end try',
        '    set beforeCount to count of attachments of theNote',
        '    set addedCount to 0',
    ]
    for p in attachment_paths:
        safe_p = _escape_applescript_string(str(p.resolve()))
        lines.extend([
            '    try',
            f'        make new attachment at theNote with data (POSIX file "{safe_p}")',
            '        set addedCount to addedCount + 1',
            '    on error errMsg',
            f'        return "PARTIAL" & tab & (addedCount as string) & tab & "{_escape_applescript_string(p.name)}" & tab & errMsg',
            '    end try',
        ])
    lines.extend([
        '    set afterCount to count of attachments of theNote',
        '    set noteTitle to name of theNote',
        '    set folderName to ""',
        '    try',
        '        set folderName to name of (container of theNote)',
        '    end try',
        '    set attNames to {}',
        '    try',
        '        set attNames to name of every attachment of theNote',
        '    end try',
        f'    set AppleScript\'s text item delimiters to "{_AS_LIST_SEP}"',
        '    set attJoined to attNames as string',
        '    set AppleScript\'s text item delimiters to ""',
        '    return "OK" & tab & (beforeCount as string) & tab & (afterCount as string) & tab & noteTitle & tab & folderName & tab & attJoined',
        'end tell',
    ])
    subprocess.run(["open", "-g", "-a", "Notes"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    res = subprocess.run(["osascript", "-"], input="\n".join(lines), capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Notes.app attach automation error: {res.stderr.strip() or res.returncode}")
    # rstrip newline only: the attachment-name list is the last field and may
    # be empty, so a full strip() would eat the trailing tab separator.
    out = res.stdout.rstrip("\r\n")
    parts = out.split("\t")
    if parts[0] == "ERROR":
        raise RuntimeError(parts[1] if len(parts) > 1 else "unknown Notes.app error")
    if parts[0] == "PARTIAL":
        added = parts[1] if len(parts) > 1 else "0"
        failed = parts[2] if len(parts) > 2 else "?"
        detail = parts[3] if len(parts) > 3 else ""
        raise RuntimeError(
            f"Notes.app refused attachment {failed!r} after {added} of {len(attachment_paths)} were added: {detail}"
        )
    if parts[0] != "OK" or len(parts) < 6:
        raise RuntimeError(f"Unrecognized attach automation response: {out!r}")
    before, after = int(parts[1]), int(parts[2])
    names = [a for a in parts[5].split(_AS_LIST_SEP) if a] if parts[5] else []
    return {"before": before, "after": after, "title": parts[3], "folder": parts[4], "attachment_names": names}


def execute_applescript_export_note(
    note_id: str, plaintext_tmp_path: Path, html_tmp_path: Path
) -> Dict[str, Any]:
    """
    Strictly read-only: looks up the note by its CoreData id and writes its full
    plaintext and HTML body to the given temp file paths (written directly by
    AppleScript via its own `write ... as «class utf8»`, so arbitrarily large
    bodies never pass through the Python subprocess stdout pipe). Never
    modifies, moves, or deletes the note. Returns metadata; raises RuntimeError
    on any failure (missing note, locked/shared note, or a file-write error) —
    the caller is expected to turn that into a clean error receipt.
    """
    safe_note_id = _escape_applescript_string(note_id)
    safe_plaintext_path = _escape_applescript_string(str(plaintext_tmp_path.resolve()))
    safe_html_path = _escape_applescript_string(str(html_tmp_path.resolve()))

    script = f'''
    tell application "Notes"
        try
            set theNote to note id "{safe_note_id}"
        on error errMsg
            return "ERROR" & tab & "note not found: " & errMsg
        end try

        set noteTitle to "Untitled Note"
        try
            set noteTitle to name of theNote
        end try

        set folderName to ""
        try
            set theContainer to container of theNote
            set folderName to name of theContainer
        end try

        set attNames to {{}}
        try
            set attNames to name of every attachment of theNote
        end try

        set plaintextOk to 1
        set thePlaintext to ""
        try
            set thePlaintext to plaintext of theNote
        on error
            set plaintextOk to 0
        end try

        set htmlOk to 1
        set theHtml to ""
        try
            set theHtml to body of theNote
        on error
            set htmlOk to 0
        end try

        try
            set fRef to open for access (POSIX file "{safe_plaintext_path}") with write permission
            set eof fRef to 0
            write thePlaintext to fRef as «class utf8»
            close access fRef
        on error errMsg2
            try
                close access (POSIX file "{safe_plaintext_path}")
            end try
            return "ERROR" & tab & "failed writing plaintext output: " & errMsg2
        end try

        try
            set fRef2 to open for access (POSIX file "{safe_html_path}") with write permission
            set eof fRef2 to 0
            write theHtml to fRef2 as «class utf8»
            close access fRef2
        on error errMsg3
            try
                close access (POSIX file "{safe_html_path}")
            end try
            return "ERROR" & tab & "failed writing html output: " & errMsg3
        end try

        set AppleScript's text item delimiters to "{_AS_LIST_SEP}"
        set attJoined to attNames as string
        set AppleScript's text item delimiters to ""

        return "OK" & tab & noteTitle & tab & folderName & tab & attJoined & tab & (plaintextOk as string) & tab & (htmlOk as string)
    end tell
    '''
    res = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True)

    if res.returncode != 0:
        error_msg = res.stderr.strip() or f"AppleScript failed with return code {res.returncode}"
        raise RuntimeError(f"Notes.app export automation error: {error_msg}")

    output_str = res.stdout.strip()
    if not output_str:
        raise RuntimeError("Notes.app export automation returned empty response")

    parts = output_str.split("\t")
    if parts[0] == "ERROR":
        raise RuntimeError(parts[1] if len(parts) > 1 else "Unknown Notes.app export error")
    if parts[0] != "OK" or len(parts) < 6:
        raise RuntimeError(f"Unrecognized export automation response: {output_str!r}")

    title, folder_name, att_joined, plaintext_ok, html_ok = parts[1], parts[2], parts[3], parts[4], parts[5]
    attachment_names = [a for a in att_joined.split(_AS_LIST_SEP) if a] if att_joined else []

    if plaintext_ok != "1":
        logger.warning(f"Note {note_id}: plaintext property unavailable, .body.txt will be empty")
    if html_ok != "1":
        logger.warning(f"Note {note_id}: body (HTML) property unavailable, .body.html will be empty")

    return {
        "title": title,
        "folder": folder_name,
        "attachments": attachment_names,
    }


# ----------------------------------------------------------------------
# Request Processing Pipeline — Export (read-only, no dedup)
# ----------------------------------------------------------------------
# NOTE (stretch goal, intentionally NOT implemented): exporting attachment
# binaries. The files live under ~/Library/Group Containers/
# group.com.apple.notes/Media/<media-UUID>/.../<filename>, and the mapping is
# note Z_PK → attachment rows (ZICCLOUDSYNCINGOBJECT.ZNOTE) → ZMEDIA → media
# row ZIDENTIFIER → directory name. That chain is undocumented and varies:
# (a) images/scans/drawings often have no Media file at all (FallbackImages/
# Previews/ or Paper bundles instead); (b) the directory layout differs across
# macOS releases (some versions nest an extra generation/UUID directory);
# (c) iCloud-only attachments may not be downloaded locally; (d) reading the
# store and the group container requires Full Disk Access for the launchd
# python. It could not be verified against a live store when this was
# written, and a mapping that silently exports the wrong or a partial file
# set is worse than none, so export continues to report attachment NAMES only.
def process_export_request(item_path: Path, config: BridgeConfig) -> Dict[str, Any]:
    """
    Processes a single "op": "export" request. Fully separate from the create
    pipeline: never touches DedupManager (an export always re-reads fresh —
    the note may have been edited since a prior export of the same id), never
    writes to Notes.app, and cannot trigger any of the create-path's side
    effects. Any failure (missing note, ambiguous title, locked/shared note,
    bad request) becomes a clean "status": "error" receipt — this function is
    never allowed to raise, crash the caller, or hang the watcher loop.
    """
    orig_name = item_path.name
    if orig_name.endswith(".processing"):
        orig_name = orig_name[:-len(".processing")]
    req_id = Path(orig_name).stem
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    req: Optional[ExportRequest] = None

    def _error(message: str, candidates: Optional[List[str]] = None) -> Dict[str, Any]:
        logger.error(f"Export request {req_id} failed: {message}")
        result = {
            "id": req_id,
            "status": "error",
            "op": "export",
            "note_id": req.note_id if req else None,
            "title": req.title if req else None,
            "folder": None,
            "attachments_count": 0,
            "attachments": [],
            "body_chars": None,
            "body_bytes": None,
            "body_sha256": None,
            "body_file": None,
            "html_file": None,
            "processed_at": now_iso,
            "error": message,
        }
        if candidates is not None:
            result["candidates"] = candidates
        _write_result(config, req_id, result)
        _archive_request(item_path, config.archive_dir / "failed", req_id)
        return result

    try:
        req = _parse_export_request(item_path)
        req_id = req.request_id
    except Exception as e:
        logger.error(f"Error parsing export request {item_path.name}: {e}", exc_info=True)
        return _error(f"Failed to parse export request: {e}")

    note_id = req.note_id
    if not note_id:
        if not req.title:
            return _error("Export request has neither note_id nor title — cannot identify the note")
        logger.info(f"Export {req_id}: resolving note by title '{req.title}'...")
        lookup = resolve_note_id_by_title(req.title)
        if lookup["status"] == "NOTFOUND":
            return _error(f"No note found with title {req.title!r}")
        if lookup["status"] == "MULTI":
            candidates = lookup.get("candidates", [])
            return _error(
                f"{len(candidates)} notes match title {req.title!r} — provide note_id to disambiguate, never guessing",
                candidates=candidates,
            )
        if lookup["status"] == "ERROR":
            return _error(f"Title lookup failed: {lookup.get('error', 'unknown error')}")
        note_id = lookup["note_id"]

    config.outbox_dir.mkdir(parents=True, exist_ok=True)
    body_filename = f"{req_id}.body.txt"
    html_filename = f"{req_id}.body.html"
    plaintext_tmp = config.outbox_dir / f"{body_filename}.tmp"
    html_tmp = config.outbox_dir / f"{html_filename}.tmp"
    plaintext_final = config.outbox_dir / body_filename
    html_final = config.outbox_dir / html_filename

    try:
        logger.info(f"Export {req_id}: reading note {note_id} (read-only)...")
        meta = execute_applescript_export_note(note_id, plaintext_tmp, html_tmp)
    except Exception as e:
        logger.error(f"Export {req_id} automation error for note {note_id}: {e}")
        for p in (plaintext_tmp, html_tmp):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        return _error(str(e))

    try:
        body_bytes_data = plaintext_tmp.read_bytes()
        body_chars = len(body_bytes_data.decode("utf-8"))
        body_bytes = len(body_bytes_data)
        body_sha256 = hashlib.sha256(body_bytes_data).hexdigest()

        os.replace(plaintext_tmp, plaintext_final)
        os.replace(html_tmp, html_final)
    except Exception as e:
        logger.error(f"Export {req_id}: failed finalizing output files: {e}", exc_info=True)
        for p in (plaintext_tmp, html_tmp):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        return _error(f"Failed writing export outputs: {e}")

    logger.info(
        f"Export {req_id}: wrote {body_filename} ({body_chars:,} chars, {body_bytes:,} bytes) "
        f"and {html_filename} for note '{meta['title']}'"
    )

    result = {
        "id": req_id,
        "status": "success",
        "op": "export",
        "note_id": note_id,
        "title": meta["title"],
        "folder": meta["folder"],
        "attachments_count": len(meta["attachments"]),
        "attachments": meta["attachments"],
        "body_chars": body_chars,
        "body_bytes": body_bytes,
        "body_sha256": body_sha256,
        "body_file": body_filename,
        "html_file": html_filename,
        "processed_at": now_iso,
        "error": None,
    }
    _write_result(config, req_id, result)
    _archive_request(item_path, config.archive_dir, req_id)
    return result


# ----------------------------------------------------------------------
# Request Processing Pipeline — Attach (append to an existing note)
# ----------------------------------------------------------------------
def process_attach_request(item_path: Path, config: BridgeConfig, dedup: DedupManager) -> Dict[str, Any]:
    """
    Processes "op": "attach". Validates every attachment BEFORE touching
    Notes.app (an unloadable entry → error, note untouched). Deduped by request
    id / content hash under an "attach:" namespace so a re-dropped manifest
    does not append the same files twice; "force": true bypasses that.
    Result: attachments_count is the note's NEW TOTAL attachment count;
    attachments lists all attachment names now on the note; attachments_added
    lists the staged names this request appended.
    """
    orig_name = item_path.name
    if orig_name.endswith(".processing"):
        orig_name = orig_name[:-len(".processing")]
    req_id = Path(orig_name).stem if not item_path.is_dir() else orig_name
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    req: Optional[AttachRequest] = None
    scratch_dir: Optional[Path] = None

    def _result(status: str, **kw) -> Dict[str, Any]:
        r = {
            "id": req_id,
            "status": status,
            "op": "attach",
            "note_id": kw.get("note_id", req.note_id if req else None),
            "title": kw.get("title", req.title if req else None),
            "folder": kw.get("folder", req.folder if req else None),
            "attachments_count": kw.get("attachments_count", 0),
            "attachments": kw.get("attachments", []),
            "attachments_added": kw.get("attachments_added", []),
            "attachments_added_count": len(kw.get("attachments_added", [])),
            "attachments_count_before": kw.get("attachments_count_before"),
            "resolved_by": kw.get("resolved_by"),
            "processed_at": now_iso,
            "error": kw.get("error"),
        }
        for extra in ("candidates", "message"):
            if extra in kw:
                r[extra] = kw[extra]
        return r

    def _fail(message: str, **kw) -> Dict[str, Any]:
        logger.error(f"Attach request {req_id} failed: {message}")
        r = _result("error", error=message, **kw)
        _write_result(config, req_id, r)
        _archive_request(item_path, config.archive_dir / "failed", req_id)
        return r

    try:
        try:
            req = _parse_attach_request(item_path)
            req_id = req.request_id
        except Exception as e:
            return _fail(f"Failed to parse attach request: {e}")

        if not req.attachments:
            return _fail("attach request has no images/attachments entries")

        dedup_id = f"attach:{req.request_id}"
        content_hash = f"attach:{req.compute_content_hash()}"
        if not req.force:
            existing = dedup.check_duplicate(dedup_id, content_hash)
            if existing:
                r = _result(
                    "duplicate",
                    note_id=existing.get("note_id"),
                    title=existing.get("title"),
                    folder=existing.get("folder"),
                    attachments_count=existing.get("attachments_count", 0),
                    attachments=existing.get("attachments", []),
                    attachments_added=existing.get("attachments_added", []),
                    attachments_count_before=existing.get("attachments_count_before"),
                    resolved_by=existing.get("resolved_by"),
                    message="These attachments were already added by this request id or identical content (use \"force\": true to add again)",
                )
                _write_result(config, req_id, r)
                _archive_request(item_path, config.archive_dir, req_id)
                return r

        target = resolve_attach_target(req.note_id, req.title, req.folder)
        if target["status"] != "OK":
            kw = {}
            if target.get("candidates") is not None:
                kw["candidates"] = target["candidates"]
            return _fail(target.get("error", "note not found"), **kw)
        note_id = target["note_id"]

        scratch_dir = Path(f"/tmp/rich_notes_bridge_attach_{req_id}_{int(time.time() * 1000)}")
        scratch_dir.mkdir(parents=True, exist_ok=True)
        staged = stage_attachments(req.attachments, scratch_dir, req.auto_named)
        added_names = [p.name for p in staged]

        logger.info(f"Attach {req_id}: adding {len(staged)} attachment(s) to note {note_id} ({target['resolved_by']})")
        try:
            meta = execute_applescript_attach_to_note(note_id, staged)
        except Exception as e:
            return _fail(str(e), note_id=note_id, resolved_by=target["resolved_by"])

        if meta["after"] < meta["before"] + len(staged):
            return _fail(
                f"Notes.app reports {meta['after']} attachment(s) after adding {len(staged)} to {meta['before']}",
                note_id=note_id, title=meta["title"], folder=meta["folder"],
                attachments_count=meta["after"], attachments=meta["attachment_names"],
                attachments_count_before=meta["before"], resolved_by=target["resolved_by"],
            )

        record = {
            "note_id": note_id,
            "title": meta["title"],
            "folder": meta["folder"],
            "attachments_count": meta["after"],
            "attachments": meta["attachment_names"],
            "attachments_added": added_names,
            "attachments_count_before": meta["before"],
            "resolved_by": target["resolved_by"],
            "content_hash": content_hash,
            "created_at": now_iso,
        }
        dedup.record(dedup_id, content_hash, record)

        r = _result(
            "success",
            note_id=note_id, title=meta["title"], folder=meta["folder"],
            attachments_count=meta["after"], attachments=meta["attachment_names"],
            attachments_added=added_names, attachments_count_before=meta["before"],
            resolved_by=target["resolved_by"],
        )
        logger.info(f"Attach {req_id}: note '{meta['title']}' now has {meta['after']} attachment(s) (was {meta['before']})")
        _write_result(config, req_id, r)
        _archive_request(item_path, config.archive_dir, req_id)
        return r
    except Exception as e:
        logger.error(f"Attach request {req_id} crashed: {e}", exc_info=True)
        return _fail(str(e))
    finally:
        if scratch_dir and scratch_dir.exists():
            shutil.rmtree(scratch_dir, ignore_errors=True)


# ----------------------------------------------------------------------
# Request Processing Pipeline
# ----------------------------------------------------------------------
def process_request(item_path: Path, config: BridgeConfig, dedup: DedupManager) -> Dict[str, Any]:
    """Processes a single request item from the inbox."""
    req = None
    scratch_dir = None
    orig_name = item_path.name
    if orig_name.endswith(".processing"):
        orig_name = orig_name[:-len(".processing")]
    req_id = Path(orig_name).stem
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    op = _detect_op(item_path, orig_name)
    if op == "export":
        return process_export_request(item_path, config)
    if op == "attach":
        return process_attach_request(item_path, config, dedup)
    if op != "create":
        logger.error(f"Request {req_id} has unrecognized op {op!r} — refusing to guess, erroring cleanly")
        error_result = {
            "id": req_id,
            "status": "error",
            "note_id": None,
            "title": "Unknown",
            "folder": "Notes",
            "attachments_count": 0,
            "attachments": [],
            "processed_at": now_iso,
            "error": f"Unrecognized op {op!r} (expected \"create\", \"export\" or \"attach\")",
        }
        _write_result(config, req_id, error_result)
        _archive_request(item_path, config.archive_dir / "failed", req_id)
        return error_result

    try:
        existing = None
        try:
            req = parse_request_from_path(item_path)
        except AttachmentLoadError:
            # A request id that already produced a note is a duplicate no
            # matter what — even if its companion files have since moved
            # (dedup by id ran before attachments mattered, and still must).
            peek_id = _peek_request_id(item_path, req_id)
            existing = dedup.check_duplicate(peek_id, "") if peek_id else None
            if not existing:
                raise
            req_id = peek_id
        if existing is None:
            if not req:
                logger.warning(f"Could not parse request from {item_path.name}")
                return {"id": req_id, "status": "skipped", "message": "Unrecognized request format"}

            req_id = req.request_id
            content_hash = req.compute_content_hash()

            # Check deduplication
            existing = dedup.check_duplicate(req.request_id, content_hash)
        if existing:
            logger.info(f"Duplicate request detected for {req_id} (note_id: {existing.get('note_id')})")
            result = {
                "id": req_id,
                "status": "duplicate",
                "note_id": existing.get("note_id"),
                "title": existing.get("title", req.title if req else None),
                "folder": existing.get("folder", req.folder if req else None),
                "attachments_count": existing.get("attachments_count", 0),
                "attachments": existing.get("attachments", []),
                "processed_at": now_iso,
                "message": "Note was already created with this request ID or content hash",
                "error": None,
            }
            _write_result(config, req_id, result)
            _archive_request(item_path, config.archive_dir, req_id)
            return result

        # Create temporary scratch directory in /tmp
        timestamp_slug = int(time.time() * 1000)
        scratch_dir = Path(f"/tmp/rich_notes_bridge_{req_id}_{timestamp_slug}")
        scratch_dir.mkdir(parents=True, exist_ok=True)

        # Synthesize HTML and stage attachments
        html_body, staged_paths = prepare_note_html_and_attachments(req, scratch_dir)
        attachment_names = [p.name for p in staged_paths]

        # Create note in Notes.app with attachments
        logger.info(f"Creating note '{req.title}' in folder '{req.folder}' with {len(staged_paths)} attachment(s)...")
        notes_res = execute_applescript_create_note(req.folder, req.title, html_body, staged_paths, scratch_dir)

        note_id = notes_res["id"]
        attachments_count = len(staged_paths)
        actual_folder = notes_res.get("folder", req.folder)

        logger.info(f"Successfully created note '{notes_res.get('name')}' (id: {note_id}) with {attachments_count} attachment(s)")

        # Record in dedup
        record_data = {
            "note_id": note_id,
            "title": req.title,
            "folder": actual_folder,
            "attachments_count": attachments_count,
            "attachments": attachment_names,
            "content_hash": content_hash,
            "created_at": now_iso,
        }
        dedup.record(req.request_id, content_hash, record_data)

        # Write result to outbox
        result = {
            "id": req_id,
            "status": "success",
            "note_id": note_id,
            "title": req.title,
            "folder": actual_folder,
            "attachments_count": attachments_count,
            "attachments": attachment_names,
            "processed_at": now_iso,
            "error": None,
        }
        _write_result(config, req_id, result)

        # Archive processed request
        _archive_request(item_path, config.archive_dir, req_id)
        return result

    except Exception as e:
        logger.error(f"Failed processing request {req_id}: {e}", exc_info=not isinstance(e, AttachmentLoadError))
        error_result = {
            "id": req_id,
            "status": "error",
            "note_id": None,
            "title": req.title if req else "Unknown",
            "folder": req.folder if req else "Notes",
            "attachments_count": 0,
            "attachments": [],
            "processed_at": now_iso,
            "error": str(e),
        }
        _write_result(config, req_id, error_result)
        _archive_request(item_path, config.archive_dir / "failed", req_id)
        return error_result

    finally:
        # Clean up scratch directory in /tmp
        if scratch_dir and scratch_dir.exists():
            try:
                shutil.rmtree(scratch_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Failed to remove scratch dir {scratch_dir}: {e}")


def _write_result(config: BridgeConfig, req_id: str, result_dict: Dict[str, Any]):
    """Writes small result JSON file into outbox for Muse to read back."""
    config.outbox_dir.mkdir(parents=True, exist_ok=True)
    out_file = config.outbox_dir / f"{req_id}.json"
    tmp_file = config.outbox_dir / f"{req_id}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(result_dict, f, indent=2, ensure_ascii=False)
    os.replace(tmp_file, out_file)
    logger.debug(f"Wrote result to {out_file}")


def _archive_request(item_path: Path, archive_base: Path, req_id: str):
    """Moves processed request from inbox to archive folder."""
    if not item_path.exists():
        return
    archive_base.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    clean_name = item_path.name
    if clean_name.endswith(".processing"):
        clean_name = clean_name[:-len(".processing")]
    dest_name = f"{clean_name}_{ts}" if (archive_base / clean_name).exists() else clean_name
    dest_path = archive_base / dest_name
    try:
        shutil.move(str(item_path), str(dest_path))
        logger.debug(f"Archived {item_path.name} -> {dest_path}")
    except Exception as e:
        logger.warning(f"Could not move {item_path} to archive: {e}")


# ----------------------------------------------------------------------
# Inbox Scanner & Watcher
# ----------------------------------------------------------------------
def is_item_ready(path: Path) -> bool:
    """Checks if a file/dir is fully written and not a partial/temp file."""
    name = path.name
    if name.startswith(".") or name.endswith((".tmp", ".temp", ".part", ".partial", ".crdownload", ".processing")):
        return False

    try:
        if path.is_dir():
            entries = list(path.iterdir())
            if not entries:
                return False
            for e in entries:
                if e.name.startswith(".") or e.name.endswith((".tmp", ".temp", ".part", ".partial")):
                    return False
        else:
            if path.stat().st_size == 0:
                return False

        mtime = path.stat().st_mtime
        if (time.time() - mtime) < 0.2:
            return False
    except Exception:
        return False
    return True


def scan_and_process_inbox(config: BridgeConfig, dedup: DedupManager) -> int:
    """Scans inbox and processes all ready requests. Returns count processed."""
    config.ensure_directories()
    items = sorted(config.inbox_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    processed_count = 0

    for item in items:
        if not item.exists():
            continue
        if not is_item_ready(item):
            continue

        processing_path = item.with_name(f"{item.name}.processing")
        try:
            item.rename(processing_path)
        except Exception:
            continue

        try:
            logger.info(f"Processing inbox item: {item.name}")
            process_request(processing_path, config, dedup)
            processed_count += 1
        except Exception as e:
            logger.error(f"Error handling {item.name}: {e}", exc_info=True)
            if processing_path.exists():
                try:
                    processing_path.rename(item)
                except Exception:
                    pass

    return processed_count


def run_daemon_loop(config: BridgeConfig, poll_interval: float = 1.0):
    """
    Main watcher loop.

    Self-healing staleness check: a long-running interpreted process keeps
    running whatever code was on disk at the moment IT started, forever —
    editing the script on disk has no effect on an already-running watcher
    until that process restarts. This silently produced stale behavior in
    production (2026-11-15: an edited-in export feature, dropped against an
    already-running watcher, got handled as a plain create because the
    resident process had no way to know the file underneath it had changed).
    To make that failure mode impossible going forward, the loop records its
    own start-of-script mtime once, and re-checks it every poll cycle; if the
    file has been edited since, it logs why and exits cleanly. Under launchd
    with KeepAlive=true (this script's own install_launchd), that exit is an
    automatic, near-instant restart onto the fresh code — no manual
    `launchctl kickstart` ever required again. Running in the foreground
    (`run`, no launchd) exits too, with a clear message instead of silently
    continuing on stale code; there's no KeepAlive to restart it there, so a
    human needs to re-launch it, but at least it says so instead of guessing.
    """
    config.ensure_directories()
    dedup = DedupManager(config.dedup_file)
    script_path = Path(__file__).resolve()
    own_code_mtime = script_path.stat().st_mtime
    logger.info(f"Starting Rich Notes Bridge daemon...")
    logger.info(f"Watching inbox : {config.inbox_dir}")
    logger.info(f"Outbox results : {config.outbox_dir}")
    logger.info(f"Archive folder : {config.archive_dir}")
    logger.info(f"Running code from {script_path} (mtime {own_code_mtime})")

    while True:
        try:
            scan_and_process_inbox(config, dedup)
        except Exception as e:
            logger.error(f"Error in watcher loop: {e}", exc_info=True)

        try:
            current_mtime = script_path.stat().st_mtime
        except Exception:
            current_mtime = own_code_mtime  # script briefly missing mid-save; don't false-trigger

        if current_mtime > own_code_mtime:
            logger.warning(
                f"{script_path.name} was edited since this watcher started "
                f"(now mtime {current_mtime}, started with {own_code_mtime}) — "
                "this process is running stale code. Exiting so launchd (KeepAlive) "
                "restarts it fresh; if running in the foreground, restart it manually."
            )
            return

        time.sleep(poll_interval)


# ----------------------------------------------------------------------
# Launchd Service Management
# ----------------------------------------------------------------------
def generate_launchd_plist(script_path: Path, base_dir: Path) -> str:
    py_path = sys.executable or "/usr/bin/python3"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py_path}</string>
        <string>{script_path.resolve()}</string>
        <string>--dir</string>
        <string>{base_dir.resolve()}</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{script_path.parent.resolve()}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>{base_dir.resolve()}/logs/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>{base_dir.resolve()}/logs/launchd.err.log</string>
</dict>
</plist>
"""


def install_launchd(config: BridgeConfig, script_path: Path):
    config.ensure_directories()
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT_PATH)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    plist_content = generate_launchd_plist(script_path, config.base_dir)
    with open(LAUNCH_AGENT_PATH, "w", encoding="utf-8") as f:
        f.write(plist_content)

    res = subprocess.run(["launchctl", "load", "-w", str(LAUNCH_AGENT_PATH)], capture_output=True, text=True)
    if res.returncode == 0:
        logger.info(f"Successfully installed and loaded LaunchAgent: {LAUNCH_AGENT_PATH}")
    else:
        logger.warning(f"Installed plist to {LAUNCH_AGENT_PATH}, but launchctl load gave: {res.stderr.strip()}")


def uninstall_launchd():
    if LAUNCH_AGENT_PATH.exists():
        subprocess.run(["launchctl", "unload", "-w", str(LAUNCH_AGENT_PATH)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        LAUNCH_AGENT_PATH.unlink()
        logger.info(f"Unloaded and removed LaunchAgent: {LAUNCH_AGENT_PATH}")
    else:
        logger.info(f"LaunchAgent plist not found at {LAUNCH_AGENT_PATH}")


def check_status(config: BridgeConfig):
    config.ensure_directories()
    print("=== Rich Notes Bridge Status ===")
    print(f"Base Directory   : {config.base_dir}")
    print(f"Inbox Directory  : {config.inbox_dir} (pending: {len(list(config.inbox_dir.glob('*')))})")
    print(f"Outbox Directory : {config.outbox_dir} (results: {len(list(config.outbox_dir.glob('*.json')))})")
    print(f"Archive Directory: {config.archive_dir}")

    if LAUNCH_AGENT_PATH.exists():
        res = subprocess.run(["launchctl", "list", LAUNCH_AGENT_LABEL], capture_output=True, text=True)
        if res.returncode == 0:
            print(f"LaunchAgent      : LOADED ({LAUNCH_AGENT_LABEL})")
            print(_watcher_freshness_line(res.stdout))
        else:
            print(f"LaunchAgent      : INSTALLED BUT NOT LOADED")
    else:
        print(f"LaunchAgent      : NOT INSTALLED")

    dedup = DedupManager(config.dedup_file)
    print(f"Processed Notes  : {len(dedup.data.get('by_id', {}))} recorded in state")


def _watcher_freshness_line(launchctl_list_output: str) -> str:
    """
    Builds the "Watcher Code" status line: compares the running watcher
    process's actual start time against the script's current mtime, as a
    passive, human-visible complement to run_daemon_loop's own self-healing
    exit-on-edit check (this just reports; it has no ability to fix anything
    — useful when the watcher is deliberately run in the foreground with no
    launchd/KeepAlive to do the restarting).
    """
    pid_match = re.search(r'"PID"\s*=\s*(\d+);', launchctl_list_output)
    if not pid_match:
        return "Watcher Code     : not currently running (no PID in launchctl list)"
    pid = pid_match.group(1)

    try:
        script_mtime = Path(__file__).resolve().stat().st_mtime
    except Exception as e:
        return f"Watcher Code     : could not stat script file: {e}"

    ps_res = subprocess.run(["ps", "-o", "lstart=", "-p", pid], capture_output=True, text=True)
    if ps_res.returncode != 0 or not ps_res.stdout.strip():
        return f"Watcher Code     : running (pid {pid}), but couldn't read its start time"

    try:
        proc_start = datetime.datetime.strptime(ps_res.stdout.strip(), "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError as e:
        return f"Watcher Code     : running (pid {pid}), but couldn't parse start time: {e}"

    if proc_start < script_mtime:
        return (
            f"Watcher Code     : STALE — pid {pid} started before the script's last edit "
            f"(the self-healing check in run_daemon_loop should restart it within "
            f"~1 poll cycle; if it hasn't, see TROUBLESHOOTING in README.md)"
        )
    return f"Watcher Code     : fresh (pid {pid} started after the script's last edit)"


# ----------------------------------------------------------------------
# Real-Image Self Test Utility
# ----------------------------------------------------------------------
def get_or_create_real_test_image() -> Tuple[str, bytes]:
    """Finds a real photographic image from desktop or resamples wallpaper."""
    desktop_dir = Path(os.path.expanduser("~/Desktop"))
    for ext in ["*.jpg", "*.jpeg", "*.png", "*.webp"]:
        candidates = list(desktop_dir.glob(ext))
        if candidates:
            cand = candidates[0]
            with open(cand, "rb") as f:
                return cand.name, f.read()

    # Fallback to resampling Apple system wallpaper using sips
    wallpapers = [
        Path("/System/Library/Wallpapers/.default/DefaultAerial.heic"),
        Path("/System/Library/CoreServices/DefaultDesktop.heic"),
    ]
    for w in wallpapers:
        if w.exists():
            tmp_out = Path("/tmp/real_wallpaper_sample.jpg")
            subprocess.run(
                ["sips", "-s", "format", "jpeg", str(w), "--resampleWidth", "400", "--out", str(tmp_out)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if tmp_out.exists():
                with open(tmp_out, "rb") as f:
                    return "apple_wallpaper.jpg", f.read()

    # Synthetic fallback: 1x1 png
    b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    return "test.png", base64.b64decode(b64)


def run_self_test(config: BridgeConfig, keep_note: bool = False) -> bool:
    """
    End-to-end self test with a REAL image and rich capabilities:
    - Tables, checklists, headings, code formatting
    - Real native attachment creation
    - Deduplication check
    - Outbox receipt verification
    """
    logger.info("Starting End-to-End Self Test with real image and rich formatting...")
    config.ensure_directories()
    dedup = DedupManager(config.dedup_file)

    test_req_id = f"test-real-{int(time.time())}"
    test_title = f"Rich Bridge Real Image Test ({test_req_id})"
    test_folder = "Notes"

    img_filename, img_bytes = get_or_create_real_test_image()
    img_b64 = base64.b64encode(img_bytes).decode("utf-8")
    logger.info(f"Using real test image: {img_filename} ({len(img_bytes):,} bytes)")

    test_payload = {
        "id": test_req_id,
        "title": test_title,
        "folder": test_folder,
        "format": "markdown",
        "body": (
            "## Real Attachment & Rich Note Capabilities Test\n\n"
            "This note verifies that **real native attachments** load correctly in Apple Notes and Quick Look.\n\n"
            "### Action Items\n"
            "- [x] Import real desktop image via AppleScript native data handler\n"
            "- [x] Support markdown tables and styled checklists\n"
            "- [ ] Ready for Muse paired device drop\n\n"
            "### Verification Table\n"
            "| Component | Mechanism | Status |\n"
            "|---|---|---|\n"
            "| Attachment | `make new attachment with data` | VERIFIED |\n"
            "| Quick Look | Native CoreData Store | SUPPORTED |\n"
            "| Format | Full Markdown + Tables + Checklists | ACTIVE |\n\n"
            f"> Attached image: **{img_filename}** ({len(img_bytes):,} bytes)\n"
        ),
        "attachments": {img_filename: img_b64},
    }

    # Drop into inbox
    test_drop_path = config.inbox_dir / f"{test_req_id}.json"
    logger.info(f"Dropping test request into {test_drop_path}...")
    with open(test_drop_path, "w", encoding="utf-8") as f:
        json.dump(test_payload, f, indent=2)
    time.sleep(0.3)

    # Process inbox (or wait for background daemon to pick it up)
    logger.info("Scanning and processing inbox...")
    processed_count = scan_and_process_inbox(config, dedup)
    result_path = config.outbox_dir / f"{test_req_id}.json"

    # If background daemon picked it up first, wait up to 3s for result file
    for _ in range(10):
        if result_path.exists():
            break
        time.sleep(0.3)

    if not result_path.exists():
        logger.error(f"Self-test FAILED: Outbox result file {result_path} not found")
        return False

    with open(result_path, "r", encoding="utf-8") as f:
        result_data = json.load(f)

    logger.info(f"Outbox Result: {json.dumps(result_data, indent=2)}")
    if result_data.get("status") not in ["success", "duplicate"]:
        logger.error(f"Self-test FAILED: Outbox status was {result_data.get('status')} - {result_data.get('error')}")
        return False

    created_note_id = result_data.get("note_id")
    if not created_note_id:
        logger.error("Self-test FAILED: No note_id returned")
        return False

    # Verify Note in Apple Notes via JXA
    logger.info(f"Verifying Note in Notes.app (id: {created_note_id})...")
    jxa_verify = f"""
    function run() {{
        const Notes = Application('Notes');
        const note = Notes.notes.byId('{created_note_id}');
        const atts = note.attachments();
        return JSON.stringify({{
            name: note.name(),
            attachments_count: atts.length,
            attachment_name: atts.length > 0 ? atts[0].name() : null
        }});
    }}
    """
    res = subprocess.run(["osascript", "-l", "JavaScript", "-"], input=jxa_verify, capture_output=True, text=True)
    if res.returncode != 0:
        logger.error(f"Self-test FAILED: Could not query note in Notes.app: {res.stderr}")
        return False

    note_info = json.loads(res.stdout.strip())
    logger.info(f"Notes.app confirmation: {note_info}")
    if note_info.get("attachments_count", 0) < 1:
        logger.error("Self-test FAILED: Note does not have attachments embedded")
        return False

    # Test Deduplication
    logger.info("Testing deduplication by re-dropping same request...")
    with open(test_drop_path, "w", encoding="utf-8") as f:
        json.dump(test_payload, f, indent=2)
    time.sleep(0.3)

    scan_and_process_inbox(config, dedup)
    time.sleep(0.5)

    with open(result_path, "r", encoding="utf-8") as f:
        dedup_result = json.load(f)

    logger.info(f"Dedup verification result status: {dedup_result.get('status')}")
    if dedup_result.get("status") != "duplicate":
        logger.warning(f"Expected duplicate status, got: {dedup_result.get('status')}")

    # Clean up note if requested
    if not keep_note:
        logger.info(f"Cleaning up test note {created_note_id} from Notes.app...")
        subprocess.run(
            ["osascript", "-e", f'tell application "Notes" to delete note id "{created_note_id}"'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Test note deleted.")
    else:
        logger.info(f"Keeping test note in Notes.app per request (id: {created_note_id})")

    logger.info("✅ End-to-End Self Test with Real Image PASSED successfully!")
    return True


# ----------------------------------------------------------------------
# Audio / Video Self Test Utility
# ----------------------------------------------------------------------
def _synth_sine_wav(path: Path, seconds: float = 0.6, freq: float = 440.0, rate: int = 22050):
    """Writes a short mono 16-bit sine tone with the stdlib `wave` module."""
    import math
    import struct
    import wave

    n = int(seconds * rate)
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


def generate_test_media(work_dir: Path) -> Dict[str, Any]:
    """
    Produces test media with stock tools only. Returns
    {"audio": (name, bytes, note), "video": (name, bytes, note) | None, "video_skip": reason}.
      audio: .m4a via `afconvert -f m4af -d aac` (stock macOS); if afconvert is
             missing/fails, the raw .wav is used and the note says so.
      video: no stock macOS CLI synthesizes video frames (avconvert only
             transcodes existing assets; ffmpeg is not stock). The closest
             stock artifact is an MPEG-4 (.mp4) container holding the AAC
             track (`afconvert -f mp4f`), which exercises the .mp4 attachment
             path. If that fails, the video case is skipped with the reason.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    wav = work_dir / "bridge_tone.wav"
    _synth_sine_wav(wav)
    out: Dict[str, Any] = {"audio": None, "video": None, "video_skip": None}
    afconvert = shutil.which("afconvert")

    m4a = work_dir / "bridge_tone.m4a"
    if afconvert:
        r = subprocess.run([afconvert, "-f", "m4af", "-d", "aac", str(wav), str(m4a)], capture_output=True, text=True)
        if r.returncode == 0 and m4a.exists() and m4a.stat().st_size > 0:
            out["audio"] = ("bridge_tone.m4a", m4a.read_bytes(), "AAC .m4a synthesized via afconvert")
        else:
            logger.warning(f"afconvert → .m4a failed ({r.stderr.strip()}); falling back to .wav")
    if out["audio"] is None:
        out["audio"] = ("bridge_tone.wav", wav.read_bytes(),
                        "afconvert unavailable — using stdlib-synthesized .wav instead of .m4a")

    mp4 = work_dir / "bridge_tone.mp4"
    if afconvert:
        r = subprocess.run([afconvert, "-f", "mp4f", "-d", "aac", str(wav), str(mp4)], capture_output=True, text=True)
        if r.returncode == 0 and mp4.exists() and mp4.stat().st_size > 0:
            out["video"] = ("bridge_tone.mp4", mp4.read_bytes(),
                            "audio-only MPEG-4 container (no stock tool synthesizes video frames)")
        else:
            out["video_skip"] = f"afconvert -f mp4f failed: {r.stderr.strip() or r.returncode}"
    else:
        out["video_skip"] = "afconvert not available; no stock tool can produce an .mp4"
    return out


def _jxa_note_attachments(note_id: str) -> Dict[str, Any]:
    jxa = f"""
    function run() {{
        const Notes = Application('Notes');
        const note = Notes.notes.byId({json.dumps(note_id)});
        const atts = note.attachments();
        return JSON.stringify({{
            name: note.name(),
            attachments_count: atts.length,
            attachment_names: atts.map(a => a.name())
        }});
    }}
    """
    res = subprocess.run(["osascript", "-l", "JavaScript", "-"], input=jxa, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"JXA read-back failed: {res.stderr.strip()}")
    return json.loads(res.stdout.strip())


def _attachment_name_matches(expected: str, names: List[str]) -> bool:
    stem = Path(expected).stem
    return any(n == expected or Path(n).stem == stem for n in (names or []))


def _drop_request_and_wait(config: BridgeConfig, dedup: DedupManager, req_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    drop = config.inbox_dir / f"{req_id}.json"
    result_path = config.outbox_dir / f"{req_id}.json"
    if result_path.exists():
        result_path.unlink()
    with open(drop, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    time.sleep(0.3)
    scan_and_process_inbox(config, dedup)
    for _ in range(20):
        if result_path.exists():
            break
        time.sleep(0.3)
    if not result_path.exists():
        return None
    with open(result_path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_media_self_test(config: BridgeConfig, keep_note: bool = False) -> bool:
    """
    Creates one note per media case (audio .m4a — or .wav fallback — and
    .mp4 when producible), each with base64 attachment data, and verifies via
    JXA that attachments_count >= 1 and the attachment name matches. Also
    checks magic-byte detection of each generated file so an unnamed blob of
    that type would be labeled correctly. Missing .mp4 → logged skip, not fail.
    """
    logger.info("Starting audio/video attachment self test...")
    config.ensure_directories()
    dedup = DedupManager(config.dedup_file)
    work = Path(f"/tmp/rich_notes_bridge_media_test_{int(time.time() * 1000)}")
    created: List[str] = []
    ok = True
    try:
        media = generate_test_media(work)
        cases = [("audio", media["audio"])]
        if media["video"]:
            cases.append(("video", media["video"]))
        else:
            logger.warning(f"Video case SKIPPED: {media['video_skip']}")

        for kind, (fname, data, note) in cases:
            logger.info(f"[{kind}] {fname} ({len(data):,} bytes) — {note}")
            detected = detect_file_extension(data)
            if detected != Path(fname).suffix:
                logger.error(f"[{kind}] FAIL: magic-byte detection gave {detected}, expected {Path(fname).suffix}")
                ok = False
            rid = f"test-media-{kind}-{int(time.time() * 1000)}"
            payload = {
                "id": rid,
                "title": f"Rich Bridge {kind.title()} Test ({rid})",
                "folder": "Notes",
                "body": f"Self-test {kind} attachment: **{fname}** — {note}",
                "attachments": {fname: base64.b64encode(data).decode("ascii")},
            }
            result = _drop_request_and_wait(config, dedup, rid, payload)
            if not result or result.get("status") != "success":
                logger.error(f"[{kind}] FAIL: result {result}")
                ok = False
                continue
            created.append(result["note_id"])
            if result.get("attachments_count") != 1 or result.get("attachments") != [fname]:
                logger.error(f"[{kind}] FAIL: receipt attachments {result.get('attachments')} (count {result.get('attachments_count')})")
                ok = False
            info = _jxa_note_attachments(result["note_id"])
            logger.info(f"[{kind}] Notes.app confirmation: {info}")
            if info.get("attachments_count", 0) < 1 or not _attachment_name_matches(fname, info.get("attachment_names")):
                logger.error(f"[{kind}] FAIL: Notes.app shows {info}")
                ok = False
            else:
                logger.info(f"[{kind}] PASS")
    except Exception as e:
        logger.error(f"Media self test crashed: {e}", exc_info=True)
        ok = False
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if not keep_note:
            for nid in created:
                subprocess.run(
                    ["osascript", "-e", f'tell application "Notes" to delete note id "{_escape_applescript_string(nid)}"'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
    logger.info("✅ Media self test PASSED" if ok else "❌ Media self test FAILED")
    return ok


# ----------------------------------------------------------------------
# Offline unit tests (no Notes.app / osascript needed; run anywhere)
# ----------------------------------------------------------------------
def run_unit_tests() -> bool:
    import tempfile

    failures: List[str] = []

    def check(cond: bool, label: str):
        (logger.info if cond else logger.error)(f"{'PASS' if cond else 'FAIL'}: {label}")
        if not cond:
            failures.append(label)

    # Fix 1 — detection
    cases = {
        b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42isom": ".m4a",
        b"\x00\x00\x00\x18ftypM4B \x00\x00\x00\x00": ".m4a",
        b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00": ".mov",
        b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00": ".heic",
        b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00": ".heic",
        b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00": ".mp4",
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00": ".mp4",
        b"\x00\x00\x00\x18ftypdash\x00\x00\x00\x00": ".mp4",
        b"\x00\x00\x00\x18ftypXXXX\x00\x00\x00\x00M4A ": ".m4a",  # compat brand
        b"ID3\x04\x00\x00\x00\x00\x00\x00": ".mp3",
        b"\xff\xfb\x90\x64\x00": ".mp3",
        b"\xff\xf1\x50\x80": ".aac",
        b"RIFF\x24\x00\x00\x00WAVEfmt ": ".wav",
        b"RIFF\x24\x00\x00\x00WEBPVP8 ": ".webp",
        b"fLaC\x00\x00\x00\x22": ".flac",
        b"OggS\x00\x02": ".ogg",
        b"\x89PNG\r\n\x1a\n....": ".png",
        b"\xff\xd8\xff\xe0JFIF": ".jpg",
        b"PK\x03\x04....": ".zip",
        b"%PDF-1.7": ".pdf",
        b"\x00\x01\x02\x03random": ".bin",
    }
    for blob, want in cases.items():
        check(detect_file_extension(blob) == want, f"detect {blob[:12]!r} -> {want} (got {detect_file_extension(blob)})")
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "t.wav"
        _synth_sine_wav(wav, seconds=0.05)
        check(detect_file_extension(wav.read_bytes()) == ".wav", "stdlib wave output detected as .wav")
    # Named file wins over magic bytes (docx is a ZIP container)
    check(staged_attachment_name("report.docx", b"PK\x03\x04") == "report.docx", "sender filename .docx not relabeled .zip")
    check(staged_attachment_name("voice", b"\x00\x00\x00\x18ftypM4A \x00") == "voice.m4a", "extensionless name gets detected .m4a")
    check(staged_attachment_name("attachment_1.png", b"ID3....", auto=True) == "attachment_1.mp3", "auto placeholder name re-detected")
    check(staged_attachment_name("attachment_1.png", b"ID3", hint="clip.mp3", auto=True) == "clip.mp3", "auto name uses path hint")

    # Fix 2 — strict decode / never drop
    good = base64.b64encode(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 64).decode()
    check(decode_base64_string(good)[4:8] == b"ftyp", "valid base64 decodes")
    check(decode_base64_string(good.rstrip("=")) == decode_base64_string(good), "unpadded base64 accepted")
    for bad in ["clips/stoats.m4a", "not base64!!", "abcd*efgh"]:
        try:
            decode_base64_string(bad)
            check(False, f"strict decode rejects {bad!r}")
        except ValueError:
            check(True, f"strict decode rejects {bad!r}")

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "real.m4a").write_bytes(b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00")
        out: Dict[str, bytes] = {}
        _extract_attachments({"real.m4a": "real.m4a", "inline.m4a": good}, out, base)
        check(set(out) == {"real.m4a", "inline.m4a"}, "dict entries: file path + inline base64 both load")
        for label, obj in [
            ("dict missing path", {"x.m4a": "missing/x.m4a"}),
            ("dict dotless missing path", {"x": "clips/audio"}),
            ("dict garbage", {"x.m4a": "%%%%not-base64%%%%%%%%%%%%"}),
            ("dict empty value", {"x.m4a": ""}),
            ("list item no data", [{"name": "x.m4a"}]),
            ("list item missing path", [{"name": "x.m4a", "path": "nope.m4a"}]),
            ("list bare missing path", ["nope.m4a"]),
        ]:
            try:
                _extract_attachments(obj, {}, base)
                check(False, f"{label} raises AttachmentLoadError")
            except AttachmentLoadError as e:
                check("x.m4a" in str(e) or "nope.m4a" in str(e) or "clips/audio" in str(e) or "'x'" in str(e),
                      f"{label} raises AttachmentLoadError naming the entry ({e})")
        # JPEG base64 starts with "/9j/" — must not be mistaken for a path.
        jpg_b64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 40).decode()
        o2: Dict[str, bytes] = {}
        _extract_attachments({"p.jpg": jpg_b64}, o2, base)
        check(o2["p.jpg"][:3] == b"\xff\xd8\xff", "JPEG base64 starting '/9j/' still decodes")

        # Staging collision: two entries with the same basename both survive.
        sd = base / "stage"
        sd.mkdir()
        staged = stage_attachments({"a/x.png": b"\x89PNG\r\n\x1a\n1", "b/x.png": b"\x89PNG\r\n\x1a\n2"}, sd)
        check(len(staged) == 2 and len({p.name for p in staged}) == 2, f"colliding names both staged ({[p.name for p in staged]})")

        # End-to-end through process_request (fails before any osascript call).
        cfg = BridgeConfig(base / "bridge")
        cfg.ensure_directories()
        dd = DedupManager(cfg.dedup_file)
        (cfg.inbox_dir / "missing-att.json").write_text(json.dumps(
            {"id": "missing-att", "title": "T", "attachments": {"ghost.m4a": "ghost.m4a"}}))
        r = process_request(cfg.inbox_dir / "missing-att.json", cfg, dd)
        check(r["status"] == "error" and "ghost.m4a" in (r.get("error") or "") and r["note_id"] is None,
              f"missing attachment → error naming file, no note ({r.get('error')})")
        rr = json.loads((cfg.outbox_dir / "missing-att.json").read_text())
        check(rr["status"] == "error" and rr["attachments"] == [], "outbox receipt is error with empty attachments list")

        # Dedup by id still wins even if the companion file has since vanished.
        dd.record("old-req", "deadbeef", {"note_id": "x-coredata://S/ICNote/p1", "title": "Old",
                                         "folder": "Notes", "attachments_count": 1, "attachments": ["a.m4a"]})
        (cfg.inbox_dir / "old-req.json").write_text(json.dumps(
            {"id": "old-req", "title": "Old", "attachments": {"a.m4a": "gone/a.m4a"}}))
        r = process_request(cfg.inbox_dir / "old-req.json", cfg, dd)
        check(r["status"] == "duplicate", f"re-dropped id with moved file → duplicate (got {r['status']})")

        # attach op: unloadable attachment → error before any Notes call.
        (cfg.inbox_dir / "att-bad.json").write_text(json.dumps(
            {"op": "attach", "id": "att-bad", "note_id": "x-coredata://S/ICNote/p1", "attachments": {"q.m4a": "nope/q.m4a"}}))
        r = process_request(cfg.inbox_dir / "att-bad.json", cfg, dd)
        check(r["status"] == "error" and r["op"] == "attach" and "q.m4a" in r["error"], f"attach with missing file → error ({r['error']})")
        (cfg.inbox_dir / "att-none.json").write_text(json.dumps({"op": "attach", "id": "att-none", "note_id": "x"}))
        r = process_request(cfg.inbox_dir / "att-none.json", cfg, dd)
        check(r["status"] == "error", "attach with no attachments → error")

        # Content hash of a legacy manifest is unchanged by the new parsing.
        (cfg.inbox_dir / "legacy.json").write_text(json.dumps(
            {"id": "legacy", "title": "L", "body": "b", "images": [{"data": good}]}))
        req = parse_request_from_path(cfg.inbox_dir / "legacy.json")
        h = hashlib.sha256()
        for part in (b"Notes", b"|", b"L", b"|", b"b", b"|att:", b"attachment_1.png", b":",
                     hashlib.sha256(decode_base64_string(good)).digest()):
            h.update(part)
        check(req.compute_content_hash() == h.hexdigest(), "legacy unnamed-list manifest keeps its content hash")
        check(staged_attachment_name("attachment_1.png", req.attachments["attachment_1.png"],
                                     auto=True) == "attachment_1.m4a", "…but stages with its real .m4a extension")

    logger.info(f"Unit tests: {'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    return not failures


# ----------------------------------------------------------------------
# Export Self Test Utility
# ----------------------------------------------------------------------
def _get_plaintext_directly_for_test(note_id: str) -> str:
    """
    Independent oracle for the export self-test: reads a note's plaintext via a
    minimal standalone AppleScript call captured over stdout — deliberately NOT
    using execute_applescript_export_note (which writes via a file instead), so
    the self-test's expectation isn't computed with the same code path it is
    verifying. A real export bug in the file-writing code would still be caught
    by comparing against this independently-derived value.
    """
    safe_note_id = _escape_applescript_string(note_id)
    script = f'tell application "Notes" to plaintext of note id "{safe_note_id}"'
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Oracle plaintext read failed: {res.stderr.strip()}")
    # osascript appends a trailing newline to -e output; strip exactly that one,
    # not arbitrary trailing whitespace the note's own content might end with.
    out = res.stdout
    if out.endswith("\n"):
        out = out[:-1]
    return out


def _build_large_plain_text_body(target_chars: int = 125_000) -> str:
    """
    Builds a plain-text body with NO markdown-special characters (#, *, -, |, >,
    backtick) so it survives create's markdown_to_html -> Notes.app render ->
    plaintext round trip without any syntax being stripped or reinterpreted —
    each line becomes exactly one <div> and should come back as exactly one
    plaintext line. Varied sentence content (not one repeated line) so a
    truncation bug can't hide behind a sea of identical characters.
    """
    sentences = [
        "The quick brown fox jumps over the lazy dog near the old stone bridge.",
        "Paste bin contents often exceed what a chat connector can read in one call.",
        "Apple Notes stores note bodies as rendered HTML inside a CoreData store.",
        "This line intentionally avoids markdown syntax so the round trip stays clean.",
        "Every paragraph here is distinct text, not a repeated filler character.",
        "The bridge communicates over a local inbox and outbox folder pair only.",
        "Export requests are strictly read only and never modify the source note.",
        "Large bodies are written directly to file from within the AppleScript itself.",
    ]
    lines = []
    total = 0
    i = 0
    while total < target_chars:
        line = f"{sentences[i % len(sentences)]} (line {i})"
        lines.append(line)
        total += len(line) + 1
        i += 1
    return "\n".join(lines)


def run_export_self_test(config: BridgeConfig, keep_note: bool = False) -> bool:
    """
    End-to-end self test for the export operation:
    1. Creates a note with a body over 100 KB via the normal create path.
    2. Independently reads the note's real plaintext (oracle, see
       _get_plaintext_directly_for_test) and computes its expected char count
       and SHA-256 BEFORE exporting anything.
    3. Exports the note via the bridge and asserts the exported .body.txt
       file's char count and SHA-256 match the independently-computed oracle
       values exactly, and that .body.html was written and is non-empty.
    4. Re-drops the SAME export request id and asserts it succeeds again with
       fresh output (not blocked by the create-dedup mechanism).
    5. Exercises title-based lookup: a unique title resolves and exports; a
       title shared by two notes errors out and lists both candidate IDs
       rather than guessing.
    6. Exercises error handling: a well-formed but nonexistent note_id
       produces a clean "status": "error" receipt, not a crash.
    """
    logger.info("Starting Export Self Test (>100KB round trip, re-export, title lookup, error handling)...")
    config.ensure_directories()
    dedup = DedupManager(config.dedup_file)

    created_note_ids: List[str] = []

    def _cleanup():
        if keep_note:
            logger.info(f"Keeping {len(created_note_ids)} export-test note(s) in Notes.app per request")
            return
        for nid in created_note_ids:
            subprocess.run(
                ["osascript", "-e", f'tell application "Notes" to delete note id "{nid}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        logger.info(f"Cleaned up {len(created_note_ids)} export-test note(s)")

    try:
        # --- 1. Create the >100KB source note ---
        large_body = _build_large_plain_text_body()
        logger.info(f"Built test body: {len(large_body):,} chars")
        if len(large_body) < 100_000:
            logger.error(f"Export self-test body generator produced only {len(large_body)} chars — test is invalid")
            return False

        create_req_id = f"export-test-create-{int(time.time())}"
        create_title = f"Export Bridge Large Note Test ({create_req_id})"
        create_payload = {"id": create_req_id, "title": create_title, "folder": "Notes", "format": "markdown", "body": large_body}
        (config.inbox_dir / f"{create_req_id}.json").write_text(json.dumps(create_payload), encoding="utf-8")
        time.sleep(0.3)
        scan_and_process_inbox(config, dedup)

        create_result_path = config.outbox_dir / f"{create_req_id}.json"
        for _ in range(10):
            if create_result_path.exists():
                break
            time.sleep(0.3)
        if not create_result_path.exists():
            logger.error("Export self-test FAILED: create step produced no outbox receipt")
            return False
        create_result = json.loads(create_result_path.read_text(encoding="utf-8"))
        if create_result.get("status") != "success" or not create_result.get("note_id"):
            logger.error(f"Export self-test FAILED: create step did not succeed: {create_result}")
            return False
        note_id = create_result["note_id"]
        created_note_ids.append(note_id)
        logger.info(f"Created large test note: {note_id}")

        # --- 2. Independent oracle, computed BEFORE the export code runs ---
        oracle_plaintext = _get_plaintext_directly_for_test(note_id)
        oracle_chars = len(oracle_plaintext)
        oracle_sha256 = hashlib.sha256(oracle_plaintext.encode("utf-8")).hexdigest()
        logger.info(f"Oracle plaintext: {oracle_chars:,} chars, sha256={oracle_sha256[:12]}...")

        # --- 3. Export via the bridge and verify against the oracle ---
        export_req_id = f"export-test-{int(time.time())}"
        export_payload = {"id": export_req_id, "op": "export", "note_id": note_id, "title": None}
        (config.inbox_dir / f"{export_req_id}.json").write_text(json.dumps(export_payload), encoding="utf-8")
        time.sleep(0.3)
        scan_and_process_inbox(config, dedup)

        export_result_path = config.outbox_dir / f"{export_req_id}.json"
        for _ in range(10):
            if export_result_path.exists():
                break
            time.sleep(0.3)
        if not export_result_path.exists():
            logger.error("Export self-test FAILED: export step produced no outbox receipt")
            return False
        export_result = json.loads(export_result_path.read_text(encoding="utf-8"))
        logger.info(f"Export receipt: {json.dumps(export_result, indent=2)}")
        if export_result.get("status") != "success":
            logger.error(f"Export self-test FAILED: export status was {export_result.get('status')}: {export_result.get('error')}")
            return False
        if export_result.get("body_chars") != oracle_chars:
            logger.error(f"Export self-test FAILED: body_chars {export_result.get('body_chars')} != oracle {oracle_chars}")
            return False
        if export_result.get("body_sha256") != oracle_sha256:
            logger.error(f"Export self-test FAILED: body_sha256 mismatch vs oracle")
            return False

        body_file = config.outbox_dir / export_result["body_file"]
        html_file = config.outbox_dir / export_result["html_file"]
        if not body_file.exists() or not html_file.exists():
            logger.error("Export self-test FAILED: .body.txt / .body.html not found in outbox")
            return False
        on_disk_bytes = body_file.read_bytes()
        on_disk_sha256 = hashlib.sha256(on_disk_bytes).hexdigest()
        if on_disk_sha256 != oracle_sha256:
            logger.error("Export self-test FAILED: on-disk .body.txt sha256 doesn't match the oracle")
            return False
        if html_file.stat().st_size == 0:
            logger.error("Export self-test FAILED: .body.html is empty")
            return False
        logger.info("Export round-trip verified: char count and SHA-256 match the independent oracle exactly.")

        # --- 4. Re-export the same id: must succeed fresh, not be dedup-blocked ---
        time.sleep(0.3)
        (config.inbox_dir / f"{export_req_id}.json").write_text(json.dumps(export_payload), encoding="utf-8")
        time.sleep(0.3)
        scan_and_process_inbox(config, dedup)
        time.sleep(0.3)
        reexport_result = json.loads(export_result_path.read_text(encoding="utf-8"))
        if reexport_result.get("status") != "success":
            logger.error(f"Export self-test FAILED: re-export status was {reexport_result.get('status')}, expected fresh success (not dedup-blocked)")
            return False
        logger.info("Re-export verified: not blocked by create-dedup, produced fresh success.")

        # --- 5a. Title-based lookup: unique title resolves ---
        title_req_id = f"export-test-title-{int(time.time())}"
        title_payload = {"id": title_req_id, "op": "export", "note_id": None, "title": create_title}
        (config.inbox_dir / f"{title_req_id}.json").write_text(json.dumps(title_payload), encoding="utf-8")
        time.sleep(0.3)
        scan_and_process_inbox(config, dedup)
        title_result_path = config.outbox_dir / f"{title_req_id}.json"
        for _ in range(10):
            if title_result_path.exists():
                break
            time.sleep(0.3)
        title_result = json.loads(title_result_path.read_text(encoding="utf-8")) if title_result_path.exists() else {}
        if title_result.get("status") != "success" or title_result.get("note_id") != note_id:
            logger.error(f"Export self-test FAILED: title-based lookup did not resolve to the right note: {title_result}")
            return False
        logger.info("Title-based lookup (unique match) verified.")

        # --- 5b. Title-based lookup: duplicate title errors with candidates listed ---
        dup_title = f"Export Bridge Dup Title Test {int(time.time())}"
        dup_payload_a = {"id": f"{title_req_id}-dup-a", "title": dup_title, "folder": "Notes", "format": "markdown", "body": "First duplicate."}
        dup_payload_b = {"id": f"{title_req_id}-dup-b", "title": dup_title, "folder": "Notes", "format": "markdown", "body": "Second duplicate."}
        for p in (dup_payload_a, dup_payload_b):
            (config.inbox_dir / f"{p['id']}.json").write_text(json.dumps(p), encoding="utf-8")
        time.sleep(0.3)
        scan_and_process_inbox(config, dedup)
        for p in (dup_payload_a, dup_payload_b):
            res_path = config.outbox_dir / f"{p['id']}.json"
            if res_path.exists():
                r = json.loads(res_path.read_text(encoding="utf-8"))
                if r.get("status") == "success" and r.get("note_id"):
                    created_note_ids.append(r["note_id"])

        dup_export_req_id = f"export-test-dup-{int(time.time())}"
        dup_export_payload = {"id": dup_export_req_id, "op": "export", "note_id": None, "title": dup_title}
        (config.inbox_dir / f"{dup_export_req_id}.json").write_text(json.dumps(dup_export_payload), encoding="utf-8")
        time.sleep(0.3)
        scan_and_process_inbox(config, dedup)
        dup_result_path = config.outbox_dir / f"{dup_export_req_id}.json"
        for _ in range(10):
            if dup_result_path.exists():
                break
            time.sleep(0.3)
        dup_result = json.loads(dup_result_path.read_text(encoding="utf-8")) if dup_result_path.exists() else {}
        if dup_result.get("status") != "error" or len(dup_result.get("candidates", [])) != 2:
            logger.error(f"Export self-test FAILED: ambiguous-title export should error with 2 candidates, got: {dup_result}")
            return False
        logger.info("Title-based lookup (ambiguous match) verified: errored cleanly, listed both candidates.")

        # --- 6. Bogus note_id: clean error, no crash ---
        bogus_req_id = f"export-test-bogus-{int(time.time())}"
        bogus_payload = {"id": bogus_req_id, "op": "export", "note_id": "x-coredata://00000000-0000-0000-0000-000000000000/ICNote/p999999999", "title": None}
        (config.inbox_dir / f"{bogus_req_id}.json").write_text(json.dumps(bogus_payload), encoding="utf-8")
        time.sleep(0.3)
        processed = scan_and_process_inbox(config, dedup)  # must not raise
        bogus_result_path = config.outbox_dir / f"{bogus_req_id}.json"
        for _ in range(10):
            if bogus_result_path.exists():
                break
            time.sleep(0.3)
        bogus_result = json.loads(bogus_result_path.read_text(encoding="utf-8")) if bogus_result_path.exists() else {}
        if bogus_result.get("status") != "error" or not bogus_result.get("error"):
            logger.error(f"Export self-test FAILED: bogus note_id should produce a clean error receipt, got: {bogus_result}")
            return False
        logger.info("Bogus note_id handling verified: clean error receipt, watcher stayed alive.")

        logger.info("✅ Export Self Test PASSED successfully!")
        return True

    finally:
        _cleanup()


# ----------------------------------------------------------------------
# CLI Interface
# ----------------------------------------------------------------------
def main():
    # NOTE on default=argparse.SUPPRESS below: --dir/-v are registered via `parents=`
    # on BOTH the top-level parser AND every subparser, so a concrete default on either
    # copy wins depending on argparse's internal re-application of subparser defaults —
    # in practice this silently discarded "--dir X test" (flag BEFORE the subcommand,
    # the only form this script's own launchd plist ever generates; see
    # generate_launchd_plist) and silently fell back to DEFAULT_BASE_DIR. SUPPRESS means
    # an unset flag leaves the attribute off the namespace entirely instead of
    # re-asserting a default, so whichever parser actually saw the flag is the one that
    # wins; the real default is applied via getattr() below instead of default=.
    # Verified empirically (2026-11-15) that this is the only combination where
    # "--dir X test", "test --dir X", and "test" (no flag) all resolve correctly.
    base_parser = argparse.ArgumentParser(add_help=False)
    base_parser.add_argument(
        "--dir",
        type=Path,
        default=argparse.SUPPRESS,
        help=f"Base directory for inbox/outbox/archive (default: {DEFAULT_BASE_DIR})",
    )
    base_parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="Enable verbose debug logging")

    parser = argparse.ArgumentParser(
        description="Rich Notes Bridge for macOS Apple Notes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[base_parser],
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser("run", parents=[base_parser], help="Run watcher daemon loop")
    subparsers.add_parser("daemon", parents=[base_parser], help="Alias for run")
    subparsers.add_parser("process", parents=[base_parser], help="Process pending requests in inbox once and exit")
    subparsers.add_parser("once", parents=[base_parser], help="Alias for process")
    subparsers.add_parser("install", parents=[base_parser], help="Install and load launchd service")
    subparsers.add_parser("uninstall", parents=[base_parser], help="Unload and remove launchd service")
    subparsers.add_parser("status", parents=[base_parser], help="Show bridge status and pending queue")

    test_parser = subparsers.add_parser("test", parents=[base_parser], help="Run end-to-end self tests (image + audio/video attachments + export round trip)")
    test_parser.add_argument("--keep", action="store_true", help="Keep the created test note in Notes.app")
    subparsers.add_parser("unit-test", parents=[base_parser], help="Run offline unit tests (no Notes.app needed)")

    args = parser.parse_args()
    config = BridgeConfig(getattr(args, "dir", DEFAULT_BASE_DIR))
    setup_logging(config, getattr(args, "verbose", False))

    script_path = Path(__file__).resolve()

    if args.command in ["run", "daemon"]:
        run_daemon_loop(config)
    elif args.command in ["process", "once"]:
        dedup = DedupManager(config.dedup_file)
        count = scan_and_process_inbox(config, dedup)
        logger.info(f"Processed {count} request(s).")
    elif args.command == "install":
        install_launchd(config, script_path)
    elif args.command == "uninstall":
        uninstall_launchd()
    elif args.command == "status":
        check_status(config)
    elif args.command == "test":
        unit_passed = run_unit_tests()
        image_test_passed = run_self_test(config, keep_note=args.keep)
        media_test_passed = run_media_self_test(config, keep_note=args.keep)
        export_test_passed = run_export_self_test(config, keep_note=args.keep)
        all_ok = unit_passed and image_test_passed and media_test_passed and export_test_passed
        if all_ok:
            logger.info("✅ ALL self tests passed (unit + image + audio/video + export round-trip).")
        else:
            logger.error(
                f"Self test suite FAILED — unit: {'PASS' if unit_passed else 'FAIL'}, "
                f"image: {'PASS' if image_test_passed else 'FAIL'}, "
                f"audio/video: {'PASS' if media_test_passed else 'FAIL'}, "
                f"export: {'PASS' if export_test_passed else 'FAIL'}"
            )
        sys.exit(0 if all_ok else 1)
    elif args.command == "unit-test":
        sys.exit(0 if run_unit_tests() else 1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
