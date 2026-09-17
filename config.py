import os
from dotenv import load_dotenv

load_dotenv()

# LINE Messaging API
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

# Amazon SP-API
SP_API_REFRESH_TOKEN = os.getenv("SP_API_REFRESH_TOKEN", "")
SP_API_CLIENT_ID = os.getenv("SP_API_CLIENT_ID", "")
SP_API_CLIENT_SECRET = os.getenv("SP_API_CLIENT_SECRET", "")

# eSIM 상품 SKU 목록 (콤마 구분, 비어있으면 전체 주문 처리)
ESIM_SKU_LIST = [s.strip() for s in os.getenv("ESIM_SKU_LIST", "").split(",") if s.strip()]

# Server
PORT = int(os.getenv("PORT", 8000))
HOST = os.getenv("HOST", "0.0.0.0")

# Database (SQLite)
DB_PATH = os.path.join(os.path.dirname(__file__), "esim_bot.db")
