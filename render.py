import logging
import threading
import time

log = logging.getLogger("agentbot.render")

TOOL_ICONS = {
    "Bash": "\U0001f4bb",
    "Read": "\U0001f4c4",
    "Write": "✏️",
    "Edit": "✏️",
    "Grep": "\U0001f50d",
    "Glob": "\U0001f50d",
    "Agent": "\U0001f916",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "TaskCreate": "\U0001f4cb",
    "TaskUpdate": "\U0001f4cb",
    "Artifact": "\U0001f4e6",
}
DEFAULT_TOOL_ICON = "\U0001f527"

MAX_MSG_LEN = 4000
MAX_TOOL_OUTPUT = 12000
BUFFER_FLUSH_LINES = 10
BUFFER_FLUSH_SECONDS = 10.0


class ChatRenderer:
    def __init__(self, chat, verbose: bool = True):
        self.chat = chat
        self.verbose = verbose
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._flush_timer = None
        self._inbound_msg_id = None
        self._reacted_receipt = False
        self._reacted_done = False
        self._turn_start = None
        self._turn_tool_count = 0
        self._pending_tools: dict[str, str] = {}
        self._show_bash_output = False

    def set_inbound(self, msg_id: int):
        self._inbound_msg_id = msg_id
        self._reacted_receipt = False
        self._reacted_done = False
        self._turn_start = time.monotonic()
        self._turn_tool_count = 0

    def react_receipt(self):
        if self._inbound_msg_id and not self._reacted_receipt:
            try:
                self.chat.account.send_reaction(self._inbound_msg_id, ["⏳"])
                self._reacted_receipt = True
            except Exception:
                log.debug("reaction send failed")

    def react_done(self, error: bool = False):
        if self._inbound_msg_id:
            emoji = "❌" if error else "✅"
            try:
                self.chat.account.send_reaction(self._inbound_msg_id, [emoji])
                self._reacted_done = True
            except Exception:
                log.debug("reaction send failed")

    def handle_event(self, event: dict):
        etype = event.get("type")

        if etype == "assistant":
            self._handle_assistant(event)
        elif etype == "user":
            self._handle_tool_result(event)
        elif etype == "result":
            self._handle_result(event)

    def _handle_assistant(self, event: dict):
        message = event.get("message", {})
        content_blocks = message.get("content", [])

        for block in content_blocks:
            btype = block.get("type")

            if btype == "text":
                text = block.get("text", "").strip()
                if text:
                    self._flush_buffer()
                    self._send_text(text)

            elif btype == "thinking":
                if self.verbose:
                    text = block.get("thinking", "").strip()
                    if text:
                        short = text[:200] + ("..." if len(text) > 200 else "")
                        self._append_buffer(f"\U0001f4ad {short}")

            elif btype == "tool_use":
                self._turn_tool_count += 1
                tool_name = block.get("name", "?")
                tool_id = block.get("id", "")
                if tool_id:
                    self._pending_tools[tool_id] = tool_name
                tool_input = block.get("input", {})
                icon = TOOL_ICONS.get(tool_name, DEFAULT_TOOL_ICON)
                desc = _tool_summary(tool_name, tool_input)
                self._append_buffer(f"{icon} {tool_name} · {desc}")

    def _handle_tool_result(self, event: dict):
        message = event.get("message", {})
        content_blocks = message.get("content", [])

        for block in content_blocks:
            if block.get("type") != "tool_result":
                continue

            tool_id = block.get("tool_use_id", "")
            tool_name = self._pending_tools.pop(tool_id, "")

            if not self._show_bash_output:
                continue
            if tool_name not in ("Bash", "Read"):
                continue

            content = block.get("content", "")
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part.get("text", ""))
                content = "\n".join(parts)

            if not content or not content.strip():
                continue

            is_error = block.get("is_error", False)

            if len(content) > MAX_TOOL_OUTPUT:
                content = content[:MAX_TOOL_OUTPUT] + "\n… (truncated)"

            self._flush_buffer()
            if is_error:
                self._send_text(f"⚠️ {content}")
            else:
                self._send_text(content)

    def _handle_result(self, event: dict):
        self._show_bash_output = False
        self._flush_buffer()
        duration_ms = event.get("duration_ms")
        usage = event.get("usage", {})
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        ctx = usage.get("context_tokens")
        ctx_max = usage.get("context_max_tokens")

        parts = []
        if self._turn_tool_count:
            parts.append(f"{self._turn_tool_count} tools")
        if duration_ms:
            parts.append(f"{duration_ms / 1000:.0f}s")
        if inp or out:
            parts.append(f"{(inp + out) / 1000:.1f}k tok")
        if ctx and ctx_max:
            pct = ctx / ctx_max * 100
            parts.append(f"{ctx / 1000:.1f}k ctx ({pct:.0f}%)")

        footer = "✅ " + " · ".join(parts) if parts else "✅ done"
        self._send_text(footer)
        self.react_done()

    def _append_buffer(self, line: str):
        with self._lock:
            self._buffer.append(line)
            if len(self._buffer) >= BUFFER_FLUSH_LINES:
                self._do_flush()
            elif not self._flush_timer:
                self._flush_timer = threading.Timer(BUFFER_FLUSH_SECONDS, self._timer_flush)
                self._flush_timer.daemon = True
                self._flush_timer.start()

    def _timer_flush(self):
        with self._lock:
            self._do_flush()

    def _flush_buffer(self):
        with self._lock:
            self._do_flush()

    def _do_flush(self):
        if self._flush_timer:
            self._flush_timer.cancel()
            self._flush_timer = None
        if self._buffer:
            text = "\n".join(self._buffer)
            self._buffer.clear()
            self._send_text_unlocked(text)

    def _send_text(self, text: str):
        with self._lock:
            self._send_text_unlocked(text)

    def _send_text_unlocked(self, text: str):
        for chunk in _split_text(text, MAX_MSG_LEN):
            try:
                self.chat.send_text(chunk)
            except Exception:
                log.exception("failed to send message")


def _split_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _tool_summary(name: str, inp: dict) -> str:
    if name == "Bash":
        cmd = inp.get("command", "")
        return cmd[:80] + ("..." if len(cmd) > 80 else "")
    if name in ("Read", "Write", "Edit"):
        return inp.get("file_path", "?").split("/")[-1]
    if name in ("Grep", "Glob"):
        return inp.get("pattern", inp.get("query", "?"))[:60]
    if name == "Agent":
        return inp.get("description", inp.get("name", "?"))[:60]
    if name in ("WebFetch", "WebSearch"):
        return inp.get("url", inp.get("query", "?"))[:60]
    if name in ("TaskCreate", "TaskUpdate"):
        return inp.get("subject", inp.get("taskId", "?"))[:60]
    return str(inp)[:60]
