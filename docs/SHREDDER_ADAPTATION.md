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

The `common` submodule now points to `zadkie1ll/shredder-common@1576d60`.
The active URL surface uses a dedicated read-only repository and does not
import the incompatible legacy views. See
[SHREDDER_COMMON_COMPATIBILITY.md](SHREDDER_COMMON_COMPATIBILITY.md).

## Compatibility work

| Area | State | Required adaptation |
| --- | --- | --- |
| Admin shell and role checks | isolated | Replace bootstrap shared passwords with Shredder admin accounts |
| Config templates | pending | Preserve the existing `shredder-admin` rotation, Lagom and WL-01 behavior |
| Users | read-only | Search by Telegram ID/username/site email; Shredder fields preserved |
| Payments | read-only | YooKassa history, LTV and recurrent state use Shredder models |
| Referrals and analytics | read-only core | Counts, bonus days and referred users are available |
| Subscription mutations | blocked | Add explicit DB/RWMS reconciliation and audit before enabling writes |
| Broadcasts | pending | Map queues and message contracts to `shredder-vpn-bot` |
| Device management | blocked | Current Shredder RWMS does not expose HWID RPC methods |
| Node provisioning and TSPU | blocked | Requires RWMS/node-agent contracts absent from the local infrastructure |

## Safety boundary

The active endpoints issue SELECT queries only and can use the shared Shredder
database after normal deployment review. Do not expose dormant legacy routes,
apply source common migrations, or enable subscription mutation endpoints.
