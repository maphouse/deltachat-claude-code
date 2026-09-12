import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("agentbot.session")


def _find_claude() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in [
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".claude" / "local" / "claude",
        Path("/usr/local/bin/claude"),
    ]:
        if candidate.is_file():
            return str(candidate)
    return "claude"


CLAUDE_BIN = _find_claude()


def control_ok(resp: dict | None) -> tuple[bool, dict, str]:
    """Unpack a control() return value into (success, payload, error).

    Responses look like {"type": "control_response", "response":
    {"subtype": "success"|"error", "request_id": ..., "response": {...} |
    "error": "..."}}.
    """
    if not resp:
        return False, {}, "no response (timed out or process died)"
    r = resp.get("response") or {}
    if r.get("subtype") == "success":
        return True, r.get("response") or {}, ""
    return False, {}, r.get("error") or json.dumps(resp)


class Session:
    def __init__(self, session_id: str, cwd: str, model: str,
                 permission_mode: str, effort: str = None,
                 resume: bool = False, on_event=None):
        self.session_id = session_id
        self.cwd = cwd
        self.model = model
        self.permission_mode = permission_mode
        self.effort = effort
        self.on_event = on_event
        self.proc = None
        self._reader_thread = None
        self._lock = threading.Lock()
        self._pending_controls: dict[str, threading.Event] = {}
        self._control_responses: dict[str, dict] = {}
        self._alive = False
        self.last_activity = time.monotonic()
        self.init_data = None
        self._start(resume=resume)

    def _build_cmd(self, resume: bool) -> list[str]:
        cmd = [
            CLAUDE_BIN, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", self.permission_mode,
        ]
        # no --model: let settings files pick, the same as running `claude` here
        if self.model:
            cmd.extend(["--model", self.model])
        if resume:
            cmd.extend(["--resume", self.session_id])
        else:
            cmd.extend(["--session-id", self.session_id])
        if self.effort:
            cmd.extend(["--effort", self.effort])
        return cmd

    def _start(self, resume: bool = False):
        cmd = self._build_cmd(resume)
        log.info("spawning: %s (cwd=%s)", " ".join(cmd), self.cwd)
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            cwd=self.cwd,
        )
        self._alive = True
        self._init_event = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name=f"reader-{self.session_id[:8]}",
        )
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, daemon=True, name=f"stderr-{self.session_id[:8]}",
        )
        self._stderr_thread.start()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Is the subprocess up and talking?

        The CLI emits system/init only after the first user message, so init is
        useless as a readiness signal — waiting on it made every --resume look
        like a failure. The control channel answers before init, and a resume of
        an unknown session id exits within ~1s, so probe that instead.
        """
        if self._init_event.is_set():
            return self.alive
        ok, _, _ = control_ok(self.control("get_context_usage", timeout=timeout))
        return ok and self.alive

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("non-json line from claude: %r", line[:200])
                    continue

                self.last_activity = time.monotonic()

                if event.get("type") == "system" and event.get("subtype") == "init":
                    self.init_data = event
                    log.info("session init: session_id=%s, model=%s",
                             event.get("session_id"), event.get("model"))
                    if event.get("session_id"):
                        self.session_id = event["session_id"]
                    self._init_event.set()

                elif event.get("type") == "control_response":
                    # the CLI nests request_id inside "response", not at top level
                    req_id = (event.get("request_id")
                              or (event.get("response") or {}).get("request_id"))
                    if req_id and req_id in self._pending_controls:
                        self._control_responses[req_id] = event
                        self._pending_controls[req_id].set()

                if self.on_event:
                    try:
                        self.on_event(event)
                    except Exception:
                        log.exception("on_event handler error")
        except Exception:
            log.exception("reader loop crashed")
        finally:
            self._alive = False
            log.info("reader loop ended for %s", self.session_id[:8])
            for ev in self._pending_controls.values():
                ev.set()

    def _stderr_loop(self):
        try:
            for line in self.proc.stderr:
                line = line.strip()
                if line:
                    log.debug("claude stderr: %s", line[:500])
        except Exception:
            pass

    @property
    def alive(self) -> bool:
        return self._alive and self.proc is not None and self.proc.poll() is None

    def send_user(self, text: str):
        msg = {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "session_id": "",
        }
        self._write(json.dumps(msg))

    def control(self, subtype: str, timeout: float = 10.0, **kwargs) -> dict | None:
        req_id = str(uuid.uuid4())
        request = {"subtype": subtype, **kwargs}
        msg = {
            "type": "control_request",
            "request_id": req_id,
            "request": request,
        }
        event = threading.Event()
        self._pending_controls[req_id] = event
        self._write(json.dumps(msg))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if event.wait(0.25):
                self._pending_controls.pop(req_id, None)
                return self._control_responses.pop(req_id, None)
            if not self.alive:
                break
        self._pending_controls.pop(req_id, None)
        if self.alive:
            log.warning("control request %s timed out", subtype)
        else:
            log.warning("control request %s: process not alive", subtype)
        return None

    def _write(self, line: str):
        with self._lock:
            if self.proc and self.proc.stdin and self.proc.poll() is None:
                try:
                    self.proc.stdin.write(line + "\n")
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    log.warning("write failed — process dead")
                    self._alive = False

    def interrupt(self):
        return self.control("interrupt")

    def terminate(self):
        self._alive = False
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        log.info("terminated session %s", self.session_id[:8])


class SessionManager:
    def __init__(self, max_live: int = 3, idle_timeout_min: int = 30):
        self.max_live = max_live
        self.idle_timeout = idle_timeout_min * 60
        self._sessions: dict[int, Session] = {}
        self._lock = threading.Lock()
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True, name="reaper")
        self._reaper.start()

    def get(self, chat_id: int) -> Session | None:
        with self._lock:
            s = self._sessions.get(chat_id)
            if s and s.alive:
                return s
            if s and not s.alive:
                del self._sessions[chat_id]
            return None

    def register(self, chat_id: int, session: Session):
        with self._lock:
            self._evict_if_needed()
            old = self._sessions.get(chat_id)
            if old and old.alive:
                old.terminate()
            self._sessions[chat_id] = session

    def remove(self, chat_id: int):
        with self._lock:
            s = self._sessions.pop(chat_id, None)
            if s and s.alive:
                s.terminate()

    def _evict_if_needed(self):
        live = {cid: s for cid, s in self._sessions.items() if s.alive}
        if len(live) < self.max_live:
            return
        oldest_cid = min(live, key=lambda c: live[c].last_activity)
        log.info("evicting session for chat %s (max_live=%d)", oldest_cid, self.max_live)
        live[oldest_cid].terminate()
        del self._sessions[oldest_cid]

    def _reap_loop(self):
        while True:
            time.sleep(60)
            now = time.monotonic()
            with self._lock:
                to_reap = [
                    cid for cid, s in self._sessions.items()
                    if s.alive and (now - s.last_activity) > self.idle_timeout
                ]
                for cid in to_reap:
                    log.info("idle-reaping session for chat %s", cid)
                    self._sessions[cid].terminate()
                    del self._sessions[cid]

    def all_live(self) -> dict[int, Session]:
        with self._lock:
            return {cid: s for cid, s in self._sessions.items() if s.alive}
