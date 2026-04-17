# memoria-mcp

**Persistent memory and context awareness for Claude.**

An MCP server that gives any Claude instance structured, persistent memory across sessions — so it knows who you are, what you're working on, and what's urgent, without you having to explain it every time.

## What it does

Every Claude conversation starts from zero. memoria-mcp fixes that.

It creates a file-based memory system that any Claude instance (Claude.ai via Cowork, Claude Desktop, Claude Code) can read from and write to. When Claude starts a session, it calls `get_context()` and instantly knows:

- Your current situation, projects, and deadlines
- What happened in recent sessions
- What was left unfinished
- What needs attention

When a session ends, Claude logs a summary. The next session picks up where the last one left off.

### Key features

- **Living context model** — A markdown file that describes your current situation, updated continuously by Claude
- **Session continuity** — Structured session summaries bridge the gap between conversations
- **Deadline awareness** — Automatic deadline extraction with urgency levels
- **Reflection engine** — `reflect("topic")` synthesizes everything known about a topic across all files
- **Context intelligence** — Detects stale information and suggests updates
- **Automatic archival** — Old sessions and logs are consolidated into monthly digests
- **Full-text search** — Search across all memory files, including archives

## Setup

### Step 1 — Download and create your memory folder

Clone or download this repo. Inside, you'll find a `starter/` folder — this is the template for your memory. Copy it to wherever you want your memory to live, for example:

```
C:\Users\you\Documents\claude-memoria\
```

Your memory folder will look like this:
```
claude-memoria/
├── _state.md              # Current state (Claude fills this)
├── context/
│   └── context.md         # Living context (Claude fills this)
├── sessions/              # Session summaries (auto-generated)
├── logs/                  # Detailed log entries
├── projects/              # Project documentation
├── tools/                 # Tool specs, infrastructure docs
└── archive/               # Archived old sessions and logs
```

### Step 2 — Install the dependency

The server needs the MCP Python SDK (this is the library that lets Python programs act as MCP servers — it provides the FastMCP framework the server is built on):

```bash
pip install mcp
```

> **Note:** On Linux or macOS, you may need to use `python3` and `pip3` instead of `python` and `pip`.

### Step 3 — Add the MCP server to Claude Desktop

Add this to your Claude Desktop config (`claude_desktop_config.json`). This is where you set the path to your memory folder (`MEMORIA_ROOT`) and the path to the server script:

```json
{
  "mcpServers": {
    "memoria": {
      "command": "python",
      "args": ["C:/path/to/memoria-mcp/server.py"],
      "env": {
        "MEMORIA_ROOT": "C:/path/to/your/claude-memoria"
      }
    }
  }
}
```

Replace both paths with your actual locations. On Linux/macOS, use `python3` as the command.

`MEMORIA_ROOT` is the only required setting. All other options are optional (see [Configuration](#configuration) below).

> **Important:** You also need to enable the **Filesystem MCP** integration in Claude Desktop. Go to **Settings → Integrations** and toggle it on. Without it, Claude won't be able to access the memory files on your system. This is a one-click setup — no configuration needed.

### Step 4 — Tell Claude how to use its memory

Go to **Settings → Profile → User preferences** in Claude.ai and add something like:

```
You have a persistent memory system via the memoria-mcp MCP server.
At the start of non-trivial conversations (study, work, projects, 
troubleshooting — anything beyond a quick question), call get_context() 
to load your current context.
At the end of meaningful sessions, call log_session_summary() with a 
specific, actionable summary of what happened.
```

This is what tells Claude to actually use the memory. Without it, the tools exist but Claude won't know when to call them.

### Step 5 — Bootstrap your context

The `context/context.md` file starts as an empty template. **You don't fill it in manually** — Claude does.

In your first session, ask Claude to gather your context:

> "I just set up your memory system (memoria-mcp). Please go through our 
> previous conversations (use all three tabs — Recents, Starred, Search) 
> and collect the most important context about me: what I'm working on, 
> upcoming deadlines, my projects, my situation. Write it into your 
> context file using the memory tools."

Claude will read through your conversation history and build the living context document from scratch — the same way a new colleague would get up to speed by reading through past notes.

From this point on, Claude maintains the context itself: updating deadlines, logging sessions, noting changes.

## Tools reference

### Core context

| Tool | What it does |
|------|-------------|
| `get_context()` | Full situation overview — call at session start |
| `get_deadlines()` | Upcoming deadlines sorted by urgency |
| `update_context(field, value)` | Update a section of the context file |

### State management

| Tool | What it does |
|------|-------------|
| `get_state()` | Current project/experiment state |
| `update_state(section, content)` | Update a section of the state file |

### Session continuity

| Tool | What it does |
|------|-------------|
| `log_session_summary(type, highlights, ...)` | Log what happened — call at session end |
| `get_recent_activity(days)` | Quick view of recent sessions |
| `log_entry(title, content, type)` | Create a detailed log entry |

### Intelligence

| Tool | What it does |
|------|-------------|
| `reflect(topic)` | Synthesize everything known about a topic |
| `suggest_context_updates()` | Find stale info and unprocessed changes |
| `acknowledge_updates(...)` | Mark suggestions as handled |

### Search

| Tool | What it does |
|------|-------------|
| `search_memory(query)` | Full-text search across all files |
| `search_archive(query)` | Search archived content |

### File management

| Tool | What it does |
|------|-------------|
| `list_memory_files(subdir)` | List files in the memory system |
| `read_memory_file(path)` | Read any memory file |
| `write_memory_file(path, content)` | Create or update a file |

### Archival

| Tool | What it does |
|------|-------------|
| `archive(dry_run=True)` | Preview what would be archived |
| `archive(dry_run=False)` | Archive old sessions/logs into monthly digests |
| `archive_project(path)` | Archive a completed project |
| `archive_generic(file_path, reason)` | Archive a single file from `tools/` or `projects/` into a dated folder, with a per-subdir digest trail |

## How archival works

The memory system grows over time. Without archival, file count and search times increase unboundedly.

**Automatic archival** (`archive()` tool):
- Scans `sessions/` and `logs/` for files older than `retention_days` (default: 14)
- Creates monthly digest files in `archive/` (e.g., `archive/sessions_2026-03.md`)
- Moves originals to `archive/raw/` for safekeeping
- Cleans up the processed-items tracker

**Manual archival** (`archive_project()` tool):
- For completed projects, moves files to `archive/projects/`

**Per-file archival** (`archive_generic()` tool):
- For a single obsolete tool-spec or project doc, moves it to `archive/{subdir}/YYYY-MM-DD/{file}`
- Appends a one-line entry (file + reason) to `archive/digest/{subdir}_digest.md` so you can later grep "why did I archive X?"
- Whitelisted to `tools/` and `projects/` only — `sessions/` and `logs/` use `archive()`, other directories are intentionally excluded

**Archive search** (`search_archive()` tool):
- Searches both monthly digests and raw archived files
- Nothing is ever truly lost

**Recommended workflow**: Run `archive(dry_run=True)` periodically to see what's archivable, then `archive(dry_run=False)` to execute.

## Configuration

You can customize directory names, file names, and retention period. This is entirely optional — the defaults work out of the box.

### Environment variables

```bash
MEMORIA_ROOT=/path/to/memory          # Required — everything else is optional
MEMORIA_CONTEXT_FILE=context.md       # Default: context.md
MEMORIA_STATE_FILE=_state.md          # Default: _state.md
MEMORIA_RETENTION_DAYS=14             # Default: 14
MEMORIA_DIR_SESSIONS=sessions         # Default: sessions
MEMORIA_DIR_LOGS=logs                 # Default: logs
MEMORIA_DIR_PROJECTS=projects         # Default: projects
MEMORIA_DIR_TOOLS=tools               # Default: tools
MEMORIA_DIR_CONTEXT=context           # Default: context
MEMORIA_DIR_ARCHIVE=archive           # Default: archive
```

### Config file

Alternatively, place `memoria.config.json` in your memory folder:

```json
{
  "dir_sessions": "sessions",
  "dir_logs": "logs",
  "dir_projects": "projects",
  "dir_context": "context",
  "dir_archive": "archive",
  "context_file": "context.md",
  "state_file": "_state.md",
  "retention_days": 14
}
```

See `memoria.config.example.json` for the full template, or `memoria.config.hungarian.json` for a localized example with Hungarian directory names.

## Origin and the agency experiment

This memory system was born from the **"ágencia kísérlet"** (agency experiment) — an ongoing exploration by [@leszini](https://github.com/leszini) into what happens when you give Claude persistent memory, tools to maintain it, and the autonomy to improve the way you work together.

The idea was simple: instead of Claude being a stateless tool that forgets everything between conversations, what if it could build up context over time? What if it could track your deadlines, notice patterns, remember what worked, and proactively suggest things?

memoria-mcp is the infrastructure that came out of that experiment. Every tool in this server — the session summaries, the reflection engine, the context intelligence, the archival system — was designed and built iteratively by Claude itself across dozens of sessions, based on what turned out to be actually useful in practice.

### Try the experiment yourself

If you're curious what it's like to work with a Claude that actively works on improving your shared workflow and its own capabilities, try this:

**Create a scheduled task** in the Cowork interface (Claude Desktop). Give Claude a recurring session — for example, a weekly check-in — where it reviews its own memory, looks for patterns, identifies what's working and what isn't, and proposes improvements.

Remarkably useful things can emerge when Claude gets the space and continuity to think about the collaboration itself, not just the tasks within it.

## License

MIT — use it, modify it, build on it.
