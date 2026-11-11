"""TEST ONLY: actual MathsAI API, synthetic search, real Lean replay compiler.

Executed in the companion backend's lightweight test environment. The test
fixture deliberately advertises its synthetic identity; never deploy it.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

backend, state = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
sys.path.insert(0, str(backend))
project = state / "lean"
project.mkdir(parents=True)
(project / "lean-toolchain").write_text(os.environ["MATHS_AI_TEST_LEAN_TOOLCHAIN"] + "\n")
(project / "lakefile.toml").write_text('name = "contracttest"\nversion = "0.1.0"\n')
(project / "lake-manifest.json").write_text('{"version":"1.1.0","packages":[]}')
os.environ["MATHS_AI_LEAN_PROJECT"] = str(project)
os.environ["MATHS_AI_PROVIDER_ID"] = "synthetic-search-real-lean-replay-test-only"
os.environ["MATHS_AI_LEAN_IMPORTS"] = "Init"
os.environ.pop("MATHS_AI_API_TOKEN", None)

from fastapi.testclient import TestClient
from maths_ai.api.app import create_app
from maths_ai.api.service import InferenceService
from maths_ai.api.provider_contract import environment_manifest
from maths_ai.hybrid_reasoner.hypergraph import ProofHypergraph
from maths_ai.data_models.proof_components import Goal, TacticCandidate


class FixtureProtocol:
    async def goal_tactic_async(self, state, *_):
        # Search/Pantograph protocol is synthetic; the separate compiler pass
        # in InferenceService.replay still checks the full resulting declaration.
        return SimpleNamespace(goals=[])


class FixtureReasoner:
    max_depth, max_nodes, max_steps = 10, 500, None
    dts_sampler = None
    def __init__(self):
        self.executor = SimpleNamespace(server=FixtureProtocol())
    async def _start_state(self, goal): return SimpleNamespace(goals=[goal])
    async def prove(self, goal, *, hypotheses=None):
        count = state / "search-count"
        count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
        graph = ProofHypergraph(Goal(expression=goal, hypotheses=hypotheses or []))
        argument = "False.elim" if os.environ.get("MATHS_AI_FIXTURE_BAD_PROOF") else "True.intro"
        graph.add_edge(graph.root_id, TacticCandidate(tactic_name="exact", arguments=[argument], probability=1), [])
        graph.tactic_steps = 1
        return graph


async def factory():
    service = InferenceService(reasoner=FixtureReasoner(), backend_mode="real", dts_state_path=state / "dts.json")
    service.environment_manifest = environment_manifest(project)
    original_replay = service.replay
    async def tracked_replay(*args, **kwargs):
        path = state / "replay-count"
        path.write_text(str(int(path.read_text()) + 1 if path.exists() else 1))
        return await original_replay(*args, **kwargs)
    service.replay = tracked_replay
    return service


with TestClient(create_app(service_factory=factory, formal_runs_dir=state / "runs")) as client:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def request(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 24 * 1024 * 1024:
                self.send_error(413)
                return
            data = self.rfile.read(size) if size else None
            response = client.request(self.command, self.path, content=data,
                                      headers={"Content-Type": "application/json"})
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)
        do_GET = do_POST = request
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}"}), flush=True)
    server.serve_forever()
