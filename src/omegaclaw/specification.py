"""Strict parsing of literal theorem input, shared by adapter and receiver."""
import json

SPEC_FIELDS = frozenset({"theorem", "definitions", "assumptions", "quantifier_domains",
                         "allowed_background", "targets", "verification_policy"})


def parse_json(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 64 * 1024:
        raise ValueError("JSON input must be a bounded string")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) != SPEC_FIELDS:
        raise ValueError("spec requires theorem, definitions, assumptions, quantifier_domains, allowed_background, targets and verification_policy")
    if not isinstance(spec["theorem"], str) or not spec["theorem"].strip():
        raise ValueError("theorem must be literal non-empty text")
    for name in ("definitions", "quantifier_domains"):
        if not isinstance(spec[name], (dict, list)):
            raise ValueError(f"{name} must explicitly describe its entries, or be empty")
    for name in ("assumptions", "allowed_background", "targets"):
        if not isinstance(spec[name], list) or not all(isinstance(x, str) and x.strip() for x in spec[name]):
            raise ValueError(f"{name} must be a list of non-empty strings")
    if not spec["targets"]:
        raise ValueError("targets must be explicit")
    policy = spec["verification_policy"]
    if not isinstance(policy, (dict, str)) or not policy:
        raise ValueError("verification_policy must be explicit")
    # Freeze a JSON-only copy; the literal input is never interpreted as code,
    # a pathname, a URL, or permission to weaken the receiver's policy.
    serialized = json.dumps(spec, ensure_ascii=False, allow_nan=False)
    if len(serialized.encode("utf-8")) > 64 * 1024:
        raise ValueError("theorem specification is too large")
    return json.loads(serialized)
