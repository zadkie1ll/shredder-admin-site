# database.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from dotenv import load_dotenv

load_dotenv()

# В .env должен быть прописан DATABASE_URL
engine = create_engine(os.getenv("DATABASE_URL"), pool_pre_ping=True)
session_factory = sessionmaker(bind=engine)
db_session = scoped_session(session_factory)

def get_db():
    return db_session()