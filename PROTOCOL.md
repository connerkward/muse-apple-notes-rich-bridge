# Rich Notes Bridge — External Agent (Muse) Protocol Specification

This protocol defines how an external agent (such as **Muse**) communicates with this Mac to create rich notes in Apple Notes containing **true native attachments (images, PDFs, documents)**, rich formatting (checklists, tables, blockquotes, typography), and target folder placement.

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

### Format A: Single JSON File Drop (Recommended)

Write a single JSON file directly to `inbox/`:
`inbox/<request_id>.json`

```json
{
  "id": "meeting-20261103",
  "title": "Architecture Review & Sketches",
  "folder": "Notes",
  "format": "markdown",
  "body": "## System Overview\n\n### Action Items\n- [x] Verified native Apple Notes attachment handler\n- [x] Added table and checklist formatting support\n- [ ] Ready for Muse paired device drop\n\n### Metrics Table\n| Component | Mechanism | Status |\n|---|---|---|\n| Attachment | `make new attachment with data` | VERIFIED |\n| Quick Look | Native CoreData Store | SUPPORTED |\n\n> Summary: All images and attachments are imported as true native attachments.\n",
  "attachments": {
    "whiteboard.jpg": "<base64 encoded string or data URI>"
  }
}
```

*Note: The keys `"attachments"` and `"images"` are interchangeable.*

---

### Format B: Multi-File Directory Drop (Recommended for multiple files)

Create a subdirectory in `inbox/`:
`inbox/<request_id>/`

Inside the directory, place:
1. `manifest.json` (or `manifest.txt`):
   ```json
   {
     "id": "meeting-20261103",
     "title": "Architecture Review & Sketches",
     "folder": "Notes",
     "format": "markdown",
     "body": "## System Overview\n\nSee attached diagram and documentation below."
   }
   ```
2. Any companion files:
   - `diagram.png.b64` (base64 text file)
   - `spec.pdf.b64` (base64 text file of PDF or document)
   - Raw files (e.g. `diagram.png`, `spec.pdf`) are also supported directly.

The bridge decodes all companion files and embeds them as native attachments in the note.

---

### Format C: Markdown Frontmatter File Drop

Write a single `.md` or `.txt` file to `inbox/<request_id>.md`:

```markdown
---
id: quick-note-01
title: Quick Scratchpad
folder: Notes
---
Here are the meeting notes:
- [x] Initial design
- [ ] Next steps
```

Companion base64 files can be placed alongside as `quick-note-01_sketch.png.b64`.

---

## 3. Rich Formatting Capabilities

The bridge converts standard Markdown into native Apple Notes HTML styling:

| Markdown Syntax | Notes.app Rendering |
|---|---|
| `# Heading 1` | 24px Bold Title |
| `## Heading 2` | 20px Bold Header |
| `### Heading 3` | 17px Bold Subheader |
| `- [ ] Task` | `☐ Task` (Checklist item) |
| `- [x] Done` | `☑ Done` (Completed checklist item) |
| `\| A \| B \|` | Formatted `<table>` with shaded header and border |
| `> Quote` | Styled blockquote with left accent border |
| `` `code` `` | Monospace inline code tag |
| ```` ```code``` ```` | Syntax-highlighted monospace pre block |
| `**bold**` / `*italic*` / `~~strike~~` | Rich typography |

---

## 4. Best Practices for Muse File Writing

To avoid partial-write race conditions when transferring files over paired channels:
1. Write the request to a temporary file (e.g. `inbox/<request_id>.tmp`).
2. Atomically rename it to `inbox/<request_id>.json` or `inbox/<request_id>/` once writing is complete.
3. The bridge ignores files ending in `.tmp`, `.partial`, `.part`, or starting with `.`.

---

## 5. Result Protocol (Outbox)

Once processed, the bridge immediately writes an execution receipt to:
`outbox/<request_id>.json`

### Success Response:
```json
{
  "id": "meeting-20261103",
  "status": "success",
  "note_id": "x-coredata://XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX/ICNote/p9450",
  "title": "Architecture Review & Sketches",
  "folder": "Notes",
  "attachments_count": 1,
  "attachments": [
    "whiteboard.jpg"
  ],
  "processed_at": "2026-11-04T03:31:31.874177+00:00",
  "error": null
}
```

### Duplicate Response (Idempotent):
If the same request ID or identical content is received again:
```json
{
  "id": "meeting-20261103",
  "status": "duplicate",
  "note_id": "x-coredata://XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX/ICNote/p9450",
  "title": "Architecture Review & Sketches",
  "folder": "Notes",
  "attachments_count": 1,
  "attachments": [
    "whiteboard.jpg"
  ],
  "processed_at": "2026-11-04T03:31:36.123456+00:00",
  "message": "Note was already created with this request ID or content hash",
  "error": null
}
```

### Error Response:
```json
{
  "id": "meeting-20261103",
  "status": "error",
  "note_id": null,
  "title": "Architecture Review & Sketches",
  "folder": "Notes",
  "attachments_count": 0,
  "attachments": [],
  "processed_at": "2026-11-04T03:31:31.874177+00:00",
  "error": "Failed to decode base64 for whiteboard.jpg: Incorrect padding"
}
```
