---
name: quick-search
description: Run fast, ripgrep-powered code/text searches and file listings by writing a small throwaway Python script that calls the `ripgrep-rs` package. Use when the user wants to search a large codebase, list files (respecting .gitignore), find regex patterns, count/aggregate matches, or do any structured search that goes beyond a single Grep/Glob call — especially when results need filtering, grouping, ranking, or post-processing in Python. Triggers: "quick search", "find every", "list all files", "ripgrep", "where is X used", "count occurrences", "find files modified after", "search and group by", "find pattern across repo".
---

# quick-search

A skill for performing fast, programmatic searches over the filesystem by **dynamically writing a temporary Python script** that uses the [`ripgrep-rs`](https://pypi.org/project/ripgrep-rs/) package. There is no pre-built script — you author one inline for each task and discard it after.

`ripgrep-rs` is a PyO3 binding that reimplements ripgrep's core in Rust and exposes it directly to Python (no shelling out). It is fast, respects `.gitignore`, and returns structured Python objects — so it's ideal when you need to combine search results with Python logic (filtering, grouping, sorting, joining with metadata).

## When to use this skill

Use `quick-search` when:

- The query benefits from **post-processing** — counting per-file, grouping by directory, joining match data with file metadata, deduping, ranking.
- You need **structured match data** (line numbers, byte offsets, submatches) that's awkward to parse from `rg` text output.
- You need **file listings with metadata** (size, mtime, permissions) in one pass.
- The search is large enough that repeated `Grep`/`Glob` calls would be slow or noisy.

For a single regex over a small set of files, prefer the built-in `Grep` tool — don't reach for this skill.

## Workflow

1. **Always write a temporary script file** — use the `Write` tool to put a small, task-specific Python file at `/tmp/quick_search_<short-name>.py`. Do **not** use `python3 -c "..."` inline one-liners; a real file is easier to read, edit, and re-run as you iterate.

2. **Run it via the project's environment**, e.g. `uv run python /tmp/quick_search_<short-name>.py` (or whichever interpreter the project uses). The `ripgrep_rs` package is already provisioned there — do not try to install it. If the import fails, ask the user how to invoke the project Python rather than installing.

3. **Iterate** — refine the script if the first pass returned too much/too little. Don't try to make a one-size-fits-all script; one script per question.

4. **Clean up** when done: `rm /tmp/quick_search_*.py` (optional; `/tmp` is ephemeral).

## API cheat sheet

```python
from ripgrep_rs import (
    search,             # raw formatted string output (like `rg`)
    search_structured,  # list[SearchMatch] — preferred for programmatic use
    files,              # list[str] of paths, respects .gitignore
    files_with_info,    # dict[str, FileInfo] with size/mtime/mode/etc.
)
```

### `search_structured(patterns, paths=None, globs=None, ...)`

Returns `list[SearchMatch]`. Each `SearchMatch` has:

- `path: str`
- `line_number: int` (1-based)
- `absolute_offset: int` (bytes from file start)
- `line_text: str`
- `submatches: list[SearchSubmatch]` where each has `text: str`, `start: int`, `end: int` (byte offsets within the line)

Common kwargs: `max_count` (per-file cap), `max_total` (global cap), `case_sensitive`, `smart_case`, `multiline`, `no_ignore`, `hidden`.

### `search(patterns, ...)`

Same filtering kwargs plus formatting kwargs: `heading`, `line_number`, `before_context`, `after_context`, `json`, `sort`. Returns a list of strings — use only when you want ripgrep's text output verbatim.

### `files(paths=None, globs=None, ...)`

Returns `list[str]`. Kwargs: `max_count`, `max_depth`, `hidden`, `no_ignore`, `include_dirs`, `absolute`, `relative_to`, `sort`.

### `files_with_info(...)`

Same kwargs as `files`. Returns `dict[str, FileInfo]`. `FileInfo` fields: `name`, `size`, `type` (`"file"`/`"directory"`), `created`, `mtime`, `islink`, `mode`, `uid`, `gid`, `ino`, `nlink`.

### Sorting

```python
from ripgrep_rs import PySortMode, PySortModeKind
sort = PySortMode(kind=PySortModeKind.LastModified, reverse=True)
# kinds: Path, LastModified, LastAccessed, Created
```

## Script templates

Adapt these — don't copy verbatim. The point of the skill is that you write the script that fits the question.

**Template A — find pattern, group results by file:**

```python
from collections import Counter
from ripgrep_rs import search_structured

matches = search_structured(
    patterns=[r"TODO\(.*?\)"],
    paths=["."],
    globs=["*.py", "*.ts"],
    smart_case=True,
)
per_file = Counter(m.path for m in matches)
for path, n in per_file.most_common(20):
    print(f"{n:4d}  {path}")
print(f"\ntotal: {len(matches)} matches in {len(per_file)} files")
```

**Template B — list recently modified source files:**

```python
from ripgrep_rs import files_with_info, PySortMode, PySortModeKind

info = files_with_info(
    paths=["."],
    globs=["*.py"],
    sort=PySortMode(kind=PySortModeKind.LastModified, reverse=True),
    max_count=25,
)
for path, fi in info.items():
    print(f"{fi.mtime:.0f}  {fi.size:>9}  {path}")
```

**Template C — print match with byte offset and submatch positions:**

```python
from ripgrep_rs import search_structured

for m in search_structured(patterns=[r"def\s+(\w+)"], paths=["src"], globs=["*.py"]):
    for sm in m.submatches:
        print(f"{m.path}:{m.line_number}:{sm.start}-{sm.end}  {sm.text}")
```

## Guidelines

- **One script, one question.** Don't build a CLI. Hard-code the parameters for the current task.
- **Print, don't return.** The agent reads stdout; structured serialisation (JSON) is fine when results feed another step.
- **Bound the output.** Use `max_count` / `max_total` and explicit slicing when scanning huge trees so the agent's context doesn't fill with noise.
- **Respect `.gitignore` by default.** Only pass `no_ignore=True` if the user explicitly wants vendored / ignored files.
- **Prefer `search_structured` over `search`** for anything beyond display — parsing `rg`'s text output is error-prone.
- **Python compatibility:** the deployed runtime here may be pre-3.12 — keep scripts free of PEP 701 f-string features and other 3.12+ syntax.
