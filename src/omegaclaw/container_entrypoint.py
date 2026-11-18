"""Narrow configuration overlay for the pinned upstream Omega entrypoint."""
import hashlib


# entrypoint.sh at OMEGA_REVISION; unrelated script changes are rejected.
UPSTREAM_ENTRYPOINT_HASH = "1fa20147cd904801999df04f07b1792f07fc5135449879cbdc574a419ead4ca8"
BRIDGE_VARIABLES = (
    "PERSISTENT_ATP_OMEGA_URL", "PERSISTENT_ATP_OMEGA_TOKEN", "PERSISTENT_ATP_OMEGA_WORKSPACE",
    "PERSISTENT_ATP_OMEGA_STATE", "PERSISTENT_ATP_OMEGA_MAX_CYCLES",
    "PERSISTENT_ATP_OMEGA_RETRY_LIMIT", "PERSISTENT_ATP_OMEGA_RETRY_BASE_SECONDS",
    "OMEGA_persistentAtpEnabled", "OMEGA_persistentAtpWorkspace",
)
_ORIGINAL_TAIL = r'''# Scrub environment: only allowlisted vars survive.
SAFE_VARS="HOME USER PATH HOSTNAME TERM LANG LC_ALL \
  PYTHONDONTWRITEBYTECODE PYTHONUNBUFFERED \
  HF_HOME SENTENCE_TRANSFORMERS_HOME HF_HUB_OFFLINE TRANSFORMERS_OFFLINE \
  CHROMA_DB_PATH EMBEDDING_PROVIDER OMEGA_DIR MEMORY_DIR TEST_SERVER_IP"

env_args=""
for var in $SAFE_VARS; do
  eval val=\${$var:-}
  if [ -n "$val" ]; then
    env_args="$env_args $var=$val"
  fi
done

exec env -i $env_args "${agent_su[@]}" -s /bin/sh -c "sh run.sh run.metta GATEWAY_URL=\"http://localhost:8080\" $*"
'''
_PATCHED_TAIL = (_ORIGINAL_TAIL
    .replace('MEMORY_DIR TEST_SERVER_IP"', 'MEMORY_DIR TEST_SERVER_IP ' + ' '.join(BRIDGE_VARIABLES) + '"')
    .replace('env_args=""', 'env_args=()')
    .replace(r'eval val=\${$var:-}', 'val=${!var-}')
    .replace('env_args="$env_args $var=$val"', 'env_args+=("$var=$val")')
    .replace('exec env -i $env_args ', 'exec env -i "${env_args[@]}" '))


def patch_entrypoint(text):
    original = text.replace(_PATCHED_TAIL, _ORIGINAL_TAIL)
    if hashlib.sha256(original.encode()).hexdigest() != UPSTREAM_ENTRYPOINT_HASH:
        raise ValueError("Omega entrypoint differs from the pinned upstream script")
    if text.count(_PATCHED_TAIL) == 1:
        return text
    if text.count(_ORIGINAL_TAIL) != 1:
        raise ValueError("Omega entrypoint environment boundary is absent or ambiguous")
    return text.replace(_ORIGINAL_TAIL, _PATCHED_TAIL)
