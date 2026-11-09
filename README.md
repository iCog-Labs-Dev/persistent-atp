# Persistent Knowledge Mining

**A durable, race-safe metagraph memory system for long-horizon automated theorem proving.**

---

## Description

This prototype introduces persistence into the theorem-proving search loop. It extends the existing symbolic knowledge infrastructure into a **MORK-backed PeTTa metagraph** that maintains research strategy, formal proof states, statement alignment, and provenance across long-running proof search sessions — rather than losing this context between runs, as the baseline did.

Writes to the knowledge base go through a **deterministic commit gate** and an **append-only journal**, ensuring every modification is traceable, reproducible, and protected from unauthorized direct mutation.

For this early prototype phase, the system is built on a contemporary off-the-shelf database to keep development pace steady, with a path toward the full MORK/PeTTa-native backend as the design matures.

