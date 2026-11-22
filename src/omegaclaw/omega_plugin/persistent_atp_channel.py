"""Pinned WebSocket/loop hooks. No model-visible authority or direct store access.

Only explicitly structured user tasks are managed. Ordinary chat continues
through Omega unchanged. Server scope and inbound sequence survive as task IDs.
"""
import hashlib
import json
import sys


def _bridge():
    bridge = sys.modules.get("omega_persistent_atp_bridge")
    return bridge if bridge is not None and bridge._enabled else None


def _source(url):
    # Changing the authenticated channel must not deliver an old task's result
    # to a new principal at the same URL. Credentials never enter task records.
    token = getattr(sys.modules.get("wschat"), "_ws_token", "")
    scope = json.dumps([url, hashlib.sha256(token.encode()).hexdigest()])
    return "websocket:" + hashlib.sha256(scope.encode()).hexdigest()


def _note_input(adapter, source, sequence, mode):
    def note(data):
        inputs = data.setdefault("channel_inputs", {})
        if sequence > inputs.get(source, -1):
            inputs[source] = sequence
            data["input_mode"] = mode
            if mode == "ordinary":
                data.setdefault("continuation", {})["used"] = 0
    adapter.state.update(note)


def accept_frame(frame, url):
    """Persist before the core channel advances last_seen_seq.

    JSON metadata is supplied by the user/channel, never extracted from model
    output. Free-form instructions remain the ordinary Omega agent's task.
    """
    bridge = _bridge()
    if bridge is None:
        return False
    adapter = bridge._get_adapter()
    source, message = _source(url), str(frame["seq"])
    request = frame.get("persistent_atp_task")
    if request is None and frame["text"].startswith("PERSISTENT_ATP_TASK "):
        try:
            from omegaclaw.specification import parse_json
            request = parse_json(frame["text"][len("PERSISTENT_ATP_TASK "):])
        except (ValueError, TypeError) as exc:
            adapter.tasks.reject(source, message, str(exc))
            _note_input(adapter, source, frame["seq"], "managed")
            return True
    if request is None:
        # New ordinary input permits another bounded batch of event-driven
        # model decisions. Identical replay does not reset the allowance.
        _note_input(adapter, source, frame["seq"], "ordinary")
        return False
    state = adapter.state.snapshot()
    if (frame["seq"] <= state.get("channel_inputs", {}).get(source, -1)
            and not any(task["source"] == source and task["message_id"] == message
                        for task in state["tasks"].values())):
        adapter.tasks.reject(source, message, "consumed ordinary message cannot become a new proof task")
        return True
    try:
        adapter.tasks.accept(source, message, request)
    except (ValueError, TypeError) as exc:
        adapter.tasks.reject(source, message, str(exc))
    _note_input(adapter, source, frame["seq"], "managed")
    return True


def acknowledged(frame, url):
    bridge = _bridge()
    if bridge and isinstance(frame.get("client_seq"), str):
        return bridge._get_adapter().tasks.acknowledge_delivery(_source(url), frame["client_seq"])
    return False


def flush(send, url):
    bridge = _bridge()
    if bridge is None:
        return
    workflow = bridge._get_adapter().tasks
    source = _source(url)
    for _ in range(4):
        task = workflow.claim_delivery(source)
        if task is None:
            return
        # Checkpoint uncertainty BEFORE socket I/O, including failed sends and
        # process death after send. Claim later replies only when reached so
        # one failed write cannot consume their attempts.
        try:
            send({"type": "agent_message", "client_seq": task["delivery"]["message_id"],
                  "text": task["delivery"]["text"]})
        except (OSError, RuntimeError):
            return


def flush_connected():
    channel = sys.modules.get("wschat")
    if channel is not None and channel._connected:
        flush(channel._send_json, channel._ws_url)


def continuation_turns():
    """Claim at most one continuation per unowned terminal operation version.

    Managed workflows progress in background ticks even with zero model turns.
    They never wake the model to duplicate their deterministic SDK operations.
    """
    bridge = _bridge()
    if bridge is None:
        return 0
    def claim(data):
        if data.get("input_mode") == "managed":
            return 0
        continuation = data.setdefault("continuation", {"used": 0, "seen": []})
        seen = continuation.setdefault("seen", [])
        if continuation.get("used", 0) >= 24:
            return 0
        for row in data["intents"].values():
            snapshot = row.get("snapshot") or {}
            if row.get("task_id") or snapshot.get("lifecycle") not in {"succeeded", "failed", "cancelled"}:
                continue
            key = [row["id"], snapshot["observation_version"]]
            if key not in seen:
                seen.append(key)
                continuation["used"] = continuation.get("used", 0) + 1
                return 2
        return 0
    return bridge._get_adapter().state.update(claim)


def model_suspended():
    """Managed tasks need heartbeat work, not speculative model polling.

    A fresh ordinary channel message restores the existing agent workflow.
    """
    bridge = _bridge()
    return bool(bridge and bridge._get_adapter().state.snapshot().get("input_mode") == "managed")
