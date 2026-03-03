# database.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

# В .env должен быть прописан DATABASE_URL
engine = create_engine(os.getenv("DATABASE_URL"), pool_pre_ping=True)
session_factory = sessionmaker(bind=engine)