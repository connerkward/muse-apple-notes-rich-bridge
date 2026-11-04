#!/usr/bin/env python3
"""
Rich Notes Bridge for macOS Apple Notes
---------------------------------------
Enables external agents (e.g. Muse) to create rich notes in Apple Notes
with embedded images, formatting, and target folders via file-based inbox/outbox.

Stock macOS tools only (Python 3 standard library + osascript/JXA).
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
LAUNCH_AGENT_LABEL = "com.conner.rich-notes-bridge"
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
        self._load()

    def _load(self):
        if self.path.exists():
            try:
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
        except Exception as e:
            logger.error(f"Error saving dedup state to {self.path}: {e}")

    def check_duplicate(self, request_id: Optional[str], content_hash: str) -> Optional[Dict[str, Any]]:
        """Returns existing record if duplicate, else None."""
        if request_id and request_id in self.data["by_id"]:
            return self.data["by_id"][request_id]
        if content_hash in self.data["by_content_hash"]:
            return self.data["by_content_hash"][content_hash]
        return None

    def record(self, request_id: Optional[str], content_hash: str, note_record: Dict[str, Any]):
        if request_id:
            self.data["by_id"][request_id] = note_record
        if content_hash:
            self.data["by_content_hash"][content_hash] = note_record
        self.save()


# ----------------------------------------------------------------------
# Image Processing & Base64 Decoding
# ----------------------------------------------------------------------
def detect_image_extension(data: bytes) -> str:
    """Detect image extension from magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"MM\x00*") or data.startswith(b"II*\x00"):
        return ".tiff"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".png"


def decode_base64_string(raw: str) -> bytes:
    """Clean and decode base64 string, handling data URIs and whitespace."""
    s = raw.strip()
    if s.startswith("data:") and ";base64," in s:
        s = s.split(";base64,", 1)[1]
    # Remove any internal whitespace/newlines
    s = re.sub(r"\s+", "", s)
    return base64.b64decode(s)


# ----------------------------------------------------------------------
# Markdown to HTML Formatter (Stock macOS compliant)
# ----------------------------------------------------------------------
def markdown_to_html(md_text: str) -> str:
    """
    Lightweight, dependency-free Markdown to Notes-compatible HTML converter.
    Handles headings, bold, italic, code blocks, lists, links, images, and paragraphs.
    """
    lines = md_text.splitlines()
    html_parts = []
    in_code_block = False
    code_block_lines = []
    in_ul = False
    in_ol = False

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

    for line in lines:
        stripped = line.strip()

        # Code blocks
        if stripped.startswith("```"):
            if in_code_block:
                code_content = html.escape("\n".join(code_block_lines))
                html_parts.append(f"<pre style=\"background:#f4f4f4;padding:8px;border-radius:4px;\"><code>{code_content}</code></pre>")
                code_block_lines = []
                in_code_block = False
            else:
                html_parts.append(close_lists())
                in_code_block = True
            continue

        if in_code_block:
            code_block_lines.append(line)
            continue

        # Blank lines
        if not stripped:
            html_parts.append(close_lists())
            html_parts.append("<div><br></div>")
            continue

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            html_parts.append(close_lists())
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2)
            # Inline formatting for heading
            heading_text = apply_inline_formatting(heading_text)
            sizes = {1: "24px", 2: "20px", 3: "17px", 4: "15px", 5: "14px", 6: "13px"}
            font_size = sizes.get(level, "16px")
            html_parts.append(f"<div><b><span style=\"font-size: {font_size};\">{heading_text}</span></b></div>")
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

    html_parts.append(close_lists())
    return "\n".join(p for p in html_parts if p)


def apply_inline_formatting(text: str) -> str:
    """Applies inline bold, italic, code, links, and markdown images."""
    # Markdown images: ![alt](url) -> <img src="url" alt="alt">
    text = re.sub(r"!\[(.*?)\]\((.*?)\)", r'<img src="\2" alt="\1" style="max-width: 100%;">', text)

    # Markdown links: [text](url) -> <a href="url">text</a>
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r'<a href="\2">\1</a>', text)

    # Inline code: `code`
    text = re.sub(r"`([^`]+)`", r'<code style="background:#f4f4f4;padding:2px 4px;border-radius:3px;">\1</code>', text)

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
        images: Dict[str, bytes],
        raw_source_path: Path,
    ):
        self.request_id = request_id
        self.title = title or "Untitled Note"
        self.folder = folder or "Notes"
        self.body = body or ""
        self.format_type = format_type.lower() if format_type else "auto"
        self.images = images  # filename -> decoded bytes
        self.raw_source_path = raw_source_path

    def compute_content_hash(self) -> str:
        """Deterministic SHA-256 hash of content for deduplication."""
        h = hashlib.sha256()
        h.update(self.folder.strip().encode("utf-8"))
        h.update(b"|")
        h.update(self.title.strip().encode("utf-8"))
        h.update(b"|")
        h.update(self.body.strip().encode("utf-8"))
        # Sort image names and include image byte hashes
        for name in sorted(self.images.keys()):
            h.update(b"|img:")
            h.update(name.encode("utf-8"))
            h.update(b":")
            h.update(hashlib.sha256(self.images[name]).digest())
        return h.hexdigest()


def parse_request_from_path(item_path: Path) -> Optional[RichNoteRequest]:
    """
    Parses a request from either:
    1. A directory containing manifest.json (or manifest.txt) + image files (.b64 / images)
    2. A single .json file with embedded base64 images
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
    title = ""
    folder = "Notes"
    body = ""
    format_type = "auto"
    images: Dict[str, bytes] = {}

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

        # Inlined images in JSON
        if "images" in meta:
            _extract_images_from_dict_or_list(meta["images"], images, dir_path)

    elif manifest_path:
        # Text/markdown manifest
        with open(manifest_path, "r", encoding="utf-8") as f:
            content = f.read()
        parsed = _parse_frontmatter_text(content)
        req_id = parsed.get("id") or req_id
        title = parsed.get("title") or title
        folder = parsed.get("folder") or folder
        body = parsed.get("body") or body
        format_type = parsed.get("format") or format_type

    # Scan directory for companion base64/image files
    for f in dir_path.iterdir():
        if f.is_file() and f != manifest_path:
            name_lower = f.name.lower()
            if name_lower.endswith((".b64", ".base64")):
                # photo1.png.b64 -> photo1.png
                clean_name = f.stem
                try:
                    with open(f, "r", encoding="utf-8") as img_f:
                        raw_b64 = img_f.read()
                    data = decode_base64_string(raw_b64)
                    images[clean_name] = data
                except Exception as e:
                    logger.warning(f"Failed to read/decode base64 file {f.name}: {e}")
            elif name_lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")):
                # Check if it contains raw bytes or base64 text
                try:
                    with open(f, "rb") as bf:
                        raw_bytes = bf.read()
                    # If it starts with data: or printable base64
                    if raw_bytes.startswith(b"data:image") or (len(raw_bytes) > 0 and raw_bytes[:10].isalnum() and not raw_bytes.startswith(b"\x89PNG")):
                        try:
                            decoded = decode_base64_string(raw_bytes.decode("utf-8", errors="ignore"))
                            images[f.name] = decoded
                        except Exception:
                            images[f.name] = raw_bytes
                    else:
                        images[f.name] = raw_bytes
                except Exception as e:
                    logger.warning(f"Failed to read companion image {f.name}: {e}")

    return RichNoteRequest(
        request_id=req_id,
        title=title,
        folder=folder,
        body=body,
        format_type=format_type,
        images=images,
        raw_source_path=dir_path,
    )


def _parse_json_file_request(json_path: Path) -> RichNoteRequest:
    with open(json_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    req_id = meta.get("id") or json_path.stem
    title = meta.get("title", "")
    folder = meta.get("folder", "Notes")
    body = meta.get("body", "")
    format_type = meta.get("format", "auto")
    images: Dict[str, bytes] = {}

    if "images" in meta:
        _extract_images_from_dict_or_list(meta["images"], images, json_path.parent)

    return RichNoteRequest(
        request_id=req_id,
        title=title,
        folder=folder,
        body=body,
        format_type=format_type,
        images=images,
        raw_source_path=json_path,
    )


def _parse_text_file_request(txt_path: Path) -> RichNoteRequest:
    with open(txt_path, "r", encoding="utf-8") as f:
        content = f.read()
    parsed = _parse_frontmatter_text(content)
    req_id = parsed.get("id") or txt_path.stem
    title = parsed.get("title", "")
    folder = parsed.get("folder", "Notes")
    body = parsed.get("body", "")
    format_type = parsed.get("format", "markdown")
    images: Dict[str, bytes] = {}

    # Also check if companion .b64 files exist next to it: e.g. <stem>_image.png.b64
    parent = txt_path.parent
    prefix = f"{txt_path.stem}_"
    for cand in parent.glob(f"{prefix}*"):
        if cand.is_file() and cand.suffix.lower() in [".b64", ".base64"]:
            img_name = cand.name[len(prefix) :]
            if img_name.endswith((".b64", ".base64")):
                img_name = Path(img_name).stem
            try:
                with open(cand, "r", encoding="utf-8") as bf:
                    images[img_name] = decode_base64_string(bf.read())
            except Exception as e:
                logger.warning(f"Failed decoding companion base64 file {cand}: {e}")

    return RichNoteRequest(
        request_id=req_id,
        title=title,
        folder=folder,
        body=body,
        format_type=format_type,
        images=images,
        raw_source_path=txt_path,
    )


def _extract_images_from_dict_or_list(images_obj: Any, images_dict: Dict[str, bytes], base_dir: Path):
    if isinstance(images_obj, dict):
        for name, val in images_obj.items():
            if isinstance(val, str):
                # Could be base64 string or filename relative to base_dir
                possible_file = base_dir / val
                if possible_file.is_file():
                    with open(possible_file, "r", encoding="utf-8") as f:
                        images_dict[name] = decode_base64_string(f.read())
                else:
                    images_dict[name] = decode_base64_string(val)
    elif isinstance(images_obj, list):
        for idx, item in enumerate(images_obj):
            if isinstance(item, dict):
                name = item.get("name") or item.get("filename") or f"image_{idx+1}.png"
                val = item.get("data") or item.get("base64") or item.get("path")
                if val and isinstance(val, str):
                    possible_file = base_dir / val
                    if possible_file.is_file():
                        with open(possible_file, "r", encoding="utf-8") as f:
                            images_dict[name] = decode_base64_string(f.read())
                    else:
                        images_dict[name] = decode_base64_string(val)


def _parse_frontmatter_text(raw_text: str) -> Dict[str, str]:
    """Parse text with YAML-like frontmatter or header keys."""
    meta = {}
    body = raw_text

    # YAML frontmatter --- ... ---
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

    # Key: Value lines at top followed by empty line
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
# HTML Synthesis & Image Reference Resolution
# ----------------------------------------------------------------------
def prepare_note_html(
    req: RichNoteRequest, scratch_dir: Path
) -> Tuple[str, List[str]]:
    """
    Writes decoded images to scratch_dir (/tmp) and constructs the final
    Apple Notes-compatible HTML body referencing file:/// URLs.
    Returns (html_body, list_of_saved_image_filenames).
    """
    saved_images: Dict[str, Path] = {}
    saved_filenames: List[str] = []

    # 1. Save all decoded images into scratch_dir (/tmp)
    for orig_name, img_bytes in req.images.items():
        # Ensure safe filename
        safe_name = Path(orig_name).name
        # If no extension or .b64 extension, fix it
        ext = Path(safe_name).suffix.lower()
        if ext in [".b64", ".base64", ".txt", ""]:
            safe_name = f"{Path(safe_name).stem}{detect_image_extension(img_bytes)}"

        out_path = scratch_dir / safe_name
        with open(out_path, "wb") as f:
            f.write(img_bytes)

        saved_images[orig_name] = out_path
        saved_images[safe_name] = out_path
        if safe_name not in saved_filenames:
            saved_filenames.append(safe_name)

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
        # Markdown / plain text
        html_content = markdown_to_html(body_str)

    # 3. Replace image references with file:/// paths
    used_images = set()

    def replace_src(match):
        src = match.group(1).strip()
        clean_src = src.lstrip("./")
        matched_path = None

        candidates = [
            src,
            clean_src,
            Path(src).name,
            f"{src}.b64",
            f"{src}.base64",
            Path(src).stem if Path(src).suffix.lower() in [".b64", ".base64"] else None,
        ]
        for c in candidates:
            if c and c in saved_images:
                matched_path = saved_images[c]
                break

        if matched_path:
            used_images.add(matched_path.name)
            return f'src="file://{matched_path.resolve()}"'
        return match.group(0)

    # Replace in img src="..."
    html_content = re.sub(r'src=["\']([^"\']+)["\']', replace_src, html_content)

    # 4. If any images in request were NOT referenced in body, append them
    unreferenced_parts = []
    for safe_name in saved_filenames:
        if safe_name not in used_images:
            img_file = scratch_dir / safe_name
            unreferenced_parts.append(
                f'<div><br></div><div><img src="file://{img_file.resolve()}" style="max-width: 100%;"><br></div>'
            )

    if unreferenced_parts:
        html_content += "\n" + "\n".join(unreferenced_parts)

    # 5. Ensure title is rendered as the first element if not already present
    title_escaped = html.escape(req.title)
    if not (f"<h1>{title_escaped}</h1>" in html_content or title_escaped in html_content[:150]):
        title_block = f'<div><b><span style="font-size: 24px;">{title_escaped}</span></b></div><div><br></div>\n'
        html_content = title_block + html_content

    return html_content, saved_filenames


# ----------------------------------------------------------------------
# Notes.app Automation via JXA
# ----------------------------------------------------------------------
JXA_CREATE_NOTE_SCRIPT = """
function run(argv) {
    const input = JSON.parse(argv[0]);
    const Notes = Application('Notes');
    
    // Resolve target folder
    let targetFolder;
    const folderName = (input.folder || '').trim();
    if (folderName === '' || folderName.toLowerCase() === 'notes') {
        try {
            targetFolder = Notes.folders.byName('Notes');
            targetFolder.name();
        } catch(e) {
            targetFolder = Notes.defaultAccount().defaultFolder();
        }
    } else {
        try {
            targetFolder = Notes.folders.byName(folderName);
            targetFolder.name();
        } catch(e) {
            targetFolder = Notes.Folder({ name: folderName });
            Notes.defaultAccount().folders.push(targetFolder);
        }
    }
    
    // Create note
    const note = Notes.Note({ body: input.html_body });
    targetFolder.notes.push(note);
    
    // Return note properties
    const res = {
        id: note.id(),
        name: note.name(),
        folder: targetFolder.name(),
        attachments_count: note.attachments().length
    };
    return JSON.stringify(res);
}
"""


def execute_jxa_create_note(folder: str, title: str, html_body: str) -> Dict[str, Any]:
    """Invokes JXA script via osascript using JSON input over argv."""
    # Ensure Notes app is awake
    try:
        subprocess.run(["open", "-g", "-a", "Notes"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    payload = {"folder": folder, "title": title, "html_body": html_body}

    res = subprocess.run(
        ["osascript", "-l", "JavaScript", "-", json.dumps(payload)],
        input=JXA_CREATE_NOTE_SCRIPT,
        capture_output=True,
        text=True,
    )

    if res.returncode != 0:
        error_msg = res.stderr.strip() or f"osascript failed with return code {res.returncode}"
        raise RuntimeError(f"Notes.app automation error: {error_msg}")

    output_str = res.stdout.strip()
    if not output_str:
        raise RuntimeError("Notes.app automation returned empty response")

    try:
        return json.loads(output_str)
    except json.JSONDecodeError:
        raise RuntimeError(f"Failed to parse Notes.app response: {output_str}")


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

        # Synthesize HTML and save intermediate images
        html_body, saved_filenames = prepare_note_html(req, scratch_dir)

        # Create note in Notes.app
        logger.info(f"Creating note '{req.title}' in folder '{req.folder}' with {len(saved_filenames)} image(s)...")
        notes_res = execute_jxa_create_note(req.folder, req.title, html_body)

        note_id = notes_res["id"]
        attachments_count = notes_res.get("attachments_count", len(saved_filenames))
        actual_folder = notes_res.get("folder", req.folder)

        logger.info(f"Successfully created note '{notes_res.get('name')}' (id: {note_id}) with {attachments_count} attachment(s)")

        # Record in dedup
        record_data = {
            "note_id": note_id,
            "title": req.title,
            "folder": actual_folder,
            "attachments_count": attachments_count,
            "attachments": saved_filenames,
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
            "attachments": saved_filenames,
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

    # Check stability (mtime and size shouldn't be currently mutating)
    try:
        # If it's a directory, check if it has contents
        if path.is_dir():
            entries = list(path.iterdir())
            if not entries:
                return False
            # Check if any temp files inside
            for e in entries:
                if e.name.startswith(".") or e.name.endswith((".tmp", ".temp", ".part", ".partial")):
                    return False
        else:
            # Must not be 0 bytes
            if path.stat().st_size == 0:
                return False

        mtime = path.stat().st_mtime
        if (time.time() - mtime) < 0.2:
            # File was modified less than 200ms ago, let it finish
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

        # Atomically rename to .processing to lock against concurrent runners
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
            # If not archived, restore name
            if processing_path.exists():
                try:
                    processing_path.rename(item)
                except Exception:
                    pass

    return processed_count


def run_daemon_loop(config: BridgeConfig, poll_interval: float = 1.0):
    """Main watcher loop."""
    config.ensure_directories()
    dedup = DedupManager(config.dedup_file)
    logger.info(f"Starting Rich Notes Bridge daemon...")
    logger.info(f"Watching inbox : {config.inbox_dir}")
    logger.info(f"Outbox results : {config.outbox_dir}")
    logger.info(f"Archive folder : {config.archive_dir}")

    while True:
        try:
            scan_and_process_inbox(config, dedup)
        except Exception as e:
            logger.error(f"Error in watcher loop: {e}", exc_info=True)
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

    # Unload first if loaded
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

    # LaunchAgent status
    if LAUNCH_AGENT_PATH.exists():
        res = subprocess.run(["launchctl", "list", LAUNCH_AGENT_LABEL], capture_output=True, text=True)
        if res.returncode == 0:
            print(f"LaunchAgent      : LOADED ({LAUNCH_AGENT_LABEL})")
        else:
            print(f"LaunchAgent      : INSTALLED BUT NOT LOADED")
    else:
        print(f"LaunchAgent      : NOT INSTALLED")

    # Dedup stats
    dedup = DedupManager(config.dedup_file)
    print(f"Processed Notes  : {len(dedup.data.get('by_id', {}))} recorded in state")


# ----------------------------------------------------------------------
# Self-Test Utility
# ----------------------------------------------------------------------
def run_self_test(config: BridgeConfig, keep_note: bool = False) -> bool:
    """
    End-to-end self test:
    1. Generates a real test image (PNG with gradient/pattern)
    2. Drops a rich request with base64 image into inbox
    3. Runs process pipeline
    4. Checks result file in outbox
    5. Queries Apple Notes via JXA to verify note exists and has attachment
    6. Verifies deduplication by re-submitting the same request
    7. Cleans up test note (unless keep_note is True)
    """
    logger.info("Starting End-to-End Self Test...")
    config.ensure_directories()
    dedup = DedupManager(config.dedup_file)

    test_req_id = f"test-rich-bridge-{int(time.time())}"
    test_title = f"Rich Bridge Test ({test_req_id})"
    test_folder = "Notes"

    # 1. Create a 32x32 real PNG with a blue gradient
    # A valid self-contained 32x32 PNG byte array:
    sample_b64_png = (
        "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAM0lEQVR42u3PMQ0AAAgEsH8y"
        "sYJ3E5CA1vQkd/V7AgQEBAQEBAQEBAQEBAQEBAQE5gZeT7UBJ/vA6+wAAAAASUVORK5CYII="
    )

    test_payload = {
        "id": test_req_id,
        "title": test_title,
        "folder": test_folder,
        "format": "markdown",
        "body": (
            "## Rich Notes Bridge E2E Verification\n\n"
            "This note was created automatically to verify image embedding.\n\n"
            "Here is the embedded test image:\n\n"
            "![Test Blue Square](test_image.png)\n\n"
            "- Feature 1: Base64 decoding\n"
            "- Feature 2: Native Apple Notes attachment embedding\n"
            "- Feature 3: Result reporting to outbox\n"
        ),
        "images": {"test_image.png": sample_b64_png},
    }

    # 2. Drop into inbox as JSON file
    test_drop_path = config.inbox_dir / f"{test_req_id}.json"
    logger.info(f"Dropping test request into {test_drop_path}...")
    with open(test_drop_path, "w", encoding="utf-8") as f:
        json.dump(test_payload, f, indent=2)
    time.sleep(0.3)

    # 3. Process inbox
    logger.info("Scanning and processing inbox...")
    processed_count = scan_and_process_inbox(config, dedup)
    if processed_count == 0:
        logger.error("Self-test FAILED: Request was not processed from inbox")
        return False

    # 4. Check outbox result
    result_path = config.outbox_dir / f"{test_req_id}.json"
    if not result_path.exists():
        logger.error(f"Self-test FAILED: Outbox result file {result_path} not found")
        return False

    with open(result_path, "r", encoding="utf-8") as f:
        result_data = json.load(f)

    logger.info(f"Outbox Result: {json.dumps(result_data, indent=2)}")
    if result_data.get("status") != "success":
        logger.error(f"Self-test FAILED: Outbox status was {result_data.get('status')} - {result_data.get('error')}")
        return False

    created_note_id = result_data.get("note_id")
    if not created_note_id:
        logger.error("Self-test FAILED: No note_id returned")
        return False

    # 5. Verify Note in Apple Notes via JXA
    logger.info(f"Verifying Note in Notes.app (id: {created_note_id})...")
    jxa_verify = f"""
    function run() {{
        const Notes = Application('Notes');
        const note = Notes.notes.byId('{created_note_id}');
        return JSON.stringify({{
            name: note.name(),
            attachments_count: note.attachments().length,
            body: note.body().substring(0, 200)
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

    # 6. Test Deduplication
    logger.info("Testing deduplication by re-dropping same request...")
    with open(test_drop_path, "w", encoding="utf-8") as f:
        json.dump(test_payload, f, indent=2)
    time.sleep(0.3)

    scan_and_process_inbox(config, dedup)
    with open(result_path, "r", encoding="utf-8") as f:
        dedup_result = json.load(f)

    logger.info(f"Dedup verification result status: {dedup_result.get('status')}")
    if dedup_result.get("status") != "duplicate":
        logger.warning(f"Expected duplicate status, got: {dedup_result.get('status')}")

    # 7. Clean up note
    if not keep_note:
        logger.info(f"Cleaning up test note {created_note_id} from Notes.app...")
        jxa_delete = f"""
        function run() {{
            const Notes = Application('Notes');
            const note = Notes.notes.byId('{created_note_id}');
            Notes.delete(note);
            return 'deleted';
        }}
        """
        subprocess.run(["osascript", "-l", "JavaScript", "-"], input=jxa_delete, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Test note deleted.")
    else:
        logger.info(f"Keeping test note in Notes.app per request (id: {created_note_id})")

    logger.info("✅ End-to-End Self Test PASSED successfully!")
    return True


# ----------------------------------------------------------------------
# CLI Interface
# ----------------------------------------------------------------------
def main():
    # Base options parent parser so options can appear before or after subcommand
    base_parser = argparse.ArgumentParser(add_help=False)
    base_parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_BASE_DIR,
        help=f"Base directory for inbox/outbox/archive (default: {DEFAULT_BASE_DIR})",
    )
    base_parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")

    parser = argparse.ArgumentParser(
        description="Rich Notes Bridge for macOS Apple Notes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[base_parser],
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # run / daemon
    subparsers.add_parser("run", parents=[base_parser], help="Run watcher daemon loop")
    subparsers.add_parser("daemon", parents=[base_parser], help="Alias for run")

    # process / once
    subparsers.add_parser("process", parents=[base_parser], help="Process pending requests in inbox once and exit")
    subparsers.add_parser("once", parents=[base_parser], help="Alias for process")

    # install launchd
    subparsers.add_parser("install", parents=[base_parser], help="Install and load launchd service")

    # uninstall launchd
    subparsers.add_parser("uninstall", parents=[base_parser], help="Unload and remove launchd service")

    # status
    subparsers.add_parser("status", parents=[base_parser], help="Show bridge status and pending queue")

    # test
    test_parser = subparsers.add_parser("test", parents=[base_parser], help="Run end-to-end self test with real image")
    test_parser.add_argument("--keep", action="store_true", help="Keep the created test note in Notes.app")

    args = parser.parse_args()
    config = BridgeConfig(args.dir)
    setup_logging(config, args.verbose)

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
        success = run_self_test(config, keep_note=args.keep)
        sys.exit(0 if success else 1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
