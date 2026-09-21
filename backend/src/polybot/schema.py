from __future__ import annotations

# Bump on every new migration. Kept in one place so the runtime health checks,
# worker readiness, and migration tests cannot silently disagree.
EXPECTED_SCHEMA_VERSION = 20
