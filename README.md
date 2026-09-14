# deltachat-claude-code <img width="45" alt="social-preview" src="https://github.com/user-attachments/assets/91703ad9-b5ad-434b-aa85-237e5851266c" />

[![PyPI](https://img.shields.io/pypi/v/deltachat-claude-code)](https://pypi.org/project/deltachat-claude-code/)
[![Python](https://img.shields.io/pypi/pyversions/deltachat-claude-code)](https://pypi.org/project/deltachat-claude-code/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Access self-hosted Claude Code from your phone — full CLI sessions over encrypted chat, no signup, no terminal needed.**

Host a bot that proxies full
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI sessions to any device via lightweight encrypted chat. This is not an API wrapper or a chatbot skin, it's the whole `claude` binary over a chat transport. Everything you get in a terminal session is bridged into an easy chat interface: file editing, bash, git,
multi-step tool chains, code review, subagents, CLAUDE.md context, and the full
slash-command surface.

[Delta Chat](https://delta.chat) is the messenger of choice here: a decentralized, encrypted chat system with no signup and that requires no
phone number, email or login: install it, add the bot as a contact, and start
prompting. See [why](#why) this is the best way to interact with Claude Code.

## Contents

- [Why](#why)
- [Screenshots](#screenshots)
- [Architecture](#architecture)
- [System requirements](#system-requirements)
- [How to setup](#how-to-setup)
- [Using Claude Code through the chat](#using-claude-code-through-the-chat)
- [Security](#security)
- [To note](#to-note)
- [Known limitations](#known-limitations)

## Why

- **Why Delta Chat?** Your prompts aren't stored anywhere but your machine. Delta Chat is
  decentralized, encrypted email under the hood, so no third-party platform ever sees your
  conversation. It also requires no account, no phone number, no signup: it's the lowest-friction path
  between "I have a server" and "I'm talking to it from any device."

- **Cumulative chat transcripts.** Claude Code session contexts are ephemeral: they live
  in a terminal that scrolls away. A project conversation turns every project
  into an infinitely scrollable, searchable chat thread, no matter how many sessions you created or /clear commands you used. The Delta Chat thread is a really useful project diary.

- **Reply-to context.** When you reply to a specific message in the chat, the
  quoted text is forwarded to Claude as context. Instead of re-explaining what
  you're referring to, just swipe-reply on the message and add your follow-up.
  This is something a terminal can't do — you can't "reply to" a specific line
  of output.

- **Project-based chats.** Use `/commission <name> <directory>` to create a dedicated group
  chat for a project in a given folder. Each commissioned chat gets its own session and randomly generated identicon avatar.

- **Vibe code with friends.** The bot is a Delta Chat contact like any other, and a commissioned group chat is just a group chat. Add your other contacts to a chat with the agent and work on a project together!

- **Session portability.** A session started from your phone can be resumed from
  a terminal (`claude --resume <id>`), and vice versa. The underlying `.jsonl`
  session file is the same one Claude Code uses natively. You're not locked into
  the chat interface; it's just another way in.

- **Voice memos.** With optional
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) integration, send
  voice memos and they'll be transcribed before reaching Claude. The bot echoes
  the transcription back so you can verify what Claude received.

- **Screenshots and file delivery.** Send images for Claude to analyze; use
  `/send <path>` to deliver generated files (images, PDFs, build artifacts)
  back to your chat. Attachments are saved to `.agentbot-inbox/` in the
  session's working directory.


## Screenshots

| Tool output in a project chat |Transcription echo before Claude responds | Multi-turn conversation with file analysis  |Committing and pushing from chat |
|:---:|:---:|:---:|:---:|
| ![Bash output](screenshots/bash-output.png) | ![Voice memo](screenshots/voice-memo.png) | ![Conversation](screenshots/conversation.png) | ![Git workflow](screenshots/git-workflow.png) |


## Architecture

```
bot.py          event loop: DC message in → slash command or session.send_user()
session.py      Session wraps `claude -p --stream-json` subprocess; SessionManager
                caps concurrent processes, idle-reaps after timeout
render.py       stream-json events → coalesced DC messages + emoji reactions (⏳/✅/❌)
commands.py     slash router: /new, /clear, /exit, /resume, /model, /mode, /commission, etc.
store.py        SQLite: chat↔session bindings, per-turn usage tracking
avatar.py       random identicon avatars for commissioned project chats
transcribe.py   optional faster-whisper voice memo transcription
provision.py    one-time setup: creates chatmail account, avatar, systemd unit
```

## System requirements

### Prerequisites

- A Linux machine with [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
  installed and authenticated (`claude` on your PATH)
- Python 3.11+
- [Delta Chat](https://delta.chat) on your phone (or any device)

### Resource usage

The bot itself is lightweight (~20 MB RSS). The cost is in the Claude Code
subprocesses it manages — each one is a full Node.js process.

| Component | RAM | Notes |
|---|---|---|
| Bot process | ~20 MB | Always resident while the service is running |
| Each Claude Code session | ~300 MB | One per active chat; idle sessions are reaped |
| faster-whisper (optional) | ~200 MB | Loaded per transcription, then released |

With the default `max_live_sessions = 3`, peak usage is roughly **1 GB** (bot +
3 sessions). Idle-reaped sessions release their memory; sending a new message
respawns the subprocess.

**Minimum:** 2 GB free RAM is comfortable for typical use (1-2 concurrent
sessions). Whisper memory is transient — it loads for each voice memo and
releases after. Machines with 4 GB+ total RAM should have no issues.

The bot uses negligible CPU when idle. CPU spikes briefly when Claude Code
processes a turn, but the actual inference happens on Anthropic's servers — your
machine just runs the tool calls (bash, file I/O, git).

## How to setup

### Install

```bash
pip install deltachat-claude-code

# Optional: voice memo transcription
pip install deltachat-claude-code[voice]
```

Or install from source:

```bash
git clone https://github.com/maphouse/deltachat-claude-code
cd deltachat-claude-code
pip install .            # or: pip install .[voice]
```

### Configure and provision

```bash
# Create a directory for your bot instance
mkdir my-bot && cd my-bot

# Copy the example config and edit it
cp /path/to/config.example.toml config.toml
# Or download it:
# curl -O https://raw.githubusercontent.com/maphouse/deltachat-claude-code/main/config.example.toml
# Edit config.toml: set admin_addresses, allowed_roots, default_cwd

# Provision (creates chatmail account, avatar, systemd unit)
deltachat-claude-code-provision
```

Provisioning prints the bot's chatmail address. Open Delta Chat on your phone,
tap "New Chat," and enter that address. Send any message to start a session.

### Running as a service

```bash
# Foreground (for testing)
deltachat-claude-code

# As a systemd service (provisioning installs this), internally called agentbot
sudo systemctl enable --now agentbot.service
journalctl -u agentbot -f   # watch logs
```

## Using Claude Code through the chat

Any `/command` not listed below is forwarded to Claude Code as-is — so
`/code-review`, `/security-review`, `/init`, `/compact`, and all other Claude Code
slash commands work.

### Commissioning chats

Use `/commission <name> [dir]` to create a dedicated group chat for a project.
Each commissioned chat gets its own session, working directory, and randomly
generated identicon avatar. This is how you keep multiple long-running projects
separate.

### Session control inside a chat

These commands control the Claude Code session from inside a persistent chat.

| Command | What it does |
|---|---|
| `/new [dir]` | Fresh session, optionally in a different directory |
| `/clear` | Restart session in the same directory |
| `/exit` | End session — prints a `claude --resume` command for terminal pickup |
| `/resume <id>` | Resume a previous session by ID |
| `/sessions` | List bound chats and recent sessions on disk |
| `/stop` | Interrupt a running turn |

### Settings

| Command | What it does |
|---|---|
| `/model [name]` | Show or set model (sonnet, opus, haiku, fable, or full ID) |
| `/mode [name]` | Show, set, or cycle permission mode |
| `/cwd [path]` | Show or change working directory |
| `/effort [level]` | Show or set effort (low, medium, high, xhigh, max) |
| `/verbose [on\|off]` | Toggle tool and thinking visibility in chat |
| `/maxsessions [n]` | Show or set max concurrent Claude subprocesses |

### Info

| Command | What it does |
|---|---|
| `/usage` | Session, today, and weekly stats (turns, tokens, context fill) |
| `/send <path>` | Send a file (image, PDF, etc.) from the server to this chat |
| `/help` | All bot commands plus Claude Code's own command list |


## Security

The `admin_addresses` list in `config.toml` is the **only access control**. The
bot runs with `bypassPermissions`, meaning Claude Code will execute any tool
without confirmation. This is deliberate — confirmation prompts can't work over
chat — but it means the allowlist is load-bearing. Only add addresses you trust
with full shell access to the machine.

## To note

- **Concurrent subprocess cap** (default 3). You can have unlimited chat groups,
  but only 3 can have a live Claude Code process at once. You can adjust this maximum
  at runtime with `/maxsessions <n>`, or permanently in `config.toml`. If you message a
  chat beyond the limit, the least-recently-used subprocess is terminated to make
  room. Nothing is lost — the session resumes automatically on the next message.
- **Idle reaping** (default 30 minutes). Inactive sessions are terminated to free
  memory (~300 MB per subprocess). The session resumes transparently when you send
  the next message. Configure with `idle_timeout_min` in `config.toml`.
- Messages are split at 4000 chars

## Known limitations

- **No plan usage visibility.** `/usage` shows context window fill and
  session-level token counts, but can't show how much of your Claude Pro/Max
  subscription quota you've consumed — the CLI doesn't expose that. Check
  [claude.ai/settings](https://claude.ai/settings) for plan-level usage.
- **No interactive prompts.** The bot runs with `bypassPermissions` because
  confirmation dialogs can't work over chat. This is a security tradeoff,
  not a bug.
- **No inline artifacts.** Claude Code artifacts and HTML previews don't
  render in Delta Chat. Generated files (images, PDFs) can be delivered
  to the chat with `/send <path>`.
- **Some slash commands need a TTY.** `/config`, `/keybindings`, `/loop`,
  and `/schedule` are blocked because they require interactive terminal input.

## License

MIT
