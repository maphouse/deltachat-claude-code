import logging
import os
import re
import shutil
import sqlite3
import threading
import tomllib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deltachat_rpc_client import Client, DeltaChat, Rpc, events
from deltachat_rpc_client.events import EventType

from . import annotate, commands, store

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


# The agentbot-wide system prompt every session gets; config.toml's `preamble`
# replaces it. Chat name/sharing facts and the chat prompt are added after it.
DEFAULT_PREAMBLE = """\
You are being used through agentbot: the user talks to you over Delta Chat (an \
encrypted messenger), often from a phone. Each of your text replies is sent as a \
chat message, so keep them concise; light markdown is fine but avoid wide tables \
and long code dumps unless asked.
Files the user sends are saved under .agentbot-inbox/ in the working directory and \
appear as [attached: <path>]; voice memos arrive already transcribed. You can't \
send files yourself — the user can fetch one with /send <path>.
The user may reset this conversation with /clear; anything that should outlive it \
belongs in files (e.g. the project's CLAUDE.md)."""


class AgentBot:
    def __init__(self):
        self.bot_dir = BOT_DIR
        self.account = None
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
        # chat -> review whose notes Claude saw last; a bare <revision> goes there
        self._active_review: dict[int, int] = {}

    def _load_config(self) -> dict:
        with open(BOT_DIR / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
        # an empty default_model means "don't pass --model at all", letting the
        # cwd's .claude/settings*.json decide; None is how that travels
        cfg["default_model"] = cfg.get("default_model") or None
        cfg.setdefault("preamble", DEFAULT_PREAMBLE)
        cfg.setdefault("admin_fingerprints", [])
        cfg.setdefault("admin_addresses", [])
        return cfg

    def _resolve_admin_ids(self):
        """Build the set of admin contact IDs from config. Called once after
        the account is available.

        Multi-transport means a single person can appear with different relay
        addresses (and thus different DC contact IDs). We collect admin
        fingerprints from config, then find every contact in the DB that
        shares one of those fingerprints — covering all relay addresses."""
        admin_fps = set(self.config["admin_fingerprints"])
        self._admin_contact_ids: set[int] = set()
        self._admin_fingerprints: set[str] = set(admin_fps)

        for addr in self.config["admin_addresses"]:
            try:
                cid = self.account._rpc.create_contact(self.account.id, addr, "")
                if cid:
                    self._admin_contact_ids.add(cid)
            except Exception:
                log.warning("could not resolve admin address %s", addr, exc_info=True)

        # Find all contacts that share an admin fingerprint (covers relays)
        if admin_fps:
            try:
                db_path = Path(ACCOUNTS_DIR).glob("*/dc.db")
                for db in db_path:
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                    c = conn.cursor()
                    placeholders = ",".join("?" * len(admin_fps))
                    c.execute(
                        f"SELECT id, addr, fingerprint FROM contacts "
                        f"WHERE fingerprint IN ({placeholders})",
                        list(admin_fps),
                    )
                    for cid, addr, fp in c.fetchall():
                        self._admin_contact_ids.add(cid)
                        log.info("admin fingerprint %s...%s -> contact %d (%s)",
                                 fp[:8], fp[-8:], cid, addr)
                    conn.close()
                    break
            except Exception:
                log.warning("fingerprint DB lookup failed", exc_info=True)

        log.info("admin contact IDs: %s", self._admin_contact_ids)

    def _is_owner(self, contact_snapshot) -> bool:
        """Check whether a contact is an admin — by contact ID first (covers
        relay addresses resolved at startup), then address fallback."""
        contact = contact_snapshot.get("contact")
        if contact and contact.id in self._admin_contact_ids:
            return True
        addr = contact_snapshot.get("address", "")
        if addr in self.config["admin_addresses"]:
            return True
        # New relay contact not seen at startup — check its fingerprint live
        if contact and self._admin_fingerprints:
            try:
                info = contact.get_encryption_info()
                for fp in self._admin_fingerprints:
                    if fp.lower() in info.lower():
                        self._admin_contact_ids.add(contact.id)
                        log.info("late-resolved admin: contact %d (%s) via fingerprint",
                                 contact.id, addr)
                        return True
            except Exception:
                pass
        return False

    def _contact_is_owner(self, contact) -> bool:
        """Convenience: takes a Contact object instead of a snapshot."""
        if contact.id in self._admin_contact_ids:
            return True
        return self._is_owner(contact.get_snapshot())

    def get_renderer(self, chat_id: int) -> ChatRenderer | None:
        return self._renderers.get(chat_id)

    def spawn_session(self, chat_id: int, session_id: str, cwd: str,
                      resume: bool = False) -> Session:
        binding = store.get_binding(chat_id)
        model = binding.get("model", self.config["default_model"]) if binding else self.config["default_model"]
        perm = binding.get("permission_mode", self.config["default_permission_mode"]) if binding else self.config["default_permission_mode"]
        effort = binding.get("effort") if binding else None
        system_prompt = self.system_prompt(chat_id)

        renderer = self._renderers.get(chat_id)
        if not renderer:
            return None

        def on_event(event):
            etype = event.get("type")
            if etype == "assistant":
                self._post_revisions(chat_id, event)
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
            resume=resume, on_event=on_event, system_prompt=system_prompt,
        )
        self.session_manager.register(chat_id, session)
        return session

    def system_prompt(self, chat_id: int) -> str:
        """What goes in --append-system-prompt: the agentbot-wide preamble, a few
        facts about this chat, then the chat prompt. Layers on top of the cwd's
        CLAUDE.md, never replaces it."""
        binding = store.get_binding(chat_id)
        name = binding.get("name") if binding else None
        parts = [self.config["preamble"].strip()]
        facts = []
        if name:
            facts.append(f"This chat is named \"{name}\".")
        if store.is_shared(chat_id):
            facts.append("This chat is shared: messages may come from people other than "
                         "the owner, and you can't tell who sent which.")
        if facts:
            parts.append(" ".join(facts))
        chat_prompt = store.get_chat_prompt(chat_id)
        if chat_prompt:
            parts.append("System prompt for this chat, set by its owner:\n" + chat_prompt)
        return "\n\n".join(parts)

    def update_chat_description(self, chat, session_id: str, cwd: str):
        desc = f"session: {session_id}\ncwd: {cwd}\nresume: claude --resume {session_id}"
        chat_prompt = store.get_chat_prompt(chat.id)
        if chat_prompt:
            desc = f"{chat_prompt}\n\n{desc}"
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

    def _post_revisions(self, chat_id: int, event: dict):
        """Move <revision> blocks out of Claude's reply and into their review
        apps, leaving a one-line pointer in the chat text."""
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "text" or "<revision" not in block.get("text", ""):
                continue
            text, found = annotate.extract_revisions(block["text"],
                                                   self._active_review.get(chat_id))
            for i, (rid, revised) in enumerate(found):
                rv = store.get_review(rid)
                if rv and rv["chat_id"] == chat_id:
                    try:
                        v = annotate.post_revision(self.account, rv, revised)
                        note = f"📝 posted v{v} to annotation #{rid}"
                    except Exception:
                        log.exception("posting revision to review %d failed", rid)
                        note = revised
                else:
                    note = revised
                text = text.replace(f"\x00{i}\x00", note)
            block["text"] = text

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
        if binding:
            store.set_binding(chat_id, session_id, cwd, binding.get("model"),
                              binding.get("permission_mode") or self.config["default_permission_mode"],
                              effort=binding.get("effort"), name=binding.get("name"))
        else:
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
        return any(self._contact_is_owner(c) for c in chat.get_contacts())

    def handle_message(self, event):
        snapshot = event.message_snapshot
        sender_snapshot = snapshot.sender.get_snapshot()
        sender = sender_snapshot.address
        text = (snapshot.text or "").strip()
        chat = snapshot.chat
        chat_id = chat.id

        owner = self._is_owner(sender_snapshot)
        if not owner and not self._guest_allowed(chat):
            log.warning("ignoring %s in chat %d (not an owner, chat not shared)",
                        sender, chat_id)
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
        quoted_id = getattr(quote_obj, "message_id", None) if quote_obj else None

        if snapshot.file:
            text = self._handle_attachment(chat_id, snapshot, text)
            if not text:
                renderer.react_done()
                return

        parsed = commands.classify(text) if text else None
        if parsed:
            cmd, args = parsed
            result = commands.handle(cmd, args, chat_id, chat, self, owner=owner,
                                     quoted=quoted, quoted_id=quoted_id, sender=sender)
            if result is not None:
                if result:
                    chat.send_text(result)
                renderer.react_done()
                return

        if quoted and text:
            text = f"[replying to: \"{quoted}\"]\n{text}"

        if text:
            if text.startswith("!"):
                renderer._show_bash_output = True
            self._deliver(chat_id, chat, text)

    def _deliver(self, chat_id: int, chat, text: str) -> bool:
        """Hand a user turn to the chat's session, starting or reviving it."""
        renderer = self._ensure_renderer(chat_id, chat)
        session = self._ensure_session(chat_id, chat)
        if not session:
            chat.send_text("failed to start session — check logs")
            renderer.react_done(error=True)
            return False

        if not session.alive:
            binding = store.get_binding(chat_id)
            if binding:
                session = self.spawn_session(
                    chat_id, binding["session_id"], binding["cwd"], resume=True,
                )
            if not session or not session.alive:
                chat.send_text("session died — try /new or /clear")
                renderer.react_done(error=True)
                return False

        session.send_user(text)
        return True

    def quoted_text(self, chat_id: int, msg_id: int | None, fallback: str | None) -> str | None:
        """The full text of a quoted message: the whole reply if it was one
        chunk of a split reply, else the message itself, else the quote."""
        if msg_id:
            renderer = self._renderers.get(chat_id)
            full = renderer.reply_text(msg_id) if renderer else None
            if full:
                return full
            try:
                from deltachat_rpc_client import Message
                text = Message(self.account, msg_id).get_snapshot().text
                if text and text.strip():
                    return text.strip()
            except Exception:
                log.debug("could not load quoted message %s", msg_id, exc_info=True)
        return fallback

    def _review_trusted(self, chat) -> bool:
        """Webxdc updates don't say who sent them, so notes are only taken from
        chats where anyone who could have sent them may use the bot: every
        member an owner, or a shared chat still holding an owner."""
        if store.is_shared(chat.id):
            return self._guest_allowed(chat)
        me = self.account.get_config("addr")
        for c in chat.get_contacts():
            snap = c.get_snapshot()
            if snap.address == me:
                continue
            if not self._is_owner(snap):
                return False
        return True

    def handle_webxdc_update(self, event):
        rv = store.get_review_by_msg(event.msg_id)
        if not rv:
            return
        batches = annotate.collect_notes(self.account, rv)
        if not batches:
            return
        chat = self.account.get_chat_by_id(rv["chat_id"])
        if not self._review_trusted(chat):
            log.warning("ignoring review notes in chat %d (untrusted members)", chat.id)
            return
        binding = store.get_binding(chat.id)
        same_session = binding and binding["session_id"] == rv["session_id"]
        for batch in batches:
            log.info("review #%d: %d notes in chat %d", rv["id"], len(batch["notes"]), chat.id)
            self._cancel_continue(chat.id)
            renderer = self._ensure_renderer(chat.id, chat)
            renderer.set_inbound(rv["msg_id"])
            renderer.react_receipt()
            self._active_review[chat.id] = rv["id"]
            text = annotate.format_notes(rv, batch, include_text=not same_session)
            if self._deliver(chat.id, chat, text):
                annotate.ack(self.account, rv, batch.get("id", ""))

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
            self.account = account
            self._resolve_admin_ids()
            client = Client(
                account,
                hooks=[(self.handle_message, events.NewMessage(is_info=False)),
                       (self.handle_webxdc_update,
                        events.RawEvent(EventType.WEBXDC_STATUS_UPDATE))],
            )
            client.run_forever()


def main():
    bot = AgentBot()
    bot.run()


if __name__ == "__main__":
    main()
