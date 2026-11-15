# Rich Notes Bridge — External Agent (Muse) Protocol Specification

This protocol defines how an external agent (such as **Muse**) communicates with this Mac to create rich notes in Apple Notes containing **true native attachments (images, PDFs, documents)**, rich formatting (checklists, tables, blockquotes, typography), and target folder placement — and to **export** the full body of an existing note back out.

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

Muse can submit requests in any of the following 3 formats. Choose whichever is easiest for your transport channel. All three formats are for **creating** a note (the implicit `"op": "create"`); reading an existing note back out is a distinct request shape — see [§6, Export Protocol](#6-export-protocol-read-an-existing-note-read-only).

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

---

## 6. Export Protocol (Read an Existing Note, Read-Only)

Some external connectors have their own Apple Notes integration that caps a single read at 64 KiB — real notes routinely exceed that (a ~125 KB note is the motivating case for this feature). Export closes that gap using the bridge's own, already-granted Notes automation access: it reads a note's full body and writes it out as plain files in the outbox, which the consumer reads via file transfer instead of through the connector's truncating read path.

**Export is a strictly read-only operation.** The code path that handles it has no ability to create, modify, move, or delete anything in Notes.app — it only ever reads a note's `name`, `plaintext`, `body` (HTML), `container` (folder), and `attachments` properties.

### Request Format — Single JSON File Drop Only

Export is **only** supported via a single JSON file in `inbox/<request_id>.json` — there is no directory or markdown-frontmatter export format, since export never has companion files to decode.

```json
{
  "id": "export-pastebin-20261115",
  "op": "export",
  "note_id": "x-coredata://XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX/ICNote/p4304",
  "title": null
}
```

| Field | Required | Notes |
|---|---|---|
| `id` | Yes | Export request id. Used for both the output filenames and the receipt filename. |
| `op` | Yes | Must be the literal string `"export"`. (Absent or `"create"` → the normal create pipeline, unaffected by any of this. Any other value → a clean `"status": "error"` receipt — never silently treated as a create.) |
| `note_id` | One of `note_id` / `title` | The `x-coredata://...` id form already returned in every create/export receipt's `note_id` field. Takes priority over `title` when both are present. |
| `title` | One of `note_id` / `title` | Exact-match lookup, used only when `note_id` is `null`/absent. Zero matches or more than one match both error out — the bridge never guesses which note you meant. |

### Identifying the Note

- **By `note_id` (recommended, unambiguous):** looked up directly via `note id "<id>"`. A missing/deleted/invalid id produces a clean error receipt, never a crash.
- **By `title` (fallback):** an **exact** string match across every note Notes.app knows about.
  - **Zero matches** → `"status": "error"`, `"error": "No note found with title '...'"`.
  - **Exactly one match** → proceeds normally.
  - **More than one match** → `"status": "error"` and a `"candidates"` array listing every matching note's `note_id`, so the caller can re-request with an unambiguous `note_id`. The bridge never picks one for you.

### Output Files

On success, the bridge writes **three** files to `outbox/`:

| File | Contents |
|---|---|
| `<id>.body.txt` | The note's full plaintext (Notes.app's own `plaintext` property; falls back to an empty string — flagged via a log warning, never a crash — if that property is unavailable on a given note). |
| `<id>.body.html` | The note's full HTML body (Notes.app's own `body` property), verbatim. |
| `<id>.json` | The receipt (below). |

Both body files are written **directly from within the AppleScript automation itself** (not relayed through the Python process's stdout), so there is no practical size ceiling from this bridge's side — the outbox is read via the consumer's own file transfer, which the background in this spec already notes has a 50 MB limit, far above any single note.

### Success Response:
```json
{
  "id": "export-pastebin-20261115",
  "status": "success",
  "op": "export",
  "note_id": "x-coredata://XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX/ICNote/p4304",
  "title": "Pastebin Dump — 2026-11-15",
  "folder": "Notes",
  "attachments_count": 0,
  "attachments": [],
  "body_chars": 125123,
  "body_bytes": 125123,
  "body_sha256": "4ab7b36055f8bbc7d29eed2c47fe12128197755521978bf64f0a17f14d58c10d",
  "body_file": "export-pastebin-20261115.body.txt",
  "html_file": "export-pastebin-20261115.body.html",
  "processed_at": "2026-11-15T08:41:00.498848+00:00",
  "error": null
}
```

`title` and `folder` reflect the note's **current, real** values read from Notes.app at export time — not whatever (if anything) the request guessed. `body_chars`/`body_bytes`/`body_sha256` describe `<id>.body.txt` specifically: `body_chars` is the decoded character count, `body_bytes` is the UTF-8 byte length, and `body_sha256` is the SHA-256 of that file's bytes — enough for a consumer to verify the transfer landed intact without re-reading the whole file.

### Error Response (ambiguous title):
```json
{
  "id": "export-pastebin-20261115",
  "status": "error",
  "op": "export",
  "note_id": null,
  "title": null,
  "folder": null,
  "attachments_count": 0,
  "attachments": [],
  "body_chars": null,
  "body_bytes": null,
  "body_sha256": null,
  "body_file": null,
  "html_file": null,
  "processed_at": "2026-11-15T08:41:29.000000+00:00",
  "error": "2 notes match title 'Pastebin Dump' — provide note_id to disambiguate, never guessing",
  "candidates": [
    "x-coredata://XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX/ICNote/p10087",
    "x-coredata://XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX/ICNote/p10088"
  ]
}
```

The `candidates` field is **only present** on this specific error (ambiguous title match) — absent from every other response, including other error kinds.

### Error Response (note not found / deleted / locked):
```json
{
  "id": "export-pastebin-20261115",
  "status": "error",
  "op": "export",
  "note_id": "x-coredata://XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX/ICNote/p999999999",
  "title": null,
  "folder": null,
  "attachments_count": 0,
  "attachments": [],
  "body_chars": null,
  "body_bytes": null,
  "body_sha256": null,
  "body_file": null,
  "html_file": null,
  "processed_at": "2026-11-15T08:41:29.000000+00:00",
  "error": "note not found: Notes got an error: Can't get note id \"x-coredata://XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX/ICNote/p999999999\"."
}
```

### Re-Export Semantics (Not Dedup-Blocked)

**Export requests are never checked against the create-dedup cache, and never recorded into it.** Dropping the same export `id` a second time always re-reads the note fresh and overwrites `<id>.body.txt` / `<id>.body.html` / `<id>.json` with current content — correct, since the note may have been edited in Notes.app since the last export. This is the one place this bridge's two request kinds behave differently: **create** requests are idempotent-by-design (same `id` or content hash → `"status": "duplicate"`, no new note); **export** requests are intentionally **not** idempotent in that sense — same `id` → always a fresh read. Create-op dedup semantics are completely unchanged by any of this.
