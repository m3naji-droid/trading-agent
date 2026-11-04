"""
إشعارات الجوال عبر تطبيق ntfy (مجاني، بدون حساب).
كل وكيل له قناة سرية عشوائية؛ تشترك فيها من تطبيق ntfy على جوالك.
"""
import json
import logging
import secrets
import threading
import time
import urllib.request

from . import memory

log = logging.getLogger("notify")
SERVER = "https://ntfy.sh"
_last = {}


def topic() -> str:
    t = memory.kv_get("ntfy_topic")
    if not t:
        t = "agent-" + secrets.token_hex(8)
        memory.kv_set("ntfy_topic", t)
    return t


def enabled() -> bool:
    return bool(memory.kv_get("notify_on", True))


def set_enabled(on: bool):
    memory.kv_set("notify_on", bool(on))


def _post(payload):
    try:
        req = urllib.request.Request(SERVER, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "trading-agent"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log.warning(f"تعذر إرسال الإشعار: {e}")


def send(title: str, message: str, priority: int = 3, tags=None, force=False, throttle_key=None, throttle_sec=3600):
    """priority: 1 (هادئ) … 5 (عاجل). throttle_key يمنع تكرار نفس الإشعار."""
    if not force and not enabled():
        return
    if throttle_key:
        if time.time() - _last.get(throttle_key, 0) < throttle_sec:
            return
        _last[throttle_key] = time.time()
    payload = {"topic": topic(), "title": title, "message": message, "priority": priority, "tags": tags or []}
    threading.Thread(target=_post, args=(payload,), daemon=True).start()
