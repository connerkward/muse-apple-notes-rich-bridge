# Rich Notes Bridge — External Agent (Muse) Protocol Specification

This protocol defines how an external agent (such as **Muse**) communicates with this Mac to create rich notes in Apple Notes containing embedded images, formatting, and target folders.

---

## 1. Directory Structure

By default, the bridge operates at `~/.rich-notes-bridge/`:

```
~/.rich-notes-bridge/
├── inbox/     <-- Muse drops requests here
├── outbox/    <-- Muse reads results here
├── archive/   <-- Processed requests are moved here automatically
├── state/     <-- Deduplication cache (dedup.json)
└── logs/      <-- Daemon and LaunchAgent logs
```

*(The root path can be customized via the `RICH_NOTES_BRIDGE_DIR` environment variable or `--dir <path>`)*.

---

## 2. Request Protocols (Drop Formats)

Muse can submit requests in any of the following 3 formats. Choose whichever is easiest for your transport channel.

### Format A: Multi-File Directory Drop (Recommended for multiple images)

Create a subdirectory in `inbox/`:
`inbox/<request_id>/`

Inside the directory, place:
1. `manifest.json` (or `manifest.txt`)
2. Base64 encoded image files (e.g. `diagram.png.b64`, `whiteboard.jpg.b64`)

#### `manifest.json` schema:
```json
{
  "id": "meeting-2026-11-03",
  "title": "Architecture Review & Sketches",
  "folder": "Notes",
  "format": "markdown",
  "body": "## Architecture Overview\n\nHere is the system diagram:\n\n![System Diagram](diagram.png)\n\nKey takeaways:\n- Decoupled inbox/outbox\n- Real native Apple Notes attachments"
}
```

#### Image companion files:
- Place image files directly alongside `manifest.json`:
  - `diagram.png.b64` (contains standard base64 string or data URI `data:image/png;base64,...`)
  - The bridge automatically matches `diagram.png` in the markdown/HTML body to `diagram.png.b64`.
  - Any images in the folder that are *not* explicitly placed via markdown/HTML image tags are automatically appended to the end of the note so nothing is lost.

---

### Format B: Single JSON File Drop (Ideal for single-file writes)

Write a single JSON file directly to `inbox/`:
`inbox/<request_id>.json`

```json
{
  "id": "doc-sync-101",
  "title": "Brainstorming Session",
  "folder": "Notes",
  "format": "markdown",
  "body": "### Brainstorm Summary\n\nAttached screenshot:\n\n![Screenshot](whiteboard.png)",
  "images": {
    "whiteboard.png": "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9h..."
  }
}
```

*(The `"images"` object maps filename to base64 string or data URI)*.

---

### Format C: Markdown Frontmatter File Drop

Write a single `.md` or `.txt` file to `inbox/<request_id>.md`:

```markdown
---
id: quick-note-01
title: Quick Scratchpad
folder: Notes
---
Here is the inline image:
![Sketch](sketch.png)
```

Companion base64 files can be placed as `quick-note-01_sketch.png.b64`.

---

## 3. Best Practices for Muse File Writing

To avoid partial-write race conditions when transferring files:
1. Write the request to a temporary file (e.g. `inbox/<request_id>.tmp`).
2. Atomically rename it to `inbox/<request_id>.json` or `inbox/<request_id>/` once writing is complete.
3. The bridge ignores files ending in `.tmp`, `.partial`, `.part`, or starting with `.`.

---

## 4. Result Protocol (Outbox)

Once processed, the bridge immediately writes a result file to:
`outbox/<request_id>.json`

### Success Response:
```json
{
  "id": "meeting-2026-11-03",
  "status": "success",
  "note_id": "x-coredata://DD081B86-368A-4CBC-A6F5-CD822A89C8EE/ICNote/p9381",
  "title": "Architecture Review & Sketches",
  "folder": "Notes",
  "attachments_count": 1,
  "attachments": [
    "diagram.png"
  ],
  "processed_at": "2026-11-04T03:06:57.543874+00:00",
  "error": null
}
```

### Duplicate Response (Idempotent):
If the same `id` or identical content is received again:
```json
{
  "id": "meeting-2026-11-03",
  "status": "duplicate",
  "note_id": "x-coredata://DD081B86-368A-4CBC-A6F5-CD822A89C8EE/ICNote/p9381",
  "title": "Architecture Review & Sketches",
  "folder": "Notes",
  "attachments_count": 1,
  "attachments": [
    "diagram.png"
  ],
  "processed_at": "2026-11-04T03:07:05.123456+00:00",
  "message": "Note was already created with this request ID or content hash",
  "error": null
}
```

### Error Response:
```json
{
  "id": "meeting-2026-11-03",
  "status": "error",
  "note_id": null,
  "title": "Architecture Review & Sketches",
  "folder": "Notes",
  "attachments_count": 0,
  "attachments": [],
  "processed_at": "2026-11-04T03:06:57.543874+00:00",
  "error": "Failed to decode base64 for diagram.png: Incorrect padding"
}
```

---

## 5. Summary of Supported Formats

| Feature | Supported Values | Notes |
|---|---|---|
| **Body format** | `markdown`, `html`, `text` (or `auto`) | Auto-detects HTML tags if omitted |
| **Image formats** | PNG, JPEG, GIF, WebP, TIFF, BMP | Auto-detects format from magic bytes |
| **Folder** | Any folder name | Automatically creates folder if it doesn't exist; defaults to `"Notes"` |
| **Intermediate files** | Cleaned immediately | All decoded scratch files are stored in `/tmp` and removed |
| **Deduplication** | By `id` and SHA-256 content hash | Safe to retry without creating duplicate notes |
