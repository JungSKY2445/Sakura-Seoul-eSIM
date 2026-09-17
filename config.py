import os
from dotenv import load_dotenv

load_dotenv()

# LINE Messaging API
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

# Server
PORT = int(os.getenv("PORT", 8000))
HOST = os.getenv("HOST", "0.0.0.0")

# Database (SQLite)
DB_PATH = os.path.join(os.path.dirname(__file__), "esim_bot.db")
