"""Read-only queries against the authoritative Shredder product schema."""

from datetime import datetime

from sqlalchemy import String, cast, func, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from common.models.db import ReferralBonus, User, YkPayment, YkRecurrentPayment
from database import session_factory
from engine.shredder_admin_models import SiteIdentity


def _identity_emails(session, user_ids):
    if not user_ids:
        return {}
    try:
        rows = (
            session.query(SiteIdentity.user_id, SiteIdentity.login)
            .filter(
                SiteIdentity.user_id.in_(user_ids), SiteIdentity.login.contains("@")
            )
            .order_by(SiteIdentity.created_at.desc())
            .all()
        )
    except SQLAlchemyError:
        session.rollback()
        return {}
    result = {}
    for user_id, login in rows:
        result.setdefault(user_id, login)
    return result


def serialize_user(user, email=None):
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "telegram_username": user.telegram_username,
        "bot_instance": user.bot_instance,
        "email": email,
        "expire_at": user.expire_at.isoformat() if user.expire_at else None,
        "autopay_allow": bool(user.autopay_allow),
        "referred_by_id": user.referred_by_id,
    }


def serialize_payment(payment, user, email=None):
    return {
        "id": payment.id,
        "payment_id": payment.payment_id,
        "user": serialize_user(user, email),
        "amount": payment.amount,
        "currency": payment.currency,
        "status": payment.status,
        "tariff": payment.subscription_period,
        "is_trial_promotion": bool(payment.is_trial_promotion),
        "created_at": payment.created_at.isoformat() if payment.created_at else None,
        "captured_at": (
            payment.captured_at.isoformat() if payment.captured_at else None
        ),
    }


def load_stats():
    with session_factory() as session:
        return {
            "users": session.query(func.count(User.id)).scalar() or 0,
            "active_users": session.query(func.count(User.id))
            .filter(User.expire_at > datetime.utcnow())
            .scalar()
            or 0,
            "successful_payments": session.query(func.count(YkPayment.id))
            .filter(YkPayment.status == "succeeded")
            .scalar()
            or 0,
            "revenue": session.query(func.coalesce(func.sum(YkPayment.amount), 0))
            .filter(YkPayment.status == "succeeded")
            .scalar()
            or 0,
            "autopay_users": session.query(func.count(YkRecurrentPayment.id)).scalar()
            or 0,
            "referred_users": session.query(func.count(User.id))
            .filter(User.referred_by_id.is_not(None))
            .scalar()
            or 0,
        }


def search_users(query="", limit=50):
    with session_factory() as session:
        statement = session.query(User)
        if query:
            pattern = f"%{query.strip()}%"
            filters = [
                User.username.ilike(pattern),
                User.telegram_username.ilike(pattern),
                cast(User.telegram_id, String).ilike(pattern),
            ]
            try:
                ids = [
                    row[0]
                    for row in session.query(SiteIdentity.user_id)
                    .filter(SiteIdentity.login.ilike(pattern))
                    .limit(limit)
                    .all()
                ]
                if ids:
                    filters.append(User.id.in_(ids))
            except SQLAlchemyError:
                session.rollback()
            statement = statement.filter(or_(*filters))
        users = statement.order_by(User.id.desc()).limit(limit).all()
        emails = _identity_emails(session, [user.id for user in users])
        return [serialize_user(user, emails.get(user.id)) for user in users]


def load_user(user_id):
    with session_factory() as session:
        user = session.query(User).filter(User.id == user_id).first()
        if user is None:
            return None
        emails = _identity_emails(session, [user.id])
        payments = (
            session.query(YkPayment)
            .filter(YkPayment.user_id == user.id)
            .order_by(YkPayment.created_at.desc())
            .limit(100)
            .all()
        )
        recurrent = (
            session.query(YkRecurrentPayment)
            .filter(YkRecurrentPayment.user_id == user.id)
            .first()
        )
        referrals = (
            session.query(User)
            .filter(User.referred_by_id == user.id)
            .order_by(User.id.desc())
            .limit(100)
            .all()
        )
        bonus_days = (
            session.query(func.coalesce(func.sum(ReferralBonus.days_added), 0))
            .filter(ReferralBonus.referrer_id == user.id)
            .scalar()
            or 0
        )
        payload = serialize_user(user, emails.get(user.id))
        payload.update(
            {
                "ltv": sum(p.amount for p in payments if p.status == "succeeded"),
                "payments": [
                    {
                        "id": p.id,
                        "amount": p.amount,
                        "currency": p.currency,
                        "status": p.status,
                        "tariff": p.subscription_period,
                        "created_at": (
                            p.created_at.isoformat() if p.created_at else None
                        ),
                    }
                    for p in payments
                ],
                "autopay": (
                    None
                    if recurrent is None
                    else {
                        "amount": recurrent.amount,
                        "currency": recurrent.currency,
                        "tariff": recurrent.subscription_period,
                        "scheduled": bool(recurrent.scheduled_payment),
                    }
                ),
                "referrals": [serialize_user(item) for item in referrals],
                "referral_bonus_days": bonus_days,
            }
        )
        return payload


def search_payments(query="", status="", limit=50):
    with session_factory() as session:
        statement = session.query(YkPayment, User).join(
            User, User.id == YkPayment.user_id
        )
        normalized_query = query.strip()
        if normalized_query:
            pattern = f"%{normalized_query}%"
            filters = [
                YkPayment.payment_id.ilike(pattern),
                cast(YkPayment.id, String).ilike(pattern),
                cast(User.telegram_id, String).ilike(pattern),
                User.username.ilike(pattern),
                User.telegram_username.ilike(pattern),
            ]
            try:
                identity_ids = [
                    row[0]
                    for row in session.query(SiteIdentity.user_id)
                    .filter(SiteIdentity.login.ilike(pattern))
                    .limit(limit)
                    .all()
                ]
                if identity_ids:
                    filters.append(User.id.in_(identity_ids))
            except SQLAlchemyError:
                session.rollback()
            statement = statement.filter(or_(*filters))
        if status.strip():
            statement = statement.filter(YkPayment.status == status.strip())
        rows = (
            statement.order_by(YkPayment.created_at.desc(), YkPayment.id.desc())
            .limit(limit)
            .all()
        )
        emails = _identity_emails(session, [user.id for _, user in rows])
        return [
            serialize_payment(payment, user, emails.get(user.id))
            for payment, user in rows
        ]


def search_referrers(query="", limit=50):
    with session_factory() as session:
        referrer = aliased(User)
        referral = aliased(User)
        statement = (
            session.query(referrer, func.count(referral.id).label("referrals_count"))
            .join(referral, referral.referred_by_id == referrer.id)
            .group_by(referrer.id)
        )
        normalized_query = query.strip()
        if normalized_query:
            pattern = f"%{normalized_query}%"
            filters = [
                cast(referrer.telegram_id, String).ilike(pattern),
                referrer.username.ilike(pattern),
                referrer.telegram_username.ilike(pattern),
            ]
            try:
                identity_ids = [
                    row[0]
                    for row in session.query(SiteIdentity.user_id)
                    .filter(SiteIdentity.login.ilike(pattern))
                    .limit(limit)
                    .all()
                ]
                if identity_ids:
                    filters.append(referrer.id.in_(identity_ids))
            except SQLAlchemyError:
                session.rollback()
            statement = statement.filter(or_(*filters))
        rows = (
            statement.order_by(func.count(referral.id).desc(), referrer.id.desc())
            .limit(limit)
            .all()
        )
        referrer_ids = [item.id for item, _ in rows]
        emails = _identity_emails(session, referrer_ids)
        bonus_days = (
            dict(
                session.query(
                    ReferralBonus.referrer_id,
                    func.coalesce(func.sum(ReferralBonus.days_added), 0),
                )
                .filter(ReferralBonus.referrer_id.in_(referrer_ids))
                .group_by(ReferralBonus.referrer_id)
                .all()
            )
            if referrer_ids
            else {}
        )
        return [
            {
                "user": serialize_user(item, emails.get(item.id)),
                "referrals_count": count,
                "bonus_days": bonus_days.get(item.id, 0),
            }
            for item, count in rows
        ]
