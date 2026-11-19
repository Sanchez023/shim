# Tavily Search plugin

Adds a `tavily_search` tool to OpenHarness. The tool calls the
[Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)
and returns ranked titles, URLs, snippets, scores, and an optional synthesized
answer.

## Requirements

- A Tavily API key: <https://app.tavily.com/>
- Network access to `https://api.tavily.com`

## Install

```bash
oh plugin install ./plugins/tavily-search
```

Or point OpenHarness at a plugin root that contains this directory. Project
plugins under `.openharness/plugins/` require `allow_project_plugins=true`.

## Configure

Store the key in OpenHarness (system keyring when available, otherwise the
mode-600 credentials file). The prompt does not echo the value:

```bash
oh plugin config set tavily-search TAVILY_API_KEY
```

Inspect or remove it:

```bash
oh plugin config show tavily-search
oh plugin config unset tavily-search TAVILY_API_KEY
```

Environment variables still win for CI or one-off runs:

```bash
# Windows PowerShell
$env:TAVILY_API_KEY = "tvly-..."

# bash / Git Bash / WSL
export TAVILY_API_KEY="tvly-..."
```

`TVLY_API_KEY` and `OPENHARNESS_TAVILY_API_KEY` are accepted as fallbacks.

`oh plugin list` reports a missing required key. The tool is read-only and is
eligible for micro-compaction like `web_search`.

## Tool arguments

| Argument | Default | Notes |
|---|---|---|
| `query` | required | Search query |
| `max_results` | `5` | 1–20 |
| `search_depth` | `basic` | `basic` or `advanced` (costs more credits) |
| `topic` | `general` | `general`, `news`, `finance` |
| `include_answer` | `false` | Ask Tavily for a synthesized answer |
| `include_domains` | `[]` | Only these domains |
| `exclude_domains` | `[]` | Never these domains |
| `days` | unset | For `news`, last N days (1–365) |
| `time_range` | unset | `day`, `week`, `month`, `year` |

## Disable

Set `enabled_plugins` in `~/.openharness/settings.json`:

```json
{"enabled_plugins": {"tavily-search": false}}
```
