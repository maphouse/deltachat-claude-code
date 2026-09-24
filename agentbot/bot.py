import logging
import os
import re
import shutil
import threading
import tomllib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deltachat_rpc_client import Client, DeltaChat, Rpc, events
from deltachat_rpc_client.events import EventType

from . import commands, store

# The RPC server may emit event types newer than the Python client knows about
# (e.g. IncomingWebxdcNotify). Patch the event loop to skip unknown types
# instead of crashing.
_original_process_events = Client._process_events

def _safe_process_events(self, until_func=None, until_event=False):
    if until_func is None:
        until_func = lambda e: False
    while True:
        event = self.account.wait_for_event()
        try:
            event["kind"] = EventType(event.kind)
        except ValueError:
            continue
        event["account"] = self.account
        self._on_event(event)
        if event.kind == EventType.INCOMING_MSG:
            self._process_messages()
        if until_func(event):
            return event
        if event.kind == until_event:
            return event

Client._process_events = _safe_process_events
from .render import ChatRenderer
from .session import Session, SessionManager
from .transcribe import transcribe, NOT_INSTALLED

BOT_DIR = Path.cwd()
ACCOUNTS_DIR = str(BOT_DIR / "accounts")


def _find_rpc_server() -> str:
    found = shutil.which("deltachat-rpc-server")
    if found:
        return found
    import sys
    venv_candidate = Path(sys.executable).parent / "deltachat-rpc-server"
    if venv_candidate.is_file():
        return str(venv_candidate)
    return "deltachat-rpc-server"


RPC_SERVER_PATH = _find_rpc_server()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("agentbot")


RESET_TIME_RE = re.compile(
    r"resets?\s+(\d{1,2}:\d{2}\s*(?:am|pm))\s*\(UTC\)",
    re.IGNORECASE,
)


class AgentBot:
    def __init__(self):
        self.bot_dir = BOT_DIR
        self.config = self._load_config()
        store.init_db()
        self.session_manager = SessionManager(
            max_live=self.config["max_live_sessions"],
            idle_timeout_min=self.config["idle_timeout_min"],
        )
        self._renderers: dict[int, ChatRenderer] = {}
        saved = store.get_setting("continue_after_reset")
        self.continue_after_reset = (saved == "1") if saved is not None else self.config.get("continue_after_reset", False)
        self._continue_timers: dict[int, threading.Timer] = {}
        self._rate_limited_chats: set[int] = set()

    def _load_config(self) -> dict:
        with open(BOT_DIR / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
        # an empty default_model means "don't pass --model at all", letting the
        # cwd's .claude/settings*.json decide; None is how that travels
        cfg["default_model"] = cfg.get("default_model") or None
        return cfg

    def get_renderer(self, chat_id: int) -> ChatRenderer | None:
        return self._renderers.get(chat_id)

    def spawn_session(self, chat_id: int, session_id: str, cwd: str,
                      resume: bool = False) -> Session:
        binding = store.get_binding(chat_id)
        model = binding.get("model", self.config["default_model"]) if binding else self.config["default_model"]
        perm = binding.get("permission_mode", self.config["default_permission_mode"]) if binding else self.config["default_permission_mode"]
        effort = binding.get("effort") if binding else None

        renderer = self._renderers.get(chat_id)
        if not renderer:
            return None

        def on_event(event):
            etype = event.get("type")
            if etype in ("assistant", "result", "user"):
                renderer.handle_event(event)
            if etype == "result":
                self._record_result(chat_id, session_id, event)
                self._log_rate_limit_result(chat_id, event)
            if etype == "assistant":
                self._check_rate_limit(chat_id, event)

        session = Session(
            session_id=session_id, cwd=cwd, model=model,
            permission_mode=perm, effort=effort,
            resume=resume, on_event=on_event,
        )
        self.session_manager.register(chat_id, session)
        return session

    def update_chat_description(self, chat, session_id: str, cwd: str):
        desc = f"session: {session_id}\ncwd: {cwd}\nresume: claude --resume {session_id}"
        try:
            chat._rpc.set_chat_description(chat.account.id, chat.id, desc)
        except Exception:
            log.debug("could not set chat description (1:1 chat?)", exc_info=True)

    def _record_result(self, chat_id: int, session_id: str, event: dict):
        store.record_usage(
            chat_id=chat_id,
            session_id=session_id,
            cost_usd=event.get("total_cost_usd"),
            input_tokens=event.get("usage", {}).get("input_tokens"),
            output_tokens=event.get("usage", {}).get("output_tokens"),
            cache_read=event.get("usage", {}).get("cache_read_input_tokens"),
            cache_creation=event.get("usage", {}).get("cache_creation_input_tokens"),
            num_turns=event.get("num_turns"),
            duration_ms=event.get("duration_ms"),
        )

    def _check_rate_limit(self, chat_id: int, event: dict):
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "text":
                continue
            m = RESET_TIME_RE.search(block.get("text", ""))
            if m:
                self._rate_limited_chats.add(chat_id)
                if self.continue_after_reset:
                    self._schedule_continue(chat_id, m.group(1))
                return

    def _log_rate_limit_result(self, chat_id: int, event: dict):
        if chat_id not in self._rate_limited_chats:
            return
        self._rate_limited_chats.discard(chat_id)
        log.info("rate-limited result event for chat %d: %s", chat_id,
                 {k: v for k, v in event.items() if k != "message"})

    def _schedule_continue(self, chat_id: int, reset_time_str: str):
        self._cancel_continue(chat_id)
        now = datetime.now(timezone.utc)
        time_str = reset_time_str.strip().lower().replace(" ", "")
        for fmt in ("%I:%M%p", "%H:%M"):
            try:
                parsed = datetime.strptime(time_str, fmt)
                break
            except ValueError:
                continue
        else:
            log.warning("could not parse reset time: %s", reset_time_str)
            return
        reset = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        if reset <= now:
            reset += timedelta(days=1)
        delay = (reset - now).total_seconds() + 60
        log.info("auto-continue for chat %d in %.0fs (reset %s UTC)",
                 chat_id, delay, reset.strftime("%H:%M"))
        timer = threading.Timer(delay, self._do_continue, args=(chat_id,))
        timer.daemon = True
        timer.start()
        self._continue_timers[chat_id] = timer
        renderer = self._renderers.get(chat_id)
        if renderer:
            renderer._send_text(
                f"⏰ will auto-continue at {reset.strftime('%H:%M')} UTC + 1 min"
            )

    def _do_continue(self, chat_id: int):
        self._continue_timers.pop(chat_id, None)
        log.info("auto-continuing chat %d after rate limit reset", chat_id)
        renderer = self._renderers.get(chat_id)
        if not renderer:
            log.warning("auto-continue: no renderer for chat %d", chat_id)
            return
        session = self.session_manager.get(chat_id)
        if not session:
            binding = store.get_binding(chat_id)
            if binding:
                session = self.spawn_session(
                    chat_id, binding["session_id"], binding["cwd"], resume=True,
                )
        if session and session.alive:
            renderer._send_text("⏰ auto-continuing after rate limit reset")
            session.send_user("continue where you left off")
        else:
            renderer._send_text("⏰ auto-continue failed — session not available")

    def _cancel_continue(self, chat_id: int):
        timer = self._continue_timers.pop(chat_id, None)
        if timer:
            timer.cancel()

    def _ensure_session(self, chat_id: int, chat) -> Session | None:
        session = self.session_manager.get(chat_id)
        if session:
            store.touch_binding(chat_id)
            return session

        binding = store.get_binding(chat_id)
        if binding:
            session = self.spawn_session(
                chat_id, binding["session_id"], binding["cwd"], resume=True,
            )
            if session and session.wait_ready(timeout=10.0):
                return session
            log.warning("resume failed for session %s, starting fresh", binding["session_id"][:8])
            self.session_manager.remove(chat_id)

        cwd = binding["cwd"] if binding else self.config["default_cwd"]
        session_id = str(uuid.uuid4())
        had_binding = binding is not None
        store.set_binding(chat_id, session_id, cwd,
                          self.config["default_model"],
                          self.config["default_permission_mode"])
        if not had_binding:
            self.update_chat_description(chat, session_id, cwd)
        return self.spawn_session(chat_id, session_id, cwd)

    def _ensure_renderer(self, chat_id: int, chat) -> ChatRenderer:
        renderer = self._renderers.get(chat_id)
        if not renderer:
            binding = store.get_binding(chat_id)
            verbose = bool(binding.get("verbose", self.config["verbose_tools"])) if binding else self.config["verbose_tools"]
            renderer = ChatRenderer(chat, verbose=verbose)
            self._renderers[chat_id] = renderer
        return renderer

    def _guest_allowed(self, chat) -> bool:
        """A non-owner may use the bot only in a chat an owner has shared, and
        only while at least one owner is still a member of it."""
        if not store.is_shared(chat.id):
            return False
        owners = set(self.config["admin_addresses"])
        return any(c.get_snapshot().address in owners for c in chat.get_contacts())

    def handle_message(self, event):
        snapshot = event.message_snapshot
        sender = snapshot.sender.get_snapshot().address
        text = (snapshot.text or "").strip()
        chat = snapshot.chat
        chat_id = chat.id

        owner = sender in self.config["admin_addresses"]
        if not owner and not self._guest_allowed(chat):
            # Stay silent: replying would spam groups the bot wasn't shared into.
            log.warning("ignoring %s in chat %d (not an owner, chat not shared)", sender, chat_id)
            return

        if not text and not snapshot.file:
            return

        self._cancel_continue(chat_id)

        log.info("message from %s in chat %d: %r", sender, chat_id, text[:100])

        renderer = self._ensure_renderer(chat_id, chat)
        renderer.set_inbound(snapshot.id)
        renderer.react_receipt()

        quote_obj = getattr(snapshot, "quote", None)
        quoted = quote_obj.text.strip() if quote_obj and getattr(quote_obj, "text", None) else None

        if snapshot.file:
            text = self._handle_attachment(chat_id, snapshot, text)
            if not text:
                renderer.react_done()
                return

        parsed = commands.classify(text) if text else None
        if parsed:
            cmd, args = parsed
            result = commands.handle(cmd, args, chat_id, chat, self, owner=owner,
                                     quoted=quoted, sender=sender)
            if result is not None:
                if result:
                    chat.send_text(result)
                renderer.react_done()
                return

        if quoted and text:
            text = f"[replying to: \"{quoted}\"]\n{text}"

        session = self._ensure_session(chat_id, chat)
        if not session:
            chat.send_text("failed to start session — check logs")
            renderer.react_done(error=True)
            return

        if not session.alive:
            binding = store.get_binding(chat_id)
            if binding:
                session = self.spawn_session(
                    chat_id, binding["session_id"], binding["cwd"], resume=True,
                )
            if not session or not session.alive:
                chat.send_text("session died — try /new or /clear")
                renderer.react_done(error=True)
                return

        if text:
            if text.startswith("!"):
                renderer._show_bash_output = True
            session.send_user(text)

    def _handle_attachment(self, chat_id: int, snapshot, text: str) -> str:
        binding = store.get_binding(chat_id)
        cwd = binding["cwd"] if binding else self.config["default_cwd"]
        inbox = Path(cwd) / ".agentbot-inbox"
        inbox.mkdir(exist_ok=True)
        src = snapshot.file
        dst = inbox / Path(src).name
        counter = 1
        while dst.exists():
            dst = inbox / f"{Path(src).stem}_{counter}{Path(src).suffix}"
            counter += 1
        shutil.copy2(src, dst)
        log.info("saved attachment to %s", dst)

        AUDIO_EXTS = {".ogg", ".mp3", ".wav", ".m4a", ".flac", ".opus", ".webm"}
        if dst.suffix.lower() in AUDIO_EXTS:
            transcript = transcribe(dst)
            if transcript and transcript != NOT_INSTALLED:
                log.info("transcribed voice memo: %s", transcript[:100])
                snapshot.chat.send_text(f"🎤 {transcript}")
                prefix = f"[voice memo transcription]\n{transcript}"
                return (prefix + "\n\n" + text) if text else prefix
            if transcript == NOT_INSTALLED:
                snapshot.chat.send_text(
                    "🎤 Voice memo received but I can't transcribe it — "
                    "faster-whisper is not installed.\n\n"
                    "To enable voice transcription, install it in the bot's venv:\n"
                    "  pip install -r requirements-voice.txt\n\n"
                    "Then restart the bot. In the meantime, please type your message."
                )
            else:
                snapshot.chat.send_text(
                    "🎤 Voice memo received but transcription failed. "
                    "Please type your message instead."
                )
            return text or ""

        suffix = f"\n[attached: {dst}]"
        return (text + suffix) if text else f"[attached: {dst}]"

    def run(self):
        log.info("starting agentbot")
        with Rpc(accounts_dir=ACCOUNTS_DIR, rpc_server_path=RPC_SERVER_PATH) as rpc:
            dc = DeltaChat(rpc)
            accounts = dc.get_all_accounts()
            if not accounts:
                log.error("no accounts — run provision.py first")
                return
            account = accounts[0]
            log.info("running as %s", account.get_config("addr"))
            client = Client(
                account,
                hooks=[(self.handle_message, events.NewMessage(is_info=False))],
            )
            client.run_forever()


def main():
    bot = AgentBot()
    bot.run()


if __name__ == "__main__":
    main()
