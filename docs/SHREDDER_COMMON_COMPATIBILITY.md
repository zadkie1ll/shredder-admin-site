# Compatibility with shredder-common

Pinned module: `zadkie1ll/shredder-common@1576d60`.

The submodule replacement is complete. The active standalone administration
surface is runtime-compatible and read-only. Incompatible upstream views remain
in the repository as a porting reference but are not imported or routed.

Run the non-mutating audit with:

```bash
.venv/bin/python scripts/check_shredder_common_compat.py
```

The audit never connects to PostgreSQL or RWMS and never runs Alembic.

The command reports `COMPATIBLE` for the active runtime contract.

## Dormant legacy incompatibilities

- Missing modules: `managed_traffic_limits`, `models.segments`,
  `models.settings`, `runtime_tariffs`, and `rwms_client_sync`.
- Runtime imports require 47 SQLAlchemy models/enums absent from Shredder.
- `User` lacks upstream `email`; Shredder stores site identities separately.
- `YkPayment` lacks `is_autopay` and `cancellation_reason`.
- `YkRecurrentPayment` lacks `next_retry_at`.
- Admin declares 16 RPC methods while local Shredder clients declare 8; node,
  HWID and revoke-subscription operations are absent.

## Safe migration direction

1. Keep Shredder users, YooKassa payments, referrals and traffic sources as the
   authoritative shared models.
2. Put administration-only models in this service with additive, reviewed
   migrations; never copy the upstream `User` or baseline migration.
3. Resolve email through Shredder site identity/OAuth tables.
4. Port read-only analytics before mutation endpoints.
5. Preserve current `shredder-admin` rotation, Lagom and WL-01 contracts.
6. Enable infrastructure only after RWMS and node-agent contracts exist.
