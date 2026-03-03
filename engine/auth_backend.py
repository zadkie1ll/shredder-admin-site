from sqlalchemy.orm import Session
from database import session_factory
from .models import User

class SQLAlchemyBackend:
    def authenticate(self, request, user_id=None):
        session = session_factory()
        user = session.query(User).filter(User.id == user_id).first()
        session.close()
        return user

    def get_user(self, user_id):
        session = session_factory()
        user = session.query(User).filter(User.id == user_id).first()
        session.close()
        return user