"""Test process killed after provider admission, before its result is saved."""
import asyncio
import json
from pathlib import Path
import sys
import time

from omegaclaw.factory import build_service
from omegaclaw.receiver import ReferenceReceiver
from persistent_atp_client import AsyncClient, Budget, FormalSearch, Preconditions
from persistent_atp_client.adapters.local import LocalReceiverAdapter

config_path, root, proof, marker = sys.argv[1:]
service = build_service(json.loads(Path(config_path).read_text()), Path(root))
original = service.executor.adapter.formal_search_start
def pause(request):
    result = original(request)
    Path(marker).write_text("provider-admitted")
    time.sleep(60)
    return result
service.executor.adapter.formal_search_start = pause
receiver = ReferenceReceiver(service)
async def main():
    async with AsyncClient(LocalReceiverAdapter(receiver)) as client:
        request = client.prepare(FormalSearch(proof, proof + "/c-1", Budget(3, 5000)),
            workspace_id="production", request_id="maths-ai-" + proof,
            preconditions=Preconditions(expected_revision=2))
        handle = await client.submit(request)
        print(json.dumps({"request": request.to_dict(), "reference": handle.ref.to_dict()}), flush=True)
        await asyncio.sleep(60)
asyncio.run(main())
