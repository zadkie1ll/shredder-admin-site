"""Read-only mappings owned by shredder-site rather than shredder-common."""

from sqlalchemy import BigInteger, Column, DateTime, String
from common.models.db import Base


class SiteIdentity(Base):
    __tablename__ = "site_identities"
    __table_args__ = {"info": {"read_only": True}}

    login = Column(String(256), primary_key=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    created_at = Column(DateTime, nullable=False)
