"""The trusted playground plan constrains a model before any tool effect."""
import json
import asyncio
import threading

import pytest

from omegaclaw.experiment import validate_planned_call


def formal_call():
    return {"name": "formal-search", "arguments": {"proof": "p1", "claim": "p1/c-1",
        "budget": '{"max_steps":100,"wall_clock_ms":10000}', "workspace": "production"}}


@pytest.mark.parametrize("field,value", [
    ("proof", "another-proof"), ("claim", "p1/c-2"), ("workspace", "another-workspace"),
    ("budget", '{"max_steps":101,"wall_clock_ms":10000}'),
    ("budget", '{"max_steps":true,"wall_clock_ms":10000}'),
    ("budget", '{"max_steps":100,"max_steps":100,"wall_clock_ms":10000}'),
])
def test_model_cannot_change_the_trusted_search_scope_or_budget(field, value):
    expected, actual = formal_call(), formal_call()
    actual["arguments"][field] = value
    with pytest.raises(ValueError):
        validate_planned_call(actual, expected)


def test_model_json_formatting_does_not_change_a_budget():
    expected, actual = formal_call(), formal_call()
    actual["arguments"]["budget"] = json.dumps({"wall_clock_ms": 10000, "max_steps": 100}, indent=2)
    validate_planned_call(actual, expected)


def test_live_preflight_refuses_reference_without_admitting_work(tmp_path):
    from omegaclaw.api import serve
    from omegaclaw.playground import PlaygroundError, preflight
    from omegaclaw.receiver import ReferenceReceiver
    from omegaclaw.service import OperationService
    service = OperationService(tmp_path)
    receiver = ReferenceReceiver(service)
    server = serve(service, "playground-preflight-token", receiver=receiver)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with pytest.raises(PlaygroundError, match="no reference fallback"):
            asyncio.run(preflight(f"http://127.0.0.1:{server.server_port}", "playground-preflight-token",
                                  "reference", "not-created", live=True))
        assert service.store.proof_ids() == []
        assert receiver._db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    finally:
        server.shutdown()
        server.server_close()
        receiver.close()
