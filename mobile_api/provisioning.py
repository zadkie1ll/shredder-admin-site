"""Provision a brand-new email user the *bot way*: a real RWMS subscription plus
a 7-day trial, then a local ``users`` row — NOT the website DB-only path.

This is the money-critical part of email login. For an EXISTING user the caller
must never come here. For a new user we:

1. Derive a DETERMINISTIC username from the email (``deterministic_username``:
   ``"m"`` + first 31 hex chars of ``sha256(email.lower())``). The same email
   always maps to the same username, which closes the crash window below.
2. Strict-read that username in RWMS (``get_user_by_username_strict``) while
   holding the caller's per-email advisory lock:
   - RWMS unavailable → abort provisioning (an outage is never treated as
     "username is free" and never as "subscription exists").
   - The subscription EXISTS but the ``users`` row does not → a previous
     provisioning attempt for this very email crashed after the panel
     ``AddUser`` but before the DB commit. ADOPT the subscription: create the
     local row pointing at it and DO NOT touch the panel (no AddUser, no
     update, no recreate). With the old random ``uuid4().hex`` names such a
     subscription was orphaned forever, because a retry could never re-derive
     its name. Adoption is gated by an OWNERSHIP GUARD
     (``assert_subscription_owned_by_email``): a panel record whose email
     explicitly names somebody else is never adopted — that would hand a
     stranger's subscription to the current login. Such a mismatch is an
     ambiguous state: ALERT log, provisioning stopped, panel untouched.
   - Confirmed NOT_FOUND → create the RWMS subscription via ``create_user``
     with ``expire_at = now + trial days`` (unconditionally — trial gating
     does not apply to the mobile email-login flow).
3. Insert the local ``User`` row with the panel-reported expiry.

Username format safety: the panel accepts ``^[a-zA-Z0-9_-]+$`` (3..36 chars);
``"m" + 31 hex`` is 32 chars of ``[m0-9a-f]`` and can never collide with the
other generators in the fleet — bot usernames are pure digits
(``str(telegram_id)``), legacy website ones are 32 hex chars (``uuid4().hex``,
and ``m`` is not a hex digit), and the legacy ``mi_`` fallback has a non-hex
second character. Existing users keep their legacy random usernames untouched:
this module is only entered when no ``users`` row exists for the email, and no
code path ever recomputes an existing user's username.

The generator itself lives in :mod:`engine.rwms_helpers` — the SAME function now
names website email registrations (magic link / Google / Yandex / payment), so a
subscription orphaned by one flow's crash can be adopted by the other. It is
re-exported here because this module is its historical home.

A concurrent duplicate on ``User.email`` is handled by the caller (re-fetch).
"""

import logging

from django.conf import settings
from sqlalchemy.exc import IntegrityError

from common.models.db import User
from common.rwms_client import RwmsUnavailableError
from engine.rwms_helpers import USERNAME_HASH_LENGTH  # noqa: F401 - re-export
from engine.rwms_helpers import USERNAME_PREFIX  # noqa: F401 - re-export
from engine.rwms_helpers import RwmsSubscriptionOwnershipError  # noqa: F401
from engine.rwms_helpers import assert_subscription_owned_by_email
from engine.rwms_helpers import create_user
from engine.rwms_helpers import deterministic_username  # noqa: F401 - re-export

logger = logging.getLogger(__name__)


def provision_trial_user(db_session, rwms_client, email):
    """Create a brand-new trial user for ``email`` (already lowercased).

    Runs under the caller's per-email advisory lock. Returns the created (or,
    on a concurrent insert, re-fetched) ``User``, or ``None`` if provisioning
    failed (RWMS unavailable / RWMS create failed).

    Raises ``RwmsSubscriptionOwnershipError`` when the panel record found under
    the derived username explicitly belongs to a different email — an ambiguous
    state the caller must surface, not silently work around."""
    username = deterministic_username(email)

    # SAFETY: strict read — None is ONLY a confirmed NOT_FOUND. An RWMS outage
    # aborts provisioning: it must never be read as "free" (would double-create)
    # nor as "exists" (would adopt a subscription we could not verify).
    try:
        existing = rwms_client.get_user_by_username_strict(username)
    except RwmsUnavailableError as error:
        logger.warning(
            "mobile_api: RWMS unavailable while checking username %s for %s, "
            "aborting provisioning: %s",
            username,
            email,
            error,
        )
        return None

    trial_period_days = settings.SITE_TRIAL_PERIOD_DAYS
    if existing is not None:
        # Crash-window recovery: the subscription exists in the panel but the
        # caller saw no users row for this email — a previous attempt died
        # after AddUser and before commit. Adopt the subscription as-is: no
        # panel writes, no recreate, existing client configs stay functional.
        #
        # ...but only once we know it is OURS. A name match alone is not proof
        # of ownership (manual/imported/badly-created panel records exist), and
        # adopting a stranger's subscription would hand over their access.
        # Explicit mismatch → ALERT + raise; panel stays untouched.
        assert_subscription_owned_by_email(existing, email, flow="mobile_api")
        rw_user = existing
        logger.warning(
            "mobile_api: adopting existing RWMS subscription %s for %s "
            "(crash-window recovery, panel untouched)",
            username,
            email,
        )
    else:
        rw_user = create_user(
            rwms_client=rwms_client,
            username=username,
            trial_period_days=trial_period_days,
            from_referrer=False,
            email=email,
        )
        if rw_user is None:
            logger.error(
                "mobile_api: RWMS create_user failed for new email user %s", email
            )
            return None

    expire_at = None
    if rw_user.HasField("expire_at"):
        expire_at = rw_user.expire_at.ToDatetime().replace(tzinfo=None)

    user = User(email=email, username=username, expire_at=expire_at)
    db_session.add(user)
    try:
        db_session.flush()
    except IntegrityError:
        # Concurrent verify for the same email won the race — reuse that user.
        db_session.rollback()
        logger.info(
            "mobile_api: concurrent insert for %s, re-fetching existing user", email
        )
        return db_session.query(User).filter(User.email == email).one_or_none()

    logger.info(
        "mobile_api: provisioned trial user %s (username=%s, trial=%sd) via email login",
        email,
        username,
        trial_period_days,
    )
    return user
