"""Reversible, digest-checked hooks for the pinned Omega runtime.

Only these exact upstream bytes or our exact derived bytes are accepted.
Unrelated edits remain errors, including when installation is repeated.
"""
import hashlib


PATCHES = {
    "src/loop.metta": (
        "8b6ceb75823fdf54bf041d4514d6c07d7a0b87b8e333c572b97d1c0e09357989",
        (("          (let $prompt (getContext)\n               (progn (log INFO \"loop\" (---------iteration $k))\n                      (heartbeat $k)",
          "          (heartbeat $k)\n          (let $prompt (getContext)\n               (progn (log INFO \"loop\" (---------iteration $k))"),
         ("                            (if (> (get-state &loops) 0)",
          "                            (progn\n                              (if (py-call (persistent_atp_channel.model_suspended))\n                                  (change-state! &loops 0) _)\n                              (if (<= (get-state &loops) 0)\n                                  (change-state! &loops (py-call (persistent_atp_channel.continuation_turns))) _)\n                            (if (> (get-state &loops) 0)"),
         ("(change-state! &loops (+ 1 (maxWakeLoops))) _)))", "(change-state! &loops (+ 1 (maxWakeLoops))) _))))"))),
    "channels/wschat.py": (
        "233c10d3d02d073c84c75b47bff7f0d7cb5cd36bff78346e0dff0c158cd50535",
        (("def _handle_frame(raw_message):\n", "def _handle_frame(raw_message):\n    global _last_seen_seq\n"),
         ("        _enqueue_user_message(seq, text)\n", "        from persistent_atp_channel import accept_frame\n        if accept_frame(frame, _ws_url):\n            with _msg_lock:\n                _last_seen_seq = max(_last_seen_seq or 0, seq)\n            return\n        _enqueue_user_message(seq, text)\n"),
         ('    if frame_type == "ack":\n', '    if frame_type == "ack":\n        from persistent_atp_channel import acknowledged\n        acknowledged(frame, _ws_url)\n'),
         ("    _drain_outbox(ws)\n", "    _drain_outbox(ws)\n    from persistent_atp_channel import flush\n    flush(lambda frame: _send_json(frame, ws=ws), _ws_url)\n"))),
}


def original_bytes(name, raw):
    digest, replacements = PATCHES[name]
    if hashlib.sha256(raw).hexdigest() == digest:
        return raw
    text = raw.decode()
    for before, after in reversed(replacements):
        if text.count(after) != 1:
            raise ValueError(f"unexpected coordinator overlay: {name}")
        text = text.replace(after, before, 1)
    if hashlib.sha256(text.encode()).hexdigest() != digest:
        raise ValueError(f"pinned runtime differs from coordinator overlay: {name}")
    return text.encode()


def patched_bytes(name, raw):
    text = original_bytes(name, raw).decode()
    for before, after in PATCHES[name][1]:
        if text.count(before) != 1:
            raise ValueError(f"coordinator hook is absent or ambiguous: {name}")
        text = text.replace(before, after, 1)
    return text.encode()
