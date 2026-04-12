---
name: notes
description: Personal notes and scratchpad manager. Create, search, edit, and organize notes with tags and pinning. Use when the user wants to save quick notes, links, snippets, or any text for later retrieval.
---

# Notes Manager

**Script:** `uv run python scripts/notes.py <command> [options]`

## Commands

| Command  | Description                                      |
| -------- | ------------------------------------------------ |
| `add`    | Create a new note                                |
| `list`   | List all notes (optionally filter by tag)        |
| `get`    | Retrieve a specific note by ID                   |
| `search` | Full-text search across titles, content, and tags |
| `edit`   | Update an existing note                          |
| `delete` | Remove a note by ID                              |
| `tags`   | Show all tags with note counts                   |
| `export` | Export notes as Markdown or JSON                 |
| `stats`  | Show summary statistics                          |

## Examples

```bash
# Add a note
uv run python scripts/notes.py add --title "API endpoint" --content "POST /api/v1/users" --tags "api,reference"

# Add a pinned note
uv run python scripts/notes.py add --title "Important" --content "Remember to update DNS" --pin

# List all notes
uv run python scripts/notes.py list

# Filter by tag
uv run python scripts/notes.py list --tag "reference"

# Search notes
uv run python scripts/notes.py search --query "DNS"

# Edit a note
uv run python scripts/notes.py edit --id abc12345 --content "Updated content" --tags "new,tags"

# Delete a note
uv run python scripts/notes.py delete --id abc12345

# Export as markdown
uv run python scripts/notes.py export --format md
```

## Data Storage

Notes are stored in `/agent/memory/notes.json` with this schema:

```json
{
  "id": "8-char hex",
  "title": "Note title",
  "content": "Note body text",
  "tags": ["tag1", "tag2"],
  "pinned": false,
  "created_at": "ISO 8601",
  "updated_at": "ISO 8601"
}
```

## Portal Integration

Notes are also accessible via the portal's **Notes** tab (Core section), which provides:
- Create, edit, and delete notes via UI
- Search and filter by tags
- Pin/unpin notes
- Inline content preview