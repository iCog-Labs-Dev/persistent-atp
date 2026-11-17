# Generated only after source verification, with a digest-pinned Omega base.
FROM @@BASE_IMAGE@@
USER root
WORKDIR /PeTTa
# The base supplies Omega's existing entrypoint, Python, Janus and dependencies.
# Reject a base with a different PeTTa rather than changing its runtime silently.
RUN test "$(git -C /PeTTa rev-parse HEAD)" = "@@PETTA_REVISION@@"
COPY omega/ /PeTTa/repos/Omega/
COPY omega/run.metta /PeTTa/run.metta
RUN python3 -c 'import sys, janus_swi, httpx; assert sys.version_info >= (3, 11)'
RUN PYTHONPATH=/PeTTa/repos/Omega/plugins/persistent_atp python3 -c 'from omegaclaw.runtime import verify_manifest; verify_manifest("/PeTTa/repos/Omega")'
# Preserve the upstream Omega entrypoint and channel/provider configuration.
