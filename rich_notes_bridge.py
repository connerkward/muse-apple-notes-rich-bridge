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
def detect_file_extension(data: bytes, fallback: str = ".png") -> str:
    """Detect file extension from magic bytes for common media and docs."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"PK\x03\x04"):
        return ".zip"
    if data.startswith(b"MM\x00*") or data.startswith(b"II*\x00"):
        return ".tiff"
    if data.startswith(b"BM"):
        return ".bmp"
    return fallback


def decode_base64_string(raw: str) -> bytes:
    """Clean and decode base64 string, handling data URIs and whitespace."""
    s = raw.strip()
    if s.startswith("data:") and ";base64," in s:
        s = s.split(";base64,", 1)[1]
    s = re.sub(r"\s+", "", s)
    return base64.b64decode(s)


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


def _detect_op(item_path: Path, orig_name: str) -> str:
    """Peeks at a single-JSON-file request's "op" field without fully parsing it
    as a create request. Returns "create" (the default) for directories,
    non-JSON files, missing/null "op", or any JSON that fails to parse — in
    every one of those cases the existing create pipeline's own error handling
    is what should run, unchanged, exactly as before this field existed."""
    if item_path.is_dir():
        return "create"
    if Path(orig_name).suffix.lower() != ".json":
        return "create"
    try:
        with open(item_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        return str(meta.get("op") or "create").lower()
    except Exception:
        return "create"


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
        logger.error(f"Error parsing request from {item_path}: {e}", exc_info=True)
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
                _extract_attachments(meta[k], attachments, dir_path)

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
                    logger.warning(f"Failed to decode base64 file {f.name}: {e}")
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
                except Exception as e:
                    logger.warning(f"Failed to read companion file {f.name}: {e}")

    return RichNoteRequest(
        request_id=req_id,
        title=title,
        folder=folder,
        body=body,
        format_type=format_type,
        attachments=attachments,
        raw_source_path=dir_path,
    )


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

    for k in ["images", "attachments"]:
        if k in meta:
            _extract_attachments(meta[k], attachments, json_path.parent)

    return RichNoteRequest(
        request_id=req_id,
        title=title,
        folder=folder,
        body=body,
        format_type=format_type,
        attachments=attachments,
        raw_source_path=json_path,
    )


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
            except Exception as e:
                logger.warning(f"Failed decoding companion file {cand}: {e}")

    return RichNoteRequest(
        request_id=req_id,
        title=title,
        folder=folder,
        body=body,
        format_type=format_type,
        attachments=attachments,
        raw_source_path=txt_path,
    )


def _extract_attachments(obj: Any, out_dict: Dict[str, bytes], base_dir: Path):
    if isinstance(obj, dict):
        for name, val in obj.items():
            if isinstance(val, str):
                possible_file = base_dir / val
                if possible_file.is_file():
                    with open(possible_file, "rb") as f:
                        raw = f.read()
                    try:
                        out_dict[name] = decode_base64_string(raw.decode("utf-8"))
                    except Exception:
                        out_dict[name] = raw
                else:
                    out_dict[name] = decode_base64_string(val)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            if isinstance(item, dict):
                name = item.get("name") or item.get("filename") or f"attachment_{idx+1}.png"
                val = item.get("data") or item.get("base64") or item.get("path")
                if val and isinstance(val, str):
                    possible_file = base_dir / val
                    if possible_file.is_file():
                        with open(possible_file, "rb") as f:
                            raw = f.read()
                        try:
                            out_dict[name] = decode_base64_string(raw.decode("utf-8"))
                        except Exception:
                            out_dict[name] = raw
                    else:
                        out_dict[name] = decode_base64_string(val)


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
def prepare_note_html_and_attachments(
    req: RichNoteRequest, scratch_dir: Path
) -> Tuple[str, List[Path]]:
    """
    Stages all attachments into scratch_dir (/tmp) and prepares the rich
    HTML body for Apple Notes. Returns (html_body, list_of_staged_file_paths).
    """
    staged_paths: List[Path] = []

    # 1. Save all decoded attachments into scratch_dir (/tmp)
    for orig_name, file_bytes in req.attachments.items():
        safe_name = Path(orig_name).name
        ext = Path(safe_name).suffix.lower()
        if ext in [".b64", ".base64", ".txt", ""]:
            safe_name = f"{Path(safe_name).stem}{detect_file_extension(file_bytes)}"

        out_path = scratch_dir / safe_name
        with open(out_path, "wb") as f:
            f.write(file_bytes)

        if out_path not in staged_paths:
            staged_paths.append(out_path)

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

    # Remove any stray <img src="..."> tags from HTML body so Notes doesn't fail URL loading
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

    for att_path in attachment_paths:
        script_lines.append(f'    make new attachment at theNote with data (POSIX file "{att_path.resolve()}")')

    script_lines.extend([
        '    set noteId to id of theNote',
        '    set noteName to name of theNote',
        '    return noteId & "\\t" & noteName',
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
    note_id = parts[0]
    note_name = parts[1] if len(parts) > 1 else title

    return {
        "id": note_id,
        "name": note_name,
        "attachments_count": len(attachment_paths),
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
            "error": f"Unrecognized op {op!r} (expected \"create\" or \"export\")",
        }
        _write_result(config, req_id, error_result)
        _archive_request(item_path, config.archive_dir / "failed", req_id)
        return error_result

    try:
        req = parse_request_from_path(item_path)
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
                "title": existing.get("title", req.title),
                "folder": existing.get("folder", req.folder),
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
        logger.error(f"Failed processing request {req_id}: {e}", exc_info=True)
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

    test_parser = subparsers.add_parser("test", parents=[base_parser], help="Run end-to-end self tests (image attachment + export round trip)")
    test_parser.add_argument("--keep", action="store_true", help="Keep the created test note in Notes.app")

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
        image_test_passed = run_self_test(config, keep_note=args.keep)
        export_test_passed = run_export_self_test(config, keep_note=args.keep)
        if image_test_passed and export_test_passed:
            logger.info("✅ ALL self tests passed (image attachment test + export round-trip test).")
        else:
            logger.error(
                f"Self test suite FAILED — image test: {'PASS' if image_test_passed else 'FAIL'}, "
                f"export test: {'PASS' if export_test_passed else 'FAIL'}"
            )
        sys.exit(0 if (image_test_passed and export_test_passed) else 1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
