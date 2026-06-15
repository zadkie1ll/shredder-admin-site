"""Provision a brand-new email user the *bot way*: a real RWMS subscription plus
a 7-day trial, then a local ``users`` row — NOT the website DB-only path.

This is the money-critical part of email login. For an EXISTING user the caller
must never come here. For a new user we:

1. Generate a fresh unique username (same generator the website uses in
   ``create_site_user``: ``uuid.uuid4().hex``; fall back to ``mi_`` + random hex).
2. SAFETY: confirm the username is free in RWMS (``get_user_by_username`` is
   ``None``) before ever calling ``AddUser`` — we never touch an existing
   subscription and never delete/recreate.
3. Create the RWMS subscription via the existing ``create_user`` helper with
   ``expire_at = now + trial days`` (unconditionally — trial gating does not
   apply to the mobile email-login flow).
4. Insert the local ``User`` row with that expiry.

A concurrent duplicate on ``User.email`` is handled by the caller (re-fetch).
"""

import logging
import secrets
import uuid

from django.conf import settings
from sqlalchemy.exc import IntegrityError

from common.models.db import User
from engine.rwms_helpers import create_user

logger = logging.getLogger(__name__)


def _generate_username(rwms_client, max_tries=5):
    """Return a username that is free in RWMS. Uses the same ``uuid4().hex``
    generator as ``create_site_user``; if a collision is seen, fall back to a
    ``mi_`` + token_hex name. Returns ``None`` if no free name was found (caller
    aborts rather than risk reusing an existing subscription)."""
    candidates = [uuid.uuid4().hex for _ in range(max_tries)]
    candidates.append("mi_" + secrets.token_hex(8))
    for username in candidates:
        # SAFETY: must be None (fresh). Never AddUser onto an existing username.
        if rwms_client.get_user_by_username(username) is None:
            return username
        logger.warning(
            "mobile_api: generated username %s already exists in RWMS, retrying",
            username,
        )
    return None


def provision_trial_user(db_session, rwms_client, email):
    """Create a brand-new trial user for ``email`` (already lowercased).

    Returns the created (or, on a concurrent insert, re-fetched) ``User``, or
    ``None`` if provisioning failed (no free username / RWMS create failed)."""
    username = _generate_username(rwms_client)
    if username is None:
        logger.error(
            "mobile_api: could not generate a free RWMS username for %s; aborting",
            email,
        )
        return None

    trial_period_days = settings.SITE_TRIAL_PERIOD_DAYS
    rw_user = create_user(
        rwms_client=rwms_client,
        username=username,
        trial_period_days=trial_period_days,
        from_referrer=False,
        email=email,
    )
    if rw_user is None:
        logger.error("mobile_api: RWMS create_user failed for new email user %s", email)
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
