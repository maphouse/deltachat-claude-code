"""/review: one of Claude's messages goes into a webxdc app where the user
selects words or phrases and annotates them; the notes come back as a turn.

Protocol (webxdc status-update payloads):
  bot  → app  {type: "doc",   v, text, title}   a version of the text
  app  → bot  {type: "notes", id, v, notes: [{start, end, quote, comment}], by, name}
  bot  → app  {type: "ack",   id}               notes delivered to Claude
Claude posts a new version by wrapping it in <revision review="N">…</revision>.
"""
import json
import logging
import re
import tempfile
import zipfile
from pathlib import Path

from . import store

log = logging.getLogger("agentbot.review")

APP_DIR = Path(__file__).parent / "reviewer"

REVISION_RE = re.compile(
    r"<revision(?:\s+review=[\"']?#?(\d+)[\"']?)?\s*>\s*([\s\S]*?)\s*</revision>")


def build_xdc() -> Path:
    """Zip the app directory into a fresh .xdc; DC copies it into its blobdir."""
    fd, path = tempfile.mkstemp(suffix=".xdc", prefix="review-")
    with zipfile.ZipFile(open(fd, "wb"), "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(APP_DIR.iterdir()):
            if f.is_file():
                z.write(f, f.name)
    return Path(path)


def _title(text: str) -> str:
    first = next((l for l in text.splitlines() if l.strip()), "")
    first = re.sub(r"[#*`_>]+", "", first).strip()
    return first[:60] + ("…" if len(first) > 60 else "")


def _doc_update(v: int, text: str, info: str = None) -> dict:
    update = {"payload": {"type": "doc", "v": v, "text": text, "title": _title(text)},
              "summary": f"v{v} · tap to annotate"}
    if info:
        update["info"] = info
    return update


def start(chat, text: str, session_id: str | None) -> int:
    """Send a review app for `text`; returns the review number."""
    xdc = build_xdc()
    try:
        msg = chat.send_message(file=str(xdc), filename="review.xdc")
    finally:
        xdc.unlink(missing_ok=True)
    review_id = store.add_review(msg.id, chat.id, session_id, text)
    msg.send_webxdc_status_update(_doc_update(1, text), "")
    return review_id


def post_revision(account, review: dict, text: str) -> int:
    """Push a new version into an existing review app; returns its number."""
    from deltachat_rpc_client import Message
    v = store.add_review_version(review["id"], text)
    Message(account, review["msg_id"]).send_webxdc_status_update(
        _doc_update(v, text, info=f"Claude posted v{v} of review #{review['id']}"), "")
    return v


def collect_notes(account, review: dict) -> list[dict]:
    """New `notes` batches since we last looked; advances the stored cursor."""
    from deltachat_rpc_client import Message
    msg = Message(account, review["msg_id"])
    updates = msg.get_webxdc_status_updates(review["last_serial"])
    batches = []
    last = review["last_serial"]
    for u in updates:
        last = max(last, u.get("serial", last))
        p = u.get("payload") or {}
        if p.get("type") == "notes" and p.get("notes"):
            batches.append(p)
    if last != review["last_serial"]:
        store.set_review_serial(review["id"], last)
    return batches


def ack(account, review: dict, batch_id: str):
    from deltachat_rpc_client import Message
    Message(account, review["msg_id"]).send_webxdc_status_update(
        {"payload": {"type": "ack", "id": batch_id}}, "")


def format_notes(review: dict, batch: dict, include_text: bool) -> str:
    """Turn a notes batch into the turn Claude sees."""
    v = batch.get("v") or len(review["versions"])
    lines = [f"[review #{review['id']}, v{v}: notes on your message]"]
    if include_text:
        versions = review["versions"]
        text = versions[v - 1] if 0 < v <= len(versions) else versions[-1]
        lines += ["Full text being reviewed (from an earlier session):", "<<<", text, ">>>"]
    for i, n in enumerate(batch["notes"], 1):
        comment = (n.get("comment") or "").strip()
        quote = n.get("quote")
        if quote:
            lines.append(f"{i}. on \"{quote}\": {comment}")
        else:
            lines.append(f"{i}. (general) {comment}")
    lines.append("")
    lines.append(f"Reply normally. If a revised version of the text would help, wrap it in "
                 f"<revision review=\"{review['id']}\">…</revision> in your reply; it is posted "
                 f"into the review app as v{len(review['versions']) + 1} instead of the chat.")
    return "\n".join(lines)


def extract_revisions(text: str, default_review: int | None) -> tuple[str, list[tuple[int, str]]]:
    """Pull <revision> blocks out of a reply. Returns the remaining text and
    [(review_id, revised_text)]; blocks with no known target stay in the text."""
    found = []

    def sub(m):
        rid = int(m.group(1)) if m.group(1) else default_review
        if rid is None or not m.group(2):
            return m.group(2)
        found.append((rid, m.group(2)))
        return f"\x00{len(found) - 1}\x00"

    out = REVISION_RE.sub(sub, text)
    return out, found
