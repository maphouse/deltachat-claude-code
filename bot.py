import logging
import os
import shutil
import tomllib
import uuid
from pathlib import Path

from deltachat_rpc_client import Client, DeltaChat, Rpc, events

from . import commands, store
from .render import ChatRenderer
from .session import Session, SessionManager
from .transcribe import transcribe

BOT_DIR = Path(__file__).resolve().parent
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

        session = Session(
            session_id=session_id, cwd=cwd, model=model,
            permission_mode=perm, effort=effort,
            resume=resume, on_event=on_event,
        )
        self.session_manager.register(chat_id, session)
        return session

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
        store.set_binding(chat_id, session_id, cwd,
                          self.config["default_model"],
                          self.config["default_permission_mode"])
        return self.spawn_session(chat_id, session_id, cwd)

    def _ensure_renderer(self, chat_id: int, chat) -> ChatRenderer:
        renderer = self._renderers.get(chat_id)
        if not renderer:
            binding = store.get_binding(chat_id)
            verbose = bool(binding.get("verbose", self.config["verbose_tools"])) if binding else self.config["verbose_tools"]
            renderer = ChatRenderer(chat, verbose=verbose)
            self._renderers[chat_id] = renderer
        return renderer

    def handle_message(self, event):
        snapshot = event.message_snapshot
        sender = snapshot.sender.get_snapshot().address
        text = (snapshot.text or "").strip()
        chat = snapshot.chat
        chat_id = chat.id

        if sender not in self.config["admin_addresses"]:
            log.warning("unauthorized message from %s", sender)
            chat.send_text("not authorized")
            return

        if not text and not snapshot.file:
            return

        log.info("message from %s in chat %d: %r", sender, chat_id, text[:100])

        renderer = self._ensure_renderer(chat_id, chat)
        renderer.set_inbound(snapshot.id)
        renderer.react_receipt()

        if snapshot.file:
            text = self._handle_attachment(chat_id, snapshot, text)

        parsed = commands.classify(text) if text else None
        if parsed:
            cmd, args = parsed
            result = commands.handle(cmd, args, chat_id, chat, self)
            if result is not None:
                chat.send_text(result)
                renderer.react_done()
                return

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
            if transcript:
                log.info("transcribed voice memo: %s", transcript[:100])
                prefix = f"[voice memo transcription]\n{transcript}"
                return (prefix + "\n\n" + text) if text else prefix

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
                hooks=[(self.handle_message, events.NewMessage())],
            )
            client.run_forever()


def main():
    bot = AgentBot()
    bot.run()


if __name__ == "__main__":
    main()
