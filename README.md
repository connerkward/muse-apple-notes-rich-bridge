# muse-apple-notes-rich-bridge

A local, file-based rich bridge for Apple Notes on macOS.

Enables external agents (such as **Muse**) operating over paired-device text channels to create notes in Apple Notes with **real, native embedded images**, rich Markdown/HTML formatting, target folder placement, and result reporting.

---

## Why This Exists

External agents connected over paired channels often have text-only Apple Notes integrations. macOS Notes automation does not support binary image transfer over simple text AppleScript invocations.

This bridge solves the limitation by implementing a local, watched file inbox/outbox protocol:
1. The external agent drops a manifest and base64-encoded image files into an inbox folder.
2. A background watcher picks up the request, decodes images to temporary files in `/tmp`, and invokes native Apple Notes automation via JavaScript for Automation (JXA).
3. Notes.app imports the images into its local CoreData store as native attachments.
4. The temporary scratch files in `/tmp` are immediately removed.
5. A result file with the new note's CoreData ID is written back to the outbox for the external agent to read.
6. The request is moved to an archive directory to keep the inbox empty and clean.

**Design Constraints:**
- **Stock macOS tools only**: Uses Python 3 standard library and system `osascript`/JXA. Zero third-party dependencies or pip packages.
- **Zero network / completely local**: No servers, no network ports, nothing exposed.
- **Deduplication**: Automatically deduplicates by request ID and SHA-256 content hash, preventing duplicate notes if a request is received multiple times.
- **Intermediate file hygiene**: All temporary files are stored strictly under `/tmp` and cleaned up upon note creation.

---

## Directory Layout

By default, the bridge manages:

```
~/.rich-notes-bridge/
├── inbox/     # External agent drops requests here
├── outbox/    # Bridge writes completion results here
├── archive/   # Processed requests are archived here
├── state/     # Persistent deduplication tracking (dedup.json)
└── logs/      # Service logs (launchd.log, bridge.log)
```

*(Override the base directory anytime via `--dir <path>` or the `RICH_NOTES_BRIDGE_DIR` environment variable).*

---

## Quick Start & Installation

### 1. Run Self-Test
Verify that your Mac's Notes automation is ready and create an end-to-end test note with an embedded image:

```bash
./rich_notes_bridge.py test
```

### 2. Install as a Background Service (launchd)
Install and activate the LaunchAgent so it runs automatically in your user session:

```bash
./rich_notes_bridge.py install
```

Check service status:
```bash
./rich_notes_bridge.py status
```

To stop or uninstall the LaunchAgent:
```bash
./rich_notes_bridge.py uninstall
```

### 3. Run Manually (Alternative to launchd)
To run the watcher in the foreground:
```bash
./rich_notes_bridge.py run
```

To process the inbox once and exit:
```bash
./rich_notes_bridge.py process
```

---

## External Agent Protocol (Muse)

For the complete specification, see [`PROTOCOL.md`](PROTOCOL.md).

### Single JSON File Drop

Write `~/.rich-notes-bridge/inbox/<request_id>.json`:

```json
{
  "id": "req-20261103-01",
  "title": "System Architecture & Sketches",
  "folder": "Notes",
  "format": "markdown",
  "body": "## System Overview\n\nHere is the whiteboard architecture:\n\n![Whiteboard](whiteboard.png)\n\nKey discussion points:\n- Fast local IPC\n- Native attachment persistence",
  "images": {
    "whiteboard.png": "iVBORw0KGgoAAAANSUhEUgAA..."
  }
}
```

### Multi-File Directory Drop

Create `~/.rich-notes-bridge/inbox/<request_id>/`:
- `manifest.json`: Title, body, target folder.
- `whiteboard.png.b64`: Base64 text file of the image.

### Reading Results (Outbox)

The bridge writes `~/.rich-notes-bridge/outbox/<request_id>.json`:

```json
{
  "id": "req-20261103-01",
  "status": "success",
  "note_id": "x-coredata://DD081B86-368A-4CBC-A6F5-CD822A89C8EE/ICNote/p9381",
  "title": "System Architecture & Sketches",
  "folder": "Notes",
  "attachments_count": 1,
  "attachments": [
    "whiteboard.png"
  ],
  "processed_at": "2026-11-04T03:06:57.543874+00:00",
  "error": null
}
```

If the same request is dropped again, the bridge responds with `"status": "duplicate"` and returns the existing `note_id` without creating duplicates in Notes.app.

---

## CLI Reference

```
usage: rich_notes_bridge.py [-h] [--dir DIR] [-v] {run,daemon,process,once,install,uninstall,status,test} ...

commands:
  run (daemon)       Run the watcher loop in the foreground
  process (once)     Process pending inbox requests once and exit
  install            Install and load the launchd background agent
  uninstall          Unload and remove the launchd background agent
  status             Show bridge status, queue counts, and launchd health
  test               Run end-to-end self test with an actual image
```

---

## Security & Privacy

- **100% Offline**: Zero external network access or remote telemetry.
- **Stock Tools Only**: Python standard library + macOS `osascript`.
- **Restricted Permissions**: Default directory permissions created with `0700`.
- **Atomic File Operations**: Uses rename-locking to prevent partial reads or concurrency races.
