"""
memoria-mcp — Persistent memory and context awareness for Claude.

An MCP server that gives any Claude instance structured access to a shared
memory system: living context, deadlines, projects, session logs, and
proactive suggestions — across sessions, across instances.

Born from the "agency experiment" (ágencia kísérlet) by @leszini,
exploring what happens when Claude gets persistent memory and the tools
to maintain it.

Features:
- Living context model (who you are, what you're working on, what's urgent)
- Session summaries for cross-session continuity
- Structured logging with chronological tracking
- Reflection engine: synthesize everything known about a topic
- Context staleness detection and update suggestions
- Automatic archival of old sessions and logs (configurable retention)
- Per-file archival of tools and projects with a dated folder and per-subdir digest
- Full-text search across all memory files

Usage:
    Set MEMORIA_ROOT to your memory directory and run:
        python server.py

Configuration:
    Environment variables:
        MEMORIA_ROOT        — Path to the memory data directory (required)
        MEMORIA_CONTEXT_FILE — Name of the living context file (default: context.md)
        MEMORIA_STATE_FILE   — Name of the state file (default: _state.md)
        MEMORIA_RETENTION_DAYS — Days before sessions/logs are archived (default: 14)

    Or place a memoria.config.json in MEMORIA_ROOT (see README for format).
"""

import os
import re
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

from mcp.server.fastmcp import FastMCP

# ============================================================
# Configuration
# ============================================================

MEMORIA_ROOT = os.environ.get("MEMORIA_ROOT", "")

if not MEMORIA_ROOT:
    raise ValueError(
        "MEMORIA_ROOT environment variable is required. "
        "Set it to the path of your memory data directory."
    )

# Load config from file if present, then overlay env vars
_config = {}
_config_path = os.path.join(MEMORIA_ROOT, "memoria.config.json")
if os.path.isfile(_config_path):
    with open(_config_path, "r", encoding="utf-8") as f:
        _config = json.load(f)


def _cfg(key: str, env_key: str, default: str) -> str:
    """Get config value: env var > config file > default."""
    return os.environ.get(env_key, _config.get(key, default))


# Directory names (configurable for localization)
DIR_SESSIONS = _cfg("dir_sessions", "MEMORIA_DIR_SESSIONS", "sessions")
DIR_LOGS = _cfg("dir_logs", "MEMORIA_DIR_LOGS", "logs")
DIR_PROJECTS = _cfg("dir_projects", "MEMORIA_DIR_PROJECTS", "projects")
DIR_TOOLS = _cfg("dir_tools", "MEMORIA_DIR_TOOLS", "tools")
DIR_CONTEXT = _cfg("dir_context", "MEMORIA_DIR_CONTEXT", "context")
DIR_ARCHIVE = _cfg("dir_archive", "MEMORIA_DIR_ARCHIVE", "archive")

# Key files
CONTEXT_FILE = _cfg("context_file", "MEMORIA_CONTEXT_FILE", "context.md")
STATE_FILE = _cfg("state_file", "MEMORIA_STATE_FILE", "_state.md")

# Retention
RETENTION_DAYS = int(_cfg("retention_days", "MEMORIA_RETENTION_DAYS", "14"))


# ============================================================
# Helpers
# ============================================================

def _safe_path(rel_path: str) -> str:
    """Resolve a relative path and ensure it stays within MEMORIA_ROOT."""
    full = os.path.realpath(os.path.join(MEMORIA_ROOT, rel_path))
    root = os.path.realpath(MEMORIA_ROOT)
    if not (full == root or full.startswith(root + os.sep)):
        raise ValueError(f"Path escapes memory root: {rel_path}")
    return full


def _read_file(rel_path: str) -> str:
    """Read a file relative to MEMORIA_ROOT."""
    full = _safe_path(rel_path)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"Not found: {rel_path}")
    with open(full, "r", encoding="utf-8") as f:
        return f.read()


def _write_file(rel_path: str, content: str) -> str:
    """Write content to a file relative to MEMORIA_ROOT."""
    full = _safe_path(rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return full


def _append_file(rel_path: str, content: str) -> str:
    """Append content to a file relative to MEMORIA_ROOT."""
    full = _safe_path(rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "a", encoding="utf-8") as f:
        f.write(content)
    return full


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML-ish frontmatter from markdown. Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta, parts[2].strip()


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter, returning only the body."""
    _, body = _parse_frontmatter(text)
    return body


def _list_files(subdir: str = "", pattern: str = "*.md") -> list[str]:
    """List files in a subdirectory of MEMORIA_ROOT."""
    root = Path(MEMORIA_ROOT) / subdir
    if not root.exists():
        return []
    return sorted(
        str(p.relative_to(MEMORIA_ROOT))
        for p in root.glob(pattern)
    )


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _days_until(date_str: str) -> int | None:
    """Calculate days from today until a date string (YYYY-MM-DD)."""
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        return (target - datetime.now()).days
    except (ValueError, TypeError):
        return None


def _parse_date(date_str: str) -> datetime | None:
    """Parse a YYYY-MM-DD date string, returning None on failure."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _is_completed_line(line: str) -> bool:
    """Check if a line describes something already completed."""
    lower = line.lower()
    markers = [
        "\u2705", "done", "completed", "finished", "submitted", "resolved",
        "leadva", "kész", "teljesítve", "befejezve", "elkészült", "megoldva",
    ]
    return any(m in lower for m in markers)


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") or stripped.startswith("|-")


def _is_metadata_line(line: str) -> bool:
    stripped = line.strip().lower()
    return any(stripped.startswith(k) for k in [
        "updated:", "created:", "source:", "type:", "next_update:",
    ])


def _is_completion_date_line(line: str) -> bool:
    """Check if a date in this line marks completion, not a deadline."""
    lower = line.lower()
    markers = [
        "live", "deployed", "released", "published", "implemented",
        "tesztelve", "létrehozva", "implementálva", "megírva", "aktiválva",
    ]
    return any(m in lower for m in markers)


def _extract_significant_words(text: str) -> set:
    """Extract significant words for fuzzy matching."""
    stopwords = {
        "the", "for", "and", "but", "not", "with", "from", "that", "this",
        "was", "are", "been", "have", "has", "will", "can", "may", "should",
        "egy", "van", "nem", "meg", "már", "ami", "aki", "és", "még",
        "volt", "lesz", "hogy", "ezt", "azt", "mint", "csak", "majd",
        "session", "kell", "nap",
    }
    words = re.findall(r'\w{3,}', text.lower())
    return {w for w in words if w not in stopwords}


def _fuzzy_match(text1: str, text2: str, threshold: float = 0.5) -> bool:
    """Check if two texts share enough significant words."""
    words1 = _extract_significant_words(text1)
    words2 = _extract_significant_words(text2)
    if not words1 or not words2:
        return False
    overlap = len(words1 & words2)
    smaller = min(len(words1), len(words2))
    return (overlap / smaller) >= threshold


# ============================================================
# Processed Items Tracker
# ============================================================

_PROCESSED_FILE = f"{DIR_SESSIONS}/_processed.json"


def _load_processed() -> dict:
    try:
        content = _read_file(_PROCESSED_FILE)
        data = json.loads(content)
        # Migrate old format: convert plain strings to dated dicts
        migrated = False
        new_items = []
        for item in data.get("resolved_items", []):
            if isinstance(item, str):
                new_items.append({"text": item, "resolved_on": data.get("last_updated", _today())})
                migrated = True
            else:
                new_items.append(item)
        if migrated:
            data["resolved_items"] = new_items
            _save_processed(data)
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"processed_sessions": [], "resolved_items": []}


def _save_processed(data: dict) -> None:
    _write_file(_PROCESSED_FILE, json.dumps(data, indent=2, ensure_ascii=False))


# ============================================================
# MCP Server
# ============================================================

mcp = FastMCP(name="memoria-mcp")


# --- Core Context Tools ---

@mcp.tool()
def get_context() -> str:
    """Get a complete overview of the current situation.

    THE tool to call at the start of any session. Returns:
    - Living context model (who, what, deadlines)
    - Current project/experiment state
    - Recent activity (last 3 days of session summaries)
    - Recent log entries

    No parameters needed.
    """
    sections = []
    today = _today()
    sections.append(f"# Context — {today}\n")

    # Living context
    ctx_path = f"{DIR_CONTEXT}/{CONTEXT_FILE}"
    try:
        helyzet = _read_file(ctx_path)
        _, body = _parse_frontmatter(helyzet)
        sections.append("## Current situation\n" + body)
    except FileNotFoundError:
        sections.append(f"## Current situation\n!! {ctx_path} not found")

    # State
    try:
        state = _read_file(STATE_FILE)
        _, body = _parse_frontmatter(state)
        sections.append("## State\n" + body)
    except FileNotFoundError:
        pass  # State file is optional

    # Recent activity
    activity = _get_recent_activity_internal(days=3)
    if activity:
        sections.append("## Recent activity (3 days)\n" + activity)

    # Recent logs
    logs = _list_files(DIR_LOGS, "*.md")
    if logs:
        recent = logs[-3:]
        log_lines = []
        for log_path in recent:
            try:
                content = _read_file(log_path)
                meta, body = _parse_frontmatter(content)
                heading = ""
                for line in body.splitlines():
                    if line.startswith("# "):
                        heading = line[2:].strip()
                        break
                log_lines.append(
                    f"- {meta.get('date', '?')} (session {meta.get('session', '?')}): {heading}"
                )
            except FileNotFoundError:
                pass
        if log_lines:
            sections.append("## Recent logs\n" + "\n".join(log_lines))

    return "\n\n".join(sections)


@mcp.tool()
def get_deadlines() -> str:
    """Get upcoming deadlines sorted by urgency.

    Scans the living context file for dates and returns them with:
    - Days remaining (negative = overdue)
    - Description
    - Urgency level

    Filters out completed items and table formatting.
    """
    ctx_path = f"{DIR_CONTEXT}/{CONTEXT_FILE}"
    try:
        helyzet = _read_file(ctx_path)
    except FileNotFoundError:
        return f"!! {ctx_path} not found"

    body = _strip_frontmatter(helyzet)
    deadlines = []
    seen_dates = set()
    date_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})")

    for line in body.splitlines():
        if _is_table_line(line) or _is_metadata_line(line) or _is_completed_line(line):
            continue
        if _is_completion_date_line(line):
            continue

        for date_str in date_pattern.findall(line):
            if date_str in seen_dates:
                continue
            days = _days_until(date_str)
            if days is not None and days >= -7:
                seen_dates.add(date_str)
                clean = line.strip().lstrip("-•*# ").replace("**", "")

                if days < 0:
                    urgency = "OVERDUE"
                elif days <= 3:
                    urgency = "[!!!] CRITICAL"
                elif days <= 7:
                    urgency = "[!!] URGENT"
                elif days <= 14:
                    urgency = "[!] UPCOMING"
                else:
                    urgency = "DISTANT"

                deadlines.append({
                    "date": date_str, "days": days,
                    "urgency": urgency, "description": clean,
                })

    deadlines.sort(key=lambda d: d["days"])

    if not deadlines:
        return "No known upcoming deadlines in the context file."

    result = "# Deadlines\n\n"
    for d in deadlines:
        result += f"{d['urgency']} | {d['date']} ({d['days']}d) | {d['description']}\n"
    return result


# --- State Management ---

@mcp.tool()
def get_state() -> str:
    """Get the current experiment/project state from the state file.

    Returns the full state including progress, next steps, and blockers.
    """
    try:
        return _read_file(STATE_FILE)
    except FileNotFoundError:
        return f"!! {STATE_FILE} not found"


@mcp.tool()
def update_state(section: str, content: str) -> str:
    """Update a section of the state file.

    Args:
        section: Section header to replace (e.g., "Next steps", "Blocker")
        content: New content for that section
    """
    try:
        state = _read_file(STATE_FILE)
    except FileNotFoundError:
        return f"!! {STATE_FILE} not found"

    pattern = rf"(## {re.escape(section)}.*?)(?=\n## |\Z)"
    match = re.search(pattern, state, re.DOTALL)
    if not match:
        return f"!! Section '{section}' not found in {STATE_FILE}"

    new_section = f"## {section}\n{content}"
    new_state = state[:match.start()] + new_section + state[match.end():]
    new_state = re.sub(r"updated: \d{4}-\d{2}-\d{2}", f"updated: {_today()}", new_state)

    _write_file(STATE_FILE, new_state)
    return f"OK: Section '{section}' updated."


# --- Logging ---

@mcp.tool()
def log_entry(title: str, content: str, entry_type: str = "session") -> str:
    """Create a new log entry.

    Args:
        title: Short title for the heading
        content: Full content (markdown)
        entry_type: "session", "decision", "research", "reflection"
    """
    today = _today()
    existing = _list_files(DIR_LOGS, f"{today}_{entry_type}_*.md")
    entry_num = len(existing) + 1
    filename = f"{DIR_LOGS}/{today}_{entry_type}_{entry_num}.md"

    entry = f"""---
date: {today}
entry: {entry_num}
type: {entry_type}
---

# {title}

{content}
"""
    _write_file(filename, entry)
    return f"OK: Log created: {filename}"


# --- Session Summaries ---

def _get_recent_activity_internal(days: int = 3) -> str:
    root = Path(MEMORIA_ROOT) / DIR_SESSIONS
    if not root.exists():
        return ""

    cutoff = datetime.now() - timedelta(days=days)
    summaries = []

    for md_file in sorted(root.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(content)
            file_date = _parse_date(meta.get("date", ""))
            if file_date and file_date >= cutoff:
                session_type = meta.get("session_type", "?")
                highlights = meta.get("highlights", "")
                if not highlights:
                    for line in body.splitlines():
                        if line.strip() and not line.startswith("#"):
                            highlights = line.strip()
                            break
                summaries.append({
                    "date": meta.get("date", "?"),
                    "type": session_type,
                    "highlights": highlights,
                })
        except Exception:
            continue

    if not summaries:
        return ""

    lines = []
    current_date = ""
    for s in summaries:
        if s["date"] != current_date:
            current_date = s["date"]
            lines.append(f"\n### {current_date}")
        lines.append(f"- **[{s['type']}]** {s['highlights']}")
    return "\n".join(lines).strip()


@mcp.tool()
def log_session_summary(
    session_type: str,
    highlights: str,
    context_updates: list[str] | None = None,
    open_items: list[str] | None = None,
) -> str:
    """Log a structured session summary for cross-session continuity.

    Call at the END of every meaningful session.

    Args:
        session_type: One of: "morning", "interactive", "autonomous",
                      "study", "work" (or any custom type)
        highlights: What happened in 1-3 specific sentences.
        context_updates: Changes that should be noted (e.g., deadline moved)
        open_items: Unfinished items for the next session
    """
    today = _today()
    existing = _list_files(DIR_SESSIONS, f"{today}_{session_type}*.md")
    if existing:
        filename = f"{DIR_SESSIONS}/{today}_{session_type}_{len(existing) + 1}.md"
    else:
        filename = f"{DIR_SESSIONS}/{today}_{session_type}.md"

    ctx_section = ""
    if context_updates:
        ctx_items = "\n".join(f"- {item}" for item in context_updates)
        ctx_section = f"\n## Context changes\n{ctx_items}\n"

    open_section = ""
    if open_items:
        open_list = "\n".join(f"- {item}" for item in open_items)
        open_section = f"\n## Open items\n{open_list}\n"

    entry = f"""---
date: {today}
session_type: {session_type}
highlights: {highlights}
---

# Session: {session_type} — {today}

{highlights}
{ctx_section}{open_section}"""

    _write_file(filename, entry)
    return f"OK: Session summary created: {filename}"


@mcp.tool()
def get_recent_activity(days: int = 3) -> str:
    """Get a quick overview of session activity from the last N days.

    Args:
        days: How many days back to look (default: 3)
    """
    result = _get_recent_activity_internal(days=days)
    if not result:
        return f"No session summaries found in the last {days} days."
    return f"# Recent activity ({days} days)\n\n{result}"


# --- Reflection & Intelligence ---

def _collect_relevant_content(query: str, max_excerpts: int = 10) -> list[dict]:
    """Search all memory files and return structured results with context."""
    query_lower = query.lower()
    results = []
    root = Path(MEMORIA_ROOT)

    for md_file in root.rglob("*.md"):
        rel_path = str(md_file.relative_to(root))
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        name_match = query_lower in rel_path.lower()
        content_lower = content.lower()

        if query_lower not in content_lower and not name_match:
            continue

        meta, body = _parse_frontmatter(content)
        lines = body.splitlines()

        excerpts = []
        for i, line in enumerate(lines):
            if query_lower in line.lower():
                start = max(0, i - 1)
                end = min(len(lines), i + 2)
                snippet = "\n".join(lines[start:end]).strip()
                if snippet not in [e["snippet"] for e in excerpts]:
                    excerpts.append({"line": i + 1, "snippet": snippet})
                if len(excerpts) >= max_excerpts:
                    break

        results.append({
            "file": rel_path, "name_match": name_match,
            "meta": meta, "excerpts": excerpts,
            "mentions": content_lower.count(query_lower),
        })

    results.sort(key=lambda r: (-r["mentions"], -r["name_match"]))
    return results


@mcp.tool()
def reflect(topic: str) -> str:
    """Synthesize everything the memory system knows about a topic.

    Unlike search_memory() which returns raw matches, reflect() provides:
    - Which files mention the topic and how prominently
    - Chronological view from session logs
    - Key excerpts organized by source type
    - Gaps in knowledge

    Args:
        topic: The topic to reflect on
    """
    results = _collect_relevant_content(topic)

    if not results:
        return f"# Reflection: '{topic}'\n\nNo results found. The topic is unknown or not yet documented."

    # Categorize by directory
    categories = defaultdict(list)
    dir_map = {
        DIR_CONTEXT: "context", DIR_PROJECTS: "projects",
        DIR_LOGS: "logs", DIR_SESSIONS: "sessions",
        DIR_TOOLS: "tools", DIR_ARCHIVE: "archive",
    }

    for r in results:
        path = r["file"]
        categorized = False
        for dir_name, cat_name in dir_map.items():
            if path.startswith(dir_name):
                categories[cat_name].append(r)
                categorized = True
                break
        if not categorized:
            categories["other"].append(r)

    # Build output
    total = len(results)
    mentions = sum(r["mentions"] for r in results)
    output = f"# Reflection: '{topic}'\n\n**{total} files**, **{mentions} mentions** total\n\n"

    section_order = [
        ("context", "Current context"),
        ("projects", "Projects"),
        ("logs", "Logs (chronological)"),
        ("sessions", "Session summaries"),
        ("tools", "Tools & infrastructure"),
        ("archive", "Archive"),
        ("other", "Other sources"),
    ]

    for cat_key, cat_title in section_order:
        items = categories.get(cat_key, [])
        if not items:
            continue

        output += f"## {cat_title}\n"

        if cat_key == "sessions":
            for r in items:
                date = r["meta"].get("date", "?")
                stype = r["meta"].get("session_type", "?")
                snippet = r["excerpts"][0]["snippet"].replace("\n", " ")[:150] if r["excerpts"] else ""
                output += f"- **{date} [{stype}]**: {snippet}\n"
            output += "\n"
        elif cat_key in ("tools", "archive", "other"):
            for r in items:
                output += f"- {r['file']} ({r['mentions']} mentions)\n"
            output += "\n"
        else:
            for r in items:
                output += f"### {r['file']}"
                if cat_key == "projects":
                    output += f" ({r['mentions']} mentions)"
                output += "\n"
                for exc in r["excerpts"][:3]:
                    output += f"> {exc['snippet']}\n\n"

    return output


@mcp.tool()
def suggest_context_updates() -> str:
    """Compare recent sessions with the living context and suggest updates.

    Finds:
    - Unprocessed context changes from session summaries
    - Open items that haven't been resolved
    - Stale dates in the context file

    Does NOT modify any files — the caller decides what to apply.
    """
    processed = _load_processed()
    processed_sessions = set(processed.get("processed_sessions", []))
    resolved_items_texts = {
        (ri["text"] if isinstance(ri, dict) else ri)
        for ri in processed.get("resolved_items", [])
    }

    root = Path(MEMORIA_ROOT) / DIR_SESSIONS
    unapplied = []
    open_items = []
    filtered_updates = 0
    filtered_items = 0

    if root.exists():
        cutoff = datetime.now() - timedelta(days=7)
        for md_file in sorted(root.glob("*.md")):
            if md_file.name == "_processed.json":
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                meta, body = _parse_frontmatter(content)
                file_date = _parse_date(meta.get("date", ""))
                if not file_date or file_date < cutoff:
                    continue

                # Context updates
                for section_header in ["## Context changes", "## Kontextus valtozasok"]:
                    if section_header in body:
                        section = body.split(section_header)[1]
                        if "## " in section[1:]:
                            section = section[:section.index("## ", 1)]

                        if md_file.name in processed_sessions:
                            for line in section.strip().splitlines():
                                line = line.strip().lstrip("- ")
                                if line:
                                    filtered_updates += 1
                        else:
                            for line in section.strip().splitlines():
                                line = line.strip().lstrip("- ")
                                if line:
                                    unapplied.append({
                                        "update": line,
                                        "source": md_file.name,
                                        "date": meta.get("date", "?"),
                                    })
                        break

                # Open items
                for section_header in ["## Open items", "## Nyitott elemek"]:
                    if section_header in body:
                        section = body.split(section_header)[1]
                        if "## " in section[1:]:
                            section = section[:section.index("## ", 1)]
                        for line in section.strip().splitlines():
                            line = line.strip().lstrip("- ")
                            if line:
                                if line in resolved_items_texts or any(_fuzzy_match(line, ri) for ri in resolved_items_texts):
                                    filtered_items += 1
                                else:
                                    open_items.append({
                                        "item": line,
                                        "source": md_file.name,
                                        "date": meta.get("date", "?"),
                                    })
                        break
            except Exception:
                continue

    # Deduplicate open items
    if open_items:
        deduped = []
        for item in open_items:
            merged = False
            for existing in deduped:
                if _fuzzy_match(item["item"], existing["item"]):
                    if item["date"] > existing["date"]:
                        existing.update(item)
                    existing["similar_count"] = existing.get("similar_count", 0) + 1
                    merged = True
                    break
            if not merged:
                deduped.append(dict(item))
        open_items = deduped

    # Check stale dates
    stale_dates = []
    ctx_path = f"{DIR_CONTEXT}/{CONTEXT_FILE}"
    try:
        helyzet = _read_file(ctx_path)
        body = _strip_frontmatter(helyzet)
        date_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})")
        now = datetime.now()
        for line in body.splitlines():
            if _is_completed_line(line) or _is_completion_date_line(line):
                continue
            for date_str in date_pattern.findall(line):
                d = _parse_date(date_str)
                if d and d < now - timedelta(days=1):
                    days_ago = (now - d).days
                    if days_ago <= 14:
                        clean = line.strip().lstrip("-•*# ").replace("**", "")
                        stale_dates.append({
                            "date": date_str, "days_ago": days_ago,
                            "line": clean[:120],
                        })
    except FileNotFoundError:
        pass

    # Build output
    output = "# Context update suggestions\n\n"

    if filtered_updates > 0 or filtered_items > 0:
        output += f"_({filtered_updates} processed updates and {filtered_items} resolved items filtered out)_\n\n"

    if unapplied:
        output += "## Unprocessed session updates\n"
        for u in unapplied:
            output += f"- [{u['date']}] {u['update']} _(from: {u['source']})_\n"
        output += "\n"
    else:
        output += "## Session updates\nAll up to date.\n\n"

    if open_items:
        output += "## Open items\n"
        for item in open_items:
            similar = item.get("similar_count", 0)
            suffix = f" _(+{similar} similar)_" if similar else ""
            output += f"- [{item['date']}] {item['item']} _(from: {item['source']})_{suffix}\n"
        output += "\n"

    if stale_dates:
        output += "## Potentially stale dates\n"
        for s in stale_dates:
            output += f"- {s['date']} ({s['days_ago']}d ago): {s['line']}\n"
        output += "\n"

    if not unapplied and not open_items and not stale_dates:
        output += "Everything looks current!\n"

    return output


@mcp.tool()
def acknowledge_updates(
    processed_sessions: list[str] | None = None,
    resolved_items: list[str] | None = None,
) -> str:
    """Mark session updates as processed and/or open items as resolved.

    Args:
        processed_sessions: Session filenames whose context changes are applied
        resolved_items: Open item texts that have been resolved
    """
    data = _load_processed()
    added_s = added_i = 0

    if processed_sessions:
        existing = set(data.get("processed_sessions", []))
        for s in processed_sessions:
            if s not in existing:
                existing.add(s)
                added_s += 1
        data["processed_sessions"] = sorted(existing)

    if resolved_items:
        existing_texts = {
            (ri["text"] if isinstance(ri, dict) else ri)
            for ri in data.get("resolved_items", [])
        }
        for item in resolved_items:
            if item not in existing_texts:
                data.setdefault("resolved_items", []).append({
                    "text": item,
                    "resolved_on": _today(),
                })
                added_i += 1

    data["last_updated"] = _today()
    _save_processed(data)

    return (
        f"OK: {added_s} sessions marked processed, {added_i} items resolved. "
        f"Totals: {len(data['processed_sessions'])} sessions, "
        f"{len(data['resolved_items'])} resolved items."
    )


# --- Search ---

@mcp.tool()
def search_memory(query: str) -> str:
    """Search across all memory files for relevant content.

    Args:
        query: Search term (case-insensitive)
    """
    query_lower = query.lower()
    results = []
    root = Path(MEMORIA_ROOT)

    for md_file in root.rglob("*.md"):
        rel_path = str(md_file.relative_to(root))
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        name_match = query_lower in rel_path.lower()
        if query_lower in content.lower() or name_match:
            excerpts = []
            for i, line in enumerate(content.splitlines()):
                if query_lower in line.lower():
                    excerpts.append(f"  L{i+1}: {line.strip()}")
                    if len(excerpts) >= 5:
                        break
            results.append({"file": rel_path, "name_match": name_match, "excerpts": excerpts[:5]})

    if not results:
        return f"No results for: '{query}'"

    output = f"# Search: '{query}' — {len(results)} results\n\n"
    for r in results:
        output += f"## {r['file']}"
        if r["name_match"]:
            output += " (filename match)"
        output += "\n"
        for exc in r["excerpts"]:
            output += exc + "\n"
        output += "\n"
    return output


# --- Context Management ---

@mcp.tool()
def update_context(field: str, value: str) -> str:
    """Update a section of the living context file.

    Args:
        field: Section keyword to find (e.g., "DEADLINES", "PROJECTS")
        value: New content for that section
    """
    filepath = f"{DIR_CONTEXT}/{CONTEXT_FILE}"
    try:
        helyzet = _read_file(filepath)
    except FileNotFoundError:
        return f"!! {filepath} not found"

    pattern = rf"(## [^\n]*{re.escape(field)}[^\n]*\n)(.*?)(?=\n## |\Z)"
    match = re.search(pattern, helyzet, re.DOTALL | re.IGNORECASE)
    if not match:
        return f"!! Section '{field}' not found."

    header = match.group(1)
    new = helyzet[:match.start()] + header + value + "\n" + helyzet[match.end():]
    new = re.sub(r"updated: \d{4}-\d{2}-\d{2}", f"updated: {_today()}", new)

    _write_file(filepath, new)
    return f"OK: '{field}' updated in context file."


# --- File Management ---

@mcp.tool()
def list_memory_files(subdir: str = "") -> str:
    """List all files in the memory system, optionally filtered by subdirectory.

    Args:
        subdir: Subdirectory to list (empty = root level)
    """
    root = Path(MEMORIA_ROOT) / subdir
    if not root.exists():
        return f"!! Directory not found: {subdir or '(root)'}"

    items = []
    for item in sorted(root.iterdir()):
        rel = str(item.relative_to(MEMORIA_ROOT))
        if item.is_dir():
            count = len(list(item.rglob("*.md")))
            items.append(f"[DIR] {rel}/ ({count} files)")
        elif item.suffix == ".md":
            size = item.stat().st_size
            mtime = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            items.append(f"[FILE] {rel} ({size} bytes, {mtime})")
        elif item.suffix == ".json":
            items.append(f"  {rel}")
        else:
            items.append(f"  {rel}")

    if not items:
        return f"Empty directory: {subdir or '(root)'}"

    return f"# Files: {subdir or 'memoria/'}\n\n" + "\n".join(items)


@mcp.tool()
def read_memory_file(path: str) -> str:
    """Read the full content of any file in the memory system.

    Args:
        path: Relative path within the memory root
    """
    try:
        return _read_file(path)
    except FileNotFoundError:
        return f"!! File not found: {path}"


@mcp.tool()
def write_memory_file(path: str, content: str) -> str:
    """Write or create a file in the memory system.

    Parent directories are created automatically.

    Args:
        path: Relative path within the memory root
        content: Full file content
    """
    _write_file(path, content)
    return f"OK: File written: {path}"


# ============================================================
# Archival System
# ============================================================

def _get_archivable_files(directory: str, retention_days: int) -> list[Path]:
    """Find files older than retention_days in a directory."""
    root = Path(MEMORIA_ROOT) / directory
    if not root.exists():
        return []

    cutoff = datetime.now() - timedelta(days=retention_days)
    archivable = []

    for md_file in sorted(root.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(content)
            file_date = _parse_date(meta.get("date", ""))
            if file_date and file_date < cutoff:
                archivable.append(md_file)
        except Exception:
            continue

    return archivable


def _create_monthly_digest(files: list[Path], source_dir: str) -> dict[str, str]:
    """Create monthly digest content from a list of files.

    Returns: {month_key: digest_content} e.g. {"2026-03": "# March 2026 ..."}
    """
    by_month: dict[str, list[dict]] = defaultdict(list)

    for md_file in files:
        try:
            content = md_file.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(content)
            date = meta.get("date", "unknown")
            month_key = date[:7] if len(date) >= 7 else "unknown"

            by_month[month_key].append({
                "filename": md_file.name,
                "date": date,
                "meta": meta,
                "body": body,
            })
        except Exception:
            continue

    digests = {}
    for month, entries in sorted(by_month.items()):
        lines = [f"# Archive: {source_dir} — {month}\n"]
        lines.append(f"_{len(entries)} entries archived on {_today()}_\n")

        for entry in entries:
            lines.append(f"## {entry['date']} — {entry['filename']}")
            # For sessions, include type and highlights
            if "session_type" in entry["meta"]:
                lines.append(f"**Type:** {entry['meta']['session_type']}")
            if "highlights" in entry["meta"]:
                lines.append(f"**Highlights:** {entry['meta']['highlights']}")
            # Include a condensed version of the body (first 500 chars)
            body_condensed = entry["body"][:500]
            if len(entry["body"]) > 500:
                body_condensed += "\n\n_(truncated)_"
            lines.append(body_condensed)
            lines.append("")

        digests[month] = "\n".join(lines)

    return digests


@mcp.tool()
def archive(dry_run: bool = True) -> str:
    """Archive old sessions and logs beyond the retention period.

    Scans sessions/ and logs/ for files older than RETENTION_DAYS,
    creates monthly digest files in archive/, and moves originals
    to archive/raw/ for safekeeping.

    Args:
        dry_run: If True (default), only shows what WOULD be archived.
                 Set to False to actually perform the archival.

    Returns:
        Summary of what was (or would be) archived.
    """
    output = f"# Archive {'preview' if dry_run else 'result'}\n"
    output += f"_Retention: {RETENTION_DAYS} days. Today: {_today()}_\n\n"

    total_archived = 0

    for source_dir in [DIR_SESSIONS, DIR_LOGS]:
        archivable = _get_archivable_files(source_dir, RETENTION_DAYS)

        if not archivable:
            output += f"## {source_dir}/\nNothing to archive.\n\n"
            continue

        output += f"## {source_dir}/\n"
        output += f"**{len(archivable)} files** ready for archival:\n"

        for f in archivable:
            meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"))
            date = meta.get("date", "?")
            output += f"- {f.name} ({date})\n"

        if not dry_run:
            # Create monthly digests
            digests = _create_monthly_digest(archivable, source_dir)
            for month_key, digest_content in digests.items():
                digest_path = f"{DIR_ARCHIVE}/{source_dir}_{month_key}.md"
                _write_file(digest_path, digest_content)
                output += f"\n→ Digest created: {digest_path}"

            # Move originals to archive/raw/
            raw_dir = Path(MEMORIA_ROOT) / DIR_ARCHIVE / "raw" / source_dir
            raw_dir.mkdir(parents=True, exist_ok=True)
            for f in archivable:
                dest = raw_dir / f.name
                shutil.move(str(f), str(dest))
                output += f"\n→ Moved: {f.name} → {DIR_ARCHIVE}/raw/{source_dir}/"

            # Clean up _processed.json references for archived sessions
            if source_dir == DIR_SESSIONS:
                processed = _load_processed()
                archived_names = {f.name for f in archivable}
                processed["processed_sessions"] = [
                    s for s in processed.get("processed_sessions", [])
                    if s not in archived_names
                ]
                # Prune old resolved_items beyond retention period
                cutoff_date = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
                old_count = len(processed.get("resolved_items", []))
                processed["resolved_items"] = [
                    ri for ri in processed.get("resolved_items", [])
                    if isinstance(ri, dict) and ri.get("resolved_on", "") >= cutoff_date
                ]
                pruned_count = old_count - len(processed["resolved_items"])
                _save_processed(processed)
                output += f"\n→ Cleaned up _processed.json ({len(archived_names)} sessions, {pruned_count} old resolved items removed)"

            total_archived += len(archivable)

        output += "\n\n"

    # Also check for archivable project files (completed projects)
    proj_root = Path(MEMORIA_ROOT) / DIR_PROJECTS
    if proj_root.exists():
        completed_projects = []
        for md_file in proj_root.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                if _is_completed_line(content[:500]):  # Check first 500 chars
                    completed_projects.append(md_file)
            except Exception:
                continue

        if completed_projects:
            output += f"## {DIR_PROJECTS}/\n"
            output += f"**{len(completed_projects)} potentially completed projects** (use archive_project to move):\n"
            for f in completed_projects:
                output += f"- {f.name}\n"
            output += "\n"

    if dry_run:
        output += f"\n_This was a dry run. Call archive(dry_run=False) to execute._"
    else:
        output += f"\n**Done!** {total_archived} files archived."

    return output


@mcp.tool()
def archive_project(path: str) -> str:
    """Archive a completed project file.

    Moves a project file to archive/projects/ with a timestamp.

    Args:
        path: Relative path of the project file to archive
    """
    source = Path(MEMORIA_ROOT) / path
    if not source.exists():
        return f"!! File not found: {path}"

    archive_dir = Path(MEMORIA_ROOT) / DIR_ARCHIVE / DIR_PROJECTS
    archive_dir.mkdir(parents=True, exist_ok=True)

    dest = archive_dir / source.name
    shutil.move(str(source), str(dest))

    return f"OK: Archived {path} → {DIR_ARCHIVE}/{DIR_PROJECTS}/{source.name}"


# Subdirectories allowed for per-file archival via archive_generic().
# sessions/logs use archive() for bulk processing; context and the archive
# directory itself are intentionally excluded (live references or
# already-archived content).
ARCHIVE_GENERIC_WHITELIST = {DIR_TOOLS, DIR_PROJECTS}


@mcp.tool()
def archive_generic(
    file_path: str,
    reason: str,
    digest_entry: bool = True,
) -> str:
    """Archive a single obsolete file from a whitelisted subdirectory.

    Complements the other archive tools:
    - archive() handles sessions/logs in bulk with monthly digests.
    - archive_project() moves completed projects to the archive/projects folder.
    - archive_generic() (this tool) moves single files from the tools or
      projects subdirectory into a dated folder
      ({DIR_ARCHIVE}/{subdir}/YYYY-MM-DD/{file}) and optionally appends the
      reason to {DIR_ARCHIVE}/digest/{subdir}_digest.md so the
      "why did I archive this?" question stays searchable later.

    Use this when a single tool-spec or project doc has become obsolete
    (draft superseded, one-off experiment, decision captured elsewhere)
    and you want it out of the active set without losing the reason.

    Args:
        file_path: Relative path of the file to archive
            (e.g., "tools/old_spec.md"). Must be inside a whitelisted
            subdirectory — by default the directories configured as
            DIR_TOOLS and DIR_PROJECTS.
        reason: 1-2 sentence explanation of why the file is obsolete.
            Written into the digest if digest_entry is True.
        digest_entry: If True (default), append an entry to the subdir's
            digest file under today's date. Set to False for silent archival.

    Returns:
        Confirmation string with the archived destination path.

    Raises:
        FileNotFoundError: The source file does not exist.
        ValueError: The path is not in the whitelist, is malformed, or is a
            path traversal attempt (the latter caught by _safe_path).
    """
    # Normalize separators for the whitelist check
    normalized = file_path.replace("\\", "/").lstrip("./")
    parts = [p for p in normalized.split("/") if p]

    if len(parts) < 2:
        raise ValueError(
            f"file_path must include a subdirectory (e.g. '{DIR_TOOLS}/x.md'), got: {file_path!r}"
        )

    subdir = parts[0]
    if subdir not in ARCHIVE_GENERIC_WHITELIST:
        raise ValueError(
            f"Subdirectory '{subdir}' is not in the archive_generic whitelist. "
            f"Allowed: {sorted(ARCHIVE_GENERIC_WHITELIST)}. "
            f"For sessions/logs use archive(); context and the archive "
            f"directory itself are intentionally excluded."
        )

    # Resolve + validate source (this also catches path traversal via _safe_path)
    source_full = _safe_path(file_path)
    if not os.path.isfile(source_full):
        raise FileNotFoundError(f"Not found: {file_path}")

    # Build destination: {DIR_ARCHIVE}/{subdir}/YYYY-MM-DD/{filename}
    today = _today()
    filename = os.path.basename(source_full)
    dest_rel = f"{DIR_ARCHIVE}/{subdir}/{today}/{filename}"
    dest_full = _safe_path(dest_rel)

    # Collision: same filename already archived today → append HHMMSS suffix
    if os.path.exists(dest_full):
        stem, ext = os.path.splitext(filename)
        timestamp = datetime.now().strftime("%H%M%S")
        filename = f"{stem}_{timestamp}{ext}"
        dest_rel = f"{DIR_ARCHIVE}/{subdir}/{today}/{filename}"
        dest_full = _safe_path(dest_rel)

    os.makedirs(os.path.dirname(dest_full), exist_ok=True)
    shutil.move(source_full, dest_full)

    # Digest entry (optional)
    digest_updated = False
    if digest_entry:
        digest_rel = f"{DIR_ARCHIVE}/digest/{subdir}_digest.md"
        digest_full = _safe_path(digest_rel)
        day_header = f"## {today}"
        entry_line = f"- `{file_path}` — {reason}\n"

        if os.path.isfile(digest_full):
            existing = _read_file(digest_rel)
            if day_header in existing:
                # Today's section already exists — just append the line.
                _append_file(digest_rel, entry_line)
            else:
                # Start a new dated section at the end of the digest.
                _append_file(digest_rel, f"\n{day_header}\n{entry_line}")
        else:
            initial = (
                f"# Archive digest: {subdir}/\n\n"
                f"_Chronological trail of files archived from {subdir}/ via "
                f"archive_generic(). Each entry ties a date to a file and a "
                f"short reason._\n\n"
                f"{day_header}\n{entry_line}"
            )
            _write_file(digest_rel, initial)
        digest_updated = True

    msg = f"OK: archived {file_path} → {dest_rel}"
    if digest_updated:
        msg += f" (digest: {DIR_ARCHIVE}/digest/{subdir}_digest.md)"
    return msg


@mcp.tool()
def search_archive(query: str) -> str:
    """Search through archived content.

    Searches both monthly digests and raw archived files.

    Args:
        query: Search term (case-insensitive)
    """
    query_lower = query.lower()
    results = []
    archive_root = Path(MEMORIA_ROOT) / DIR_ARCHIVE

    if not archive_root.exists():
        return "No archive directory found. Nothing has been archived yet."

    for md_file in archive_root.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            if query_lower in content.lower():
                rel = str(md_file.relative_to(Path(MEMORIA_ROOT)))
                excerpts = []
                for i, line in enumerate(content.splitlines()):
                    if query_lower in line.lower():
                        excerpts.append(f"  L{i+1}: {line.strip()}")
                        if len(excerpts) >= 3:
                            break
                results.append({"file": rel, "excerpts": excerpts})
        except Exception:
            continue

    if not results:
        return f"No archived results for: '{query}'"

    output = f"# Archive search: '{query}' — {len(results)} results\n\n"
    for r in results:
        output += f"## {r['file']}\n"
        for exc in r["excerpts"]:
            output += exc + "\n"
        output += "\n"
    return output


# ============================================================
# Start
# ============================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
