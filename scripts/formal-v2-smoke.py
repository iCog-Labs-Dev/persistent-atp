"""Probe a running protocol v2 provider with a saved request.

Requires ARTIFACT_DB_URL and an applied artifact schema. This performs search
and replay, with durable artifact copies; it does not mutate the proof graph.
"""
import argparse
import json
from pathlib import Path
import time

from artifacts.store import ArtifactStore
from mathproof import maths_ai_http_atp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--request', required=True, type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--deadline-seconds', type=float, default=120)
    args = parser.parse_args()
    if args.deadline_seconds <= 0:
        parser.error('--deadline-seconds must be positive')
    request = json.loads(args.request.read_text())
    with ArtifactStore() as artifacts:
        if args.source:
            digest = artifacts.put(args.source.read_bytes())
            if request['input']['kind'] != 'source' or request['input']['artifact_hash'] != digest:
                parser.error('--source does not match the source input artifact hash')
        atp = maths_ai_http_atp(args.base_url, artifacts=artifacts)
        result = atp.formal_search_start(request)
        deadline = time.monotonic() + args.deadline_seconds
        while result['disposition'] == 'searching':
            if time.monotonic() >= deadline:
                print(json.dumps({'error':'local probe deadline; remote run may still be active', 'run_id':request['run_id']}))
                return 2
            time.sleep(0.25)
            result = atp.formal_search_status(request['run_id'])
        report = {'result':result}
        if result.get('certificate'):
            report['replay'] = atp.formal_replay(result['certificate'],request['environment_hash'])
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0 if report.get('replay',{}).get('status') == 'verified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
