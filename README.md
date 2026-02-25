# mail-calendar-macos-mcp

An MCP (Model Context Protocol) server that provides safe, structured access to **Apple Mail** and **Apple Calendar** on macOS via `osascript`.

## Features

- **Apple Mail**: list accounts, mailboxes, messages; read message bodies; send emails; move/archive messages.
- **Apple Calendar**: list calendars, list events (with recurrence expansion), create events.
- **Two-step confirmation**: write actions (send, move, create event) require a prepare + confirm flow with a token, preventing accidental execution.
- **No raw AppleScript exposure**: all user input is passed as `argv` to constant scripts, preventing injection.

## Requirements

- macOS (uses `/usr/bin/osascript`)
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Quick Start

```bash
# Run directly with uv (no install needed)
uv run server.py
```

All Python dependencies are declared inline via PEP 723, so `uv run` handles them automatically.

## Configuration

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `OSASCRIPT_MCP_TIMEOUT_MS` | `30000` | Timeout for osascript execution (ms) |
| `OSASCRIPT_MCP_DEBUG` | `0` | Set to `1` to print osascript stderr |

## MCP Client Setup

### OpenCode

Add to your `opencode.jsonc`:

```jsonc
{
  "mcp": {
    "mail-calendar-macos": {
      "type": "local",
      "command": [
        "uv",
        "run",
        "--script",
        "/path/to/mail-calendar-macos-mcp/server.py"
      ],
      "environment": {
        "OSASCRIPT_MCP_DEBUG": "0",
        "OSASCRIPT_MCP_TIMEOUT_MS": "120000"
      }
    }
  }
}
```

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mail-calendar-macos": {
      "command": "uv",
      "args": ["run", "--script", "/path/to/mail-calendar-macos-mcp/server.py"],
      "env": {
        "OSASCRIPT_MCP_DEBUG": "0",
        "OSASCRIPT_MCP_TIMEOUT_MS": "120000"
      }
    }
  }
}
```

## Tools

### Mail

| Tool | Description |
|---|---|
| `mail_list_accounts` | List Apple Mail accounts |
| `mail_list_mailboxes` | List mailboxes for an account |
| `mail_list_messages` | List recent messages (metadata) |
| `mail_get_message` | Get a message with optional body |
| `mail_prepare_send` | Prepare an email (returns confirmation token) |
| `mail_send` | Send a prepared email |
| `mail_prepare_move` | Prepare moving a message (returns confirmation token) |
| `mail_move_message` | Move a prepared message |

### Calendar

| Tool | Description |
|---|---|
| `calendar_list_calendars` | List calendars by name |
| `calendar_list_calendars_detailed` | List calendars with index and source account |
| `calendar_list_events` | List events in a date range (with recurrence expansion) |
| `calendar_prepare_create_event` | Prepare creating an event (returns confirmation token) |
| `calendar_create_event` | Create a prepared event |

## License

MIT
