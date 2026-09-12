import glob
import logging
import os
import re
import uuid
from pathlib import Path

from . import store
from .avatar import make_avatar
from .session import control_ok

log = logging.getLogger("agentbot.commands")

SLASH_RE = re.compile(r"^/([a-z][a-z0-9-]*)(?:\s+([\s\S]+))?$", re.IGNORECASE)

BLOCKED = {"config", "keybindings", "keybindings-help", "update-config", "loop", "schedule"}


def classify(text: str) -> tuple[str, str] | None:
    text = text.strip()
    if not text.startswith("/"):
        return None
    m = SLASH_RE.match(text)
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or "").strip()


def handle(cmd: str, args: str, chat_id: int, chat, bot) -> str | None:
    handler = HANDLERS.get(cmd)
    if handler:
        return handler(args, chat_id, chat, bot)
    if cmd in BLOCKED:
        return f"/{cmd} requires a TTY — use `ssh thinkpad -t 'claude'` instead."
    return None


def _cmd_new(args, chat_id, chat, bot):
    cwd = args or bot.config["default_cwd"]
    cwd = os.path.expanduser(cwd)
    if not _check_root(cwd, bot):
        return f"path not under allowed_roots: {cwd}"
    if not os.path.isdir(cwd):
        return f"directory not found: {cwd}"
    old = store.get_binding(chat_id)
    old_id = old["session_id"] if old else None
    bot.session_manager.remove(chat_id)
    session_id = str(uuid.uuid4())
    session = bot.spawn_session(chat_id, session_id, cwd)
    store.set_binding(chat_id, session_id, cwd, bot.config["default_model"],
                      bot.config["default_permission_mode"])
    msg = f"new session in {cwd}"
    if old_id:
        msg += f"\nprevious: {old_id}"
    return msg


def _cmd_clear(args, chat_id, chat, bot):
    binding = store.get_binding(chat_id)
    if not binding:
        return "no session bound to this chat"
    old_id = binding["session_id"]
    bot.session_manager.remove(chat_id)
    session_id = str(uuid.uuid4())
    cwd = binding["cwd"]
    bot.spawn_session(chat_id, session_id, cwd)
    store.set_binding(chat_id, session_id, cwd, binding.get("model", bot.config["default_model"]),
                      binding.get("permission_mode", bot.config["default_permission_mode"]),
                      effort=binding.get("effort"), name=binding.get("name"))
    return f"cleared — fresh session in {cwd}\nprevious: {old_id}"


def _cmd_exit(args, chat_id, chat, bot):
    binding = store.get_binding(chat_id)
    if not binding:
        return "no session bound to this chat"
    sid = binding["session_id"]
    cwd = binding["cwd"]
    usage = store.session_usage(sid)
    turns = usage.get("turns", 0)
    cost = usage.get("total_cost") or 0
    bot.session_manager.remove(chat_id)
    store.delete_binding(chat_id)
    lines = [
        f"session ended · {turns} turns · ~${cost:.2f}",
        f"Terminal:  ssh thinkpad -t 'cd {cwd} && claude --resume {sid}'",
        f"Here:     /resume {sid}",
    ]
    return "\n".join(lines)


def _cmd_resume(args, chat_id, chat, bot):
    if not args:
        return "usage: /resume <session-id>"
    session_id = args.split()[0]
    binding = store.get_binding(chat_id)
    cwd = binding["cwd"] if binding else bot.config["default_cwd"]
    bot.session_manager.remove(chat_id)
    session = bot.spawn_session(chat_id, session_id, cwd, resume=True)
    store.set_binding(chat_id, session_id, cwd,
                      binding.get("model", bot.config["default_model"]) if binding else bot.config["default_model"],
                      binding.get("permission_mode", bot.config["default_permission_mode"]) if binding else bot.config["default_permission_mode"])
    return f"resumed session {session_id} in {cwd}"


def _cmd_sessions(args, chat_id, chat, bot):
    bindings = store.all_bindings()
    live = bot.session_manager.all_live()
    lines = ["bound chats:"]
    if not bindings:
        lines.append("  (none)")
    for b in bindings:
        sid = b["session_id"][:8]
        status = "live" if b["chat_id"] in live else "idle"
        name = b.get("name") or ""
        lines.append(f"  {name or 'chat'} · {sid}… · {b['cwd']} [{status}]")
    recent = _scan_recent_sessions()
    if recent:
        lines.append("\nrecent sessions on disk:")
        for sid, path in recent[:10]:
            lines.append(f"  {sid[:8]}… · {path}")
    return "\n".join(lines)


def _cmd_stop(args, chat_id, chat, bot):
    session = bot.session_manager.get(chat_id)
    if not session:
        return "no live session"
    session.interrupt()
    return "interrupted"


CLEAR_MODEL = {"default", "auto", "settings", "-"}


def _current_model(session, binding, bot):
    """What this chat is actually running on, or None if settings files decide."""
    if session and session.model:
        return session.model
    if session and session.init_data and session.init_data.get("model"):
        return session.init_data["model"]
    return (binding.get("model") if binding else None) or bot.config["default_model"]


def _cmd_model(args, chat_id, chat, bot):
    session = bot.session_manager.get(chat_id)
    binding = store.get_binding(chat_id)

    if not args:
        current = _current_model(session, binding, bot)
        shown = current or "from settings files"
        if session and session.init_data:
            models = _get_init_list(session, "models")
            lines = []
            for m in models:
                value, resolved = m.get("value", ""), m.get("resolvedModel", "")
                marker = " ←" if current and (value == current or current in resolved) else ""
                lines.append(f"  {m.get('displayName', value or '?')}{marker}")
            if lines:
                return f"model: {shown}\n" + "\n".join(lines)
        return f"model: {shown}"

    model = args.split()[0]
    if model.lower() in CLEAR_MODEL:
        store.update_binding(chat_id, model=None)
        return ("model override cleared — the cwd's settings files decide "
                "(takes effect on next session start)")

    if session and session.alive:
        ok, _, err = control_ok(session.control("set_model", model=model))
        if ok:
            session.model = model
            store.update_binding(chat_id, model=model)
            return f"model → {model}"
        return f"set_model failed: {err}"
    store.update_binding(chat_id, model=model)
    return f"model → {model} (takes effect on next session start)"


def _cmd_mode(args, chat_id, chat, bot):
    modes = ["auto", "plan", "acceptEdits", "manual", "dontAsk", "bypassPermissions"]
    binding = store.get_binding(chat_id)
    current = binding.get("permission_mode", bot.config["default_permission_mode"]) if binding else bot.config["default_permission_mode"]

    if not args:
        idx = modes.index(current) if current in modes else -1
        new_mode = modes[(idx + 1) % len(modes)]
    else:
        new_mode = args.split()[0]
        if new_mode not in modes:
            return f"unknown mode: {new_mode}\navailable: {', '.join(modes)}"

    session = bot.session_manager.get(chat_id)
    if session and session.alive:
        ok, _, err = control_ok(session.control("set_permission_mode", mode=new_mode))
        if ok:
            session.permission_mode = new_mode
            store.update_binding(chat_id, permission_mode=new_mode)
            return f"mode → {new_mode}"
        return f"set_permission_mode failed: {err}"
    store.update_binding(chat_id, permission_mode=new_mode)
    return f"mode → {new_mode} (takes effect on next session start)"


def _cmd_cwd(args, chat_id, chat, bot):
    binding = store.get_binding(chat_id)
    if not args:
        cwd = binding["cwd"] if binding else bot.config["default_cwd"]
        return f"cwd: {cwd}"
    new_cwd = os.path.expanduser(args.split()[0])
    if not _check_root(new_cwd, bot):
        return f"path not under allowed_roots: {new_cwd}"
    if not os.path.isdir(new_cwd):
        return f"directory not found: {new_cwd}"
    if not binding:
        return "no session bound — use /new first"
    session = bot.session_manager.get(chat_id)
    sid = binding["session_id"]
    if session and session.alive:
        session.terminate()
    bot.spawn_session(chat_id, sid, new_cwd, resume=True)
    store.update_binding(chat_id, cwd=new_cwd)
    return f"cwd → {new_cwd} (session resumed)"


def _cmd_effort(args, chat_id, chat, bot):
    levels = ["low", "medium", "high", "xhigh", "max"]
    if not args:
        binding = store.get_binding(chat_id)
        return f"effort: {binding.get('effort', '(default)') if binding else '(default)'}"
    level = args.split()[0].lower()
    if level not in levels:
        return f"unknown effort: {level}\navailable: {', '.join(levels)}"
    binding = store.get_binding(chat_id)
    if not binding:
        return "no session bound — use /new first"
    session = bot.session_manager.get(chat_id)
    sid = binding["session_id"]
    if session and session.alive:
        session.terminate()
    store.update_binding(chat_id, effort=level)
    bot.spawn_session(chat_id, sid, binding["cwd"], resume=True)
    return f"effort → {level} (session restarted)"


def _cmd_verbose(args, chat_id, chat, bot):
    renderer = bot.get_renderer(chat_id)
    if not renderer:
        return "no active session"
    if not args:
        renderer.verbose = not renderer.verbose
    else:
        renderer.verbose = args.lower() in ("on", "true", "1", "yes")
    state = "on" if renderer.verbose else "off"
    store.update_binding(chat_id, verbose=1 if renderer.verbose else 0)
    return f"verbose → {state}"


def _cmd_maxsessions(args, chat_id, chat, bot):
    if not args:
        return f"max concurrent sessions: {bot.session_manager.max_live}"
    try:
        n = int(args.split()[0])
    except ValueError:
        return "usage: /maxsessions <number>"
    if n < 1:
        return "minimum is 1"
    bot.session_manager.max_live = n
    return f"max concurrent sessions → {n} (until restart; edit config.toml to persist)"


def _cmd_usage(args, chat_id, chat, bot):
    binding = store.get_binding(chat_id)
    lines = []
    if binding:
        su = store.session_usage(binding["session_id"])
        turns = su.get("turns", 0)
        inp = su.get("total_input") or 0
        out = su.get("total_output") or 0
        dur = su.get("total_duration") or 0
        parts = [f"{turns} turns"]
        if inp or out:
            parts.append(f"{(inp + out) / 1000:.1f}k tok")
        if dur:
            parts.append(f"{dur / 1000:.0f}s")
        lines.append(f"session: {' · '.join(parts)}")
    session = bot.session_manager.get(chat_id)
    if session and session.alive:
        ok, usage, _ = control_ok(session.control("get_context_usage"))
        if ok:
            tokens = usage.get("totalTokens", 0)
            max_t = usage.get("maxTokens") or 1
            pct = tokens / max_t * 100
            lines.append(f"context: {tokens / 1000:.1f}k / {max_t / 1000:.0f}k ({pct:.0f}%)")
    tu = store.today_usage()
    t_turns = tu.get("turns", 0)
    if t_turns:
        t_tok = ((tu.get("total_input") or 0) + (tu.get("total_output") or 0)) / 1000
        lines.append(f"today: {t_turns} turns · {t_tok:.1f}k tok")
    wu = store.week_usage()
    w_turns = wu.get("turns", 0)
    w_sessions = wu.get("sessions", 0)
    if w_turns:
        w_tok = ((wu.get("total_input") or 0) + (wu.get("total_output") or 0)) / 1000
        w_dur = (wu.get("total_duration") or 0) / 1000 / 60
        lines.append(f"this week: {w_sessions} sessions · {w_turns} turns · {w_tok:.1f}k tok · {w_dur:.0f}m")
    return "\n".join(lines)


def _cmd_help(args, chat_id, chat, bot):
    lines = [
        "agentbot commands:",
        "",
        "session:",
        "  /new [dir]         — fresh session",
        "  /clear             — restart session, same cwd",
        "  /exit              — end session, show resume routes",
        "  /resume <id>       — resume a session",
        "  /sessions          — list bound chats + recent",
        "  /stop              — interrupt running turn",
        "",
        "settings:",
        "  /model [name|default] — show/set model (default = use settings files)",
        "  /mode [name]       — show/cycle/set permission mode",
        "  /cwd [path]        — show/change working directory",
        "  /effort [level]    — show/set effort level",
        "  /verbose [on|off]  — toggle tool/thinking visibility",
        "  /maxsessions [n]   — show/set max concurrent sessions",
        "",
        "info:",
        "  /usage             — cost and context stats",
        "  /commission <name> [dir] — new chat for a project",
        "  /help              — this message",
        "",
        "any other /command is passed through to Claude Code.",
    ]
    session = bot.session_manager.get(chat_id)
    if session and session.init_data:
        cmds = _get_init_list(session, "commands")
        if cmds:
            lines.append("")
            lines.append("claude code commands:")
            for c in cmds[:20]:
                name = c if isinstance(c, str) else c.get("name", c.get("value", "?"))
                lines.append(f"  /{name}")
    return "\n".join(lines)


def _cmd_commission(args, chat_id, chat, bot):
    if not args:
        return "usage: /commission <name> [dir]"
    parts = args.split(None, 1)
    name = parts[0]
    cwd = parts[1] if len(parts) > 1 else bot.config["default_cwd"]
    cwd = os.path.expanduser(cwd)
    if not _check_root(cwd, bot):
        return f"path not under allowed_roots: {cwd}"
    if not os.path.isdir(cwd):
        return f"directory not found: {cwd}"
    try:
        account = chat.account
        group = account.create_group(name)
        members = [c for c in chat.get_contacts()]
        if members:
            group.add_contact(*members)
        try:
            avatar_path = Path(bot.bot_dir) / "avatars" / f"{name}.png"
            avatar_path.parent.mkdir(exist_ok=True)
            make_avatar(avatar_path)
            group.set_image(str(avatar_path))
        except Exception:
            log.warning("avatar for group %r failed", name, exc_info=True)
        group_chat_id = group.id
        session_id = str(uuid.uuid4())
        store.set_binding(group_chat_id, session_id, cwd,
                          bot.config["default_model"],
                          bot.config["default_permission_mode"],
                          name=name)
        group.send_text(f"📂 {name} — {cwd}\nsession {session_id}\nsend a message to start")
        return f"created group '{name}' for {cwd}"
    except Exception as e:
        log.exception("commission failed")
        return f"commission failed: {e}"


def _check_root(path: str, bot) -> bool:
    path = os.path.realpath(path)
    for root in bot.config.get("allowed_roots", []):
        if path == root or path.startswith(root + "/"):
            return True
    return False


def _get_init_list(session, key: str) -> list:
    if not session.init_data:
        return []
    return session.init_data.get(key, [])


def _scan_recent_sessions() -> list[tuple[str, str]]:
    base = os.path.expanduser("~/.claude/projects")
    results = []
    try:
        for f in sorted(glob.glob(f"{base}/**/*.jsonl", recursive=True),
                        key=os.path.getmtime, reverse=True)[:20]:
            sid = os.path.basename(f).removesuffix(".jsonl")
            proj = os.path.dirname(f).removeprefix(base + "/")
            results.append((sid, proj))
    except Exception:
        pass
    return results


HANDLERS = {
    "new": _cmd_new,
    "clear": _cmd_clear,
    "exit": _cmd_exit,
    "resume": _cmd_resume,
    "sessions": _cmd_sessions,
    "stop": _cmd_stop,
    "model": _cmd_model,
    "mode": _cmd_mode,
    "cwd": _cmd_cwd,
    "effort": _cmd_effort,
    "verbose": _cmd_verbose,
    "usage": _cmd_usage,
    "cost": _cmd_usage,
    "help": _cmd_help,
    "commission": _cmd_commission,
    "maxsessions": _cmd_maxsessions,
}
