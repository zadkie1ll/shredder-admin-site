# Shredder infrastructure adaptation

This fork runs the Monkey Island administration surface as a standalone
Shredder service. The customer landing, registration, cabinet, mobile API and
payment entry points are intentionally not routed by `web_app.admin_urls`.

## Current stage

- `origin` is `zadkie1ll/shredder-admin-site`; the source repository is kept as
  `upstream`.
- `web_app.admin_settings` maps `SHREDDER_ADMIN_*` variables to the original
  settings contract.
- `web_app.admin_wsgi` does not start the source project's censor and
  infrastructure workers implicitly.
- The container exposes only `/`, `/health/` and `/support-admin/**`.
- Common/Alembic migrations are never applied by the container.

The source `common` submodule is still pinned temporarily so the fork remains
bootable while model adapters are implemented. It must not be pointed at a
production Shredder database until the compatibility work below is complete.

## Compatibility work

| Area | State | Required adaptation |
| --- | --- | --- |
| Admin shell and role checks | isolated | Replace bootstrap shared passwords with Shredder admin accounts |
| Config templates | pending | Preserve the existing `shredder-admin` rotation, Lagom and WL-01 behavior |
| Users | blocked | Map Shredder nullable username, mandatory telegram id, bot instance and site identities |
| Payments | pending | Keep Shredder YooKassa tables and service semantics |
| Referrals and analytics | pending | Rewrite queries against Shredder models and event names |
| Subscription mutations | blocked | Add explicit DB/RWMS reconciliation and audit before enabling writes |
| Broadcasts | pending | Map queues and message contracts to `shredder-vpn-bot` |
| Device management | blocked | Current Shredder RWMS does not expose HWID RPC methods |
| Node provisioning and TSPU | blocked | Requires RWMS/node-agent contracts absent from the local infrastructure |

## Safety boundary

Until the blocked rows are resolved, run this service only against disposable
development databases. Do not apply the source common migrations to the shared
Shredder database and do not enable subscription mutation endpoints in
production.
