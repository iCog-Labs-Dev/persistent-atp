# Generated only after source verification, with a digest-pinned Omega base.
FROM @@BASE_IMAGE@@
USER root
WORKDIR /PeTTa
ENV OMEGA_DIR=/PeTTa/repos/Omega
ENV PERSISTENT_ATP_OMEGA_STATE=/PeTTa/repos/Omega/memory/persistent_atp/workflow.json
# The base supplies Omega's existing entrypoint, Python, Janus and dependencies.
# Reject a base with a different PeTTa rather than changing its runtime silently.
RUN test "$(git -C /PeTTa rev-parse HEAD)" = "@@PETTA_REVISION@@"
COPY omega/ /PeTTa/repos/Omega/
COPY omega/run.metta /PeTTa/run.metta
RUN python3 -c 'import sys, httpx; assert sys.version_info >= (3, 11)'
RUN swipl -q -g 'use_module(library(janus)), py_call(sys:version_info, _), halt'
RUN PYTHONPATH=/PeTTa/repos/Omega/plugins/persistent_atp python3 -c 'from omegaclaw.runtime import verify_manifest; verify_manifest("/PeTTa/repos/Omega")'
# Keep the upstream startup flow with the verified bridge allowlist overlay.
ENTRYPOINT ["/bin/bash", "/PeTTa/repos/Omega/entrypoint.sh"]
