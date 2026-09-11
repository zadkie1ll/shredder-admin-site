from database import session_factory
from common.models.db import User


class SQLAlchemyBackend:
    def authenticate(self, request, user_id=None):
        session = session_factory()
        try:
            return session.query(User).filter(User.id == user_id).first()
        finally:
            session.close()

    def get_user(self, user_id):
        session = session_factory()
        try:
            return session.query(User).filter(User.id == user_id).first()
        finally:
            session.close()
