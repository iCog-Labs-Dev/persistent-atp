# Provisional formal v2 GNN slice

Canonical schema: `formal-v2.schema.json`.

SHA-256: `caba333d83ecacad29a84708430bfed328614ad4bd3aadd544da9eb78b88cac7`.

This coordinated implementation is on `formal-v2-provider-client`, based on
persistent-atp main `2f3ac00ae4ced1f9a508df6bc0379fe97a152323`. Its provider is
Mathematics-ai-atp branch `formal-v2-gnn-integration`, based on PR94
`e2be6fbdc0ed045a02c2fa58d9eb42f4099dd880`.

The provider handoff is `maths_ai/gnn_inference/docs/formal_v2_gnn_slice.md` in
that repository. Both processes consume these exact schema bytes. The provider
requires the configured digest and the client rejects mismatched protocol
revisions. This is a provisional review contract, not an approved v2 release.

`mathproof.formal_v2.FormalV2HTTPATP` supports immutable/idempotent admission,
polling, cancellation, bounded retry, artifact transfer and bound independent
replay. Backend selection is optional. `restore(snapshot)` reconnects from a
coordinator-owned durable record; there is no new client journal. Logical replay
has a stable default idempotency key; explicitly new verification requires a new
replay ID. Saved-frontier resume is unsupported.

The v2 snapshot/receipt is not the older canonical hypergraph result. The new
client does not mark claims proved or fabricate a v1 graph. Coordinator task
builders, canonical graph export, lease/fencing proposal admission and shared
vocabularies still need coordinated migration before production promotion.

Current source input is a `lean-declaration-v1` JSON context package with
prefix/header/declaration, not an arbitrary Lean module file. Goal input uses
supported ordered explicit locals, preserving lets; requested/inferred text
universes are rejected by this first provider profile.

Validation: **190 mathproof tests and 63 subtests passed**. The coordinated
provider checks passed **178 tests** with two existing MCTS live-test skips.
Real HTTP/native acceptance uses this client and the actual accepted model,
including source/goal admission, independently checked receipts, cancellation
of a real Lean process and native-let preservation. Injected lifecycle tests
remain labelled separately. Both wheels build; the v2 schema is included as a
data file for installed-client use.
