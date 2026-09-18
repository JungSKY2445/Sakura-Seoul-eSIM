"""
eSIM 재고 및 주문 매칭 데이터베이스 관리
"""
import sqlite3
from datetime import datetime
from config import DB_PATH


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """DB 테이블 초기화"""
    conn = get_connection()
    cursor = conn.cursor()

    # eSIM 재고 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS esim_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            iccid TEXT UNIQUE NOT NULL,
            rental_number TEXT,
            phone_number TEXT,
            sm_dp_address TEXT,
            activation_code TEXT,
            qr_code_data TEXT,
            qr_code_image_path TEXT,
            plan_name TEXT,
            data_amount TEXT,
            validity_days INTEGER,
            country TEXT DEFAULT 'JP',
            status TEXT DEFAULT 'unassigned'
                CHECK(status IN ('unassigned', 'assigned', 'delivered', 'expired')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 기존 테이블에 컬럼 추가 (마이그레이션 대응)
    for col in ["rental_number", "phone_number"]:
        try:
            cursor.execute(f"ALTER TABLE esim_inventory ADD COLUMN {col} TEXT")
        except Exception:
            pass  # 이미 존재

    # 주문 매칭 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE NOT NULL,
            amazon_order_id TEXT,
            buyer_email TEXT,
            esim_id INTEGER,
            line_user_id TEXT,
            qr_token TEXT UNIQUE,
            status TEXT DEFAULT 'pending'
                CHECK(status IN ('pending', 'matched', 'delivered')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            delivered_at TIMESTAMP,
            FOREIGN KEY (esim_id) REFERENCES esim_inventory(id)
        )
    """)

    # 기존 테이블에 qr_token 컬럼 추가 (마이그레이션)
    try:
        cursor.execute("ALTER TABLE order_mapping ADD COLUMN qr_token TEXT UNIQUE")
    except Exception:
        pass

    # LINE 유저 세션 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS line_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_user_id TEXT NOT NULL,
            current_step TEXT DEFAULT 'idle',
            temp_data TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


# ========== eSIM 재고 관리 ==========

def add_esim(iccid, sm_dp_address, activation_code, qr_code_data,
             plan_name, data_amount, validity_days, country="JP",
             rental_number=None, phone_number=None):
    """eSIM 재고 1건 추가"""
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO esim_inventory
            (iccid, rental_number, phone_number, sm_dp_address,
             activation_code, qr_code_data,
             plan_name, data_amount, validity_days, country)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (iccid, rental_number, phone_number, sm_dp_address,
              activation_code, qr_code_data,
              plan_name, data_amount, validity_days, country))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # 중복 ICCID
    finally:
        conn.close()


def get_unassigned_esim():
    """미할당 eSIM 1건 가져오기 (FIFO)"""
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM esim_inventory
        WHERE status = 'unassigned'
        ORDER BY id ASC LIMIT 1
    """).fetchone()
    conn.close()
    return dict(row) if row else None


def assign_esim_to_order(esim_id, order_id):
    """eSIM을 주문에 할당"""
    conn = get_connection()
    now = datetime.now().isoformat()
    conn.execute("""
        UPDATE esim_inventory SET status = 'assigned', updated_at = ?
        WHERE id = ?
    """, (now, esim_id))
    conn.execute("""
        UPDATE order_mapping SET esim_id = ?, status = 'matched'
        WHERE order_id = ?
    """, (esim_id, order_id))
    conn.commit()
    conn.close()


def mark_esim_delivered(order_id):
    """eSIM 전달 완료 처리"""
    conn = get_connection()
    now = datetime.now().isoformat()
    conn.execute("""
        UPDATE order_mapping SET status = 'delivered', delivered_at = ?
        WHERE order_id = ?
    """, (now, order_id))
    # eSIM 상태도 업데이트
    conn.execute("""
        UPDATE esim_inventory SET status = 'delivered', updated_at = ?
        WHERE id = (SELECT esim_id FROM order_mapping WHERE order_id = ?)
    """, (now, order_id))
    conn.commit()
    conn.close()


# ========== 주문 관리 ==========

def create_order(order_id, amazon_order_id=None, buyer_email=None):
    """주문 생성"""
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO order_mapping (order_id, amazon_order_id, buyer_email)
            VALUES (?, ?, ?)
        """, (order_id, amazon_order_id, buyer_email))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_order_by_id(order_id):
    """주문번호로 주문 조회"""
    conn = get_connection()
    row = conn.execute("""
        SELECT om.*, ei.iccid, ei.sm_dp_address, ei.activation_code,
               ei.qr_code_data, ei.plan_name, ei.data_amount, ei.validity_days
        FROM order_mapping om
        LEFT JOIN esim_inventory ei ON om.esim_id = ei.id
        WHERE om.order_id = ?
    """, (order_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_order_by_amazon_id(amazon_order_id):
    """아마존 주문번호로 조회"""
    conn = get_connection()
    row = conn.execute("""
        SELECT om.*, ei.iccid, ei.sm_dp_address, ei.activation_code,
               ei.qr_code_data, ei.plan_name, ei.data_amount, ei.validity_days
        FROM order_mapping om
        LEFT JOIN esim_inventory ei ON om.esim_id = ei.id
        WHERE om.amazon_order_id = ?
    """, (amazon_order_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def generate_qr_token(order_id):
    """주문에 대한 QR 토큰 생성/반환"""
    import secrets as _secrets
    conn = get_connection()
    # 이미 토큰이 있으면 반환
    row = conn.execute("SELECT qr_token FROM order_mapping WHERE order_id = ?",
                       (order_id,)).fetchone()
    if row and row["qr_token"]:
        conn.close()
        return row["qr_token"]
    # 새 토큰 생성
    token = _secrets.token_urlsafe(16)
    conn.execute("UPDATE order_mapping SET qr_token = ? WHERE order_id = ?",
                 (token, order_id))
    conn.commit()
    conn.close()
    return token


def get_order_by_qr_token(token):
    """QR 토큰으로 주문+eSIM 조회"""
    conn = get_connection()
    row = conn.execute("""
        SELECT om.*, ei.iccid, ei.sm_dp_address, ei.activation_code,
               ei.qr_code_data, ei.plan_name, ei.data_amount, ei.validity_days
        FROM order_mapping om
        LEFT JOIN esim_inventory ei ON om.esim_id = ei.id
        WHERE om.qr_token = ?
    """, (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ========== LINE 세션 관리 ==========

def get_or_create_session(line_user_id):
    """LINE 유저 세션 조회 또는 생성"""
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM line_sessions WHERE line_user_id = ?
    """, (line_user_id,)).fetchone()
    if not row:
        conn.execute("""
            INSERT INTO line_sessions (line_user_id) VALUES (?)
        """, (line_user_id,))
        conn.commit()
        row = conn.execute("""
            SELECT * FROM line_sessions WHERE line_user_id = ?
        """, (line_user_id,)).fetchone()
    conn.close()
    return dict(row)


def update_session(line_user_id, step, temp_data=None):
    """LINE 유저 세션 업데이트"""
    conn = get_connection()
    now = datetime.now().isoformat()
    conn.execute("""
        UPDATE line_sessions SET current_step = ?, temp_data = ?, updated_at = ?
        WHERE line_user_id = ?
    """, (step, temp_data, now, line_user_id))
    conn.commit()
    conn.close()


# ========== 통계 ==========

def get_inventory_stats():
    """재고 현황 통계"""
    conn = get_connection()
    stats = {}
    for status in ['unassigned', 'assigned', 'delivered', 'expired']:
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM esim_inventory WHERE status = ?
        """, (status,)).fetchone()
        stats[status] = row['cnt']
    stats['total'] = sum(stats.values())
    conn.close()
    return stats


if __name__ == "__main__":
    init_db()
    print("✓ Database initialized successfully")
    print(f"  Path: {DB_PATH}")
