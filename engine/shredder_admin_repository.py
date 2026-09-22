"""Read-only queries against the authoritative Shredder product schema."""

from datetime import datetime
from sqlalchemy import String, cast, func, or_
from sqlalchemy.exc import SQLAlchemyError
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
