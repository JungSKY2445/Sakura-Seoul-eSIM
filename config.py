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

# Admin API Key (admin 엔드포인트 인증용)
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

# Server
PORT = int(os.getenv("PORT", 8000))
HOST = os.getenv("HOST", "0.0.0.0")

# Database (SQLite)
# Railway Volume이 마운트되어 있으면 /data 사용, 아니면 로컬
_data_dir = "/data" if os.path.isdir("/data") else os.path.dirname(__file__)
DB_PATH = os.path.join(_data_dir, "esim_bot.db")
