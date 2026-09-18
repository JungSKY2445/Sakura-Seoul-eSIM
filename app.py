"""
LINE eSIM Bot - メインアプリケーション
Amazon eSIM自動発行システム
"""
import io
import re
import secrets
import functools
import logging
import requests as http_requests
from flask import Flask, request, abort, jsonify
from PIL import Image, ImageDraw, ImageFont

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    ImageMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    FollowEvent,
)

import config
import database as db
import messages as msg
import amazon_api

# Flask app
app = Flask(__name__)

# DB 자동 초기화 (서버 시작 시)
db.init_db()

# LINE SDK v3 설정
line_config = Configuration(access_token=config.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(config.LINE_CHANNEL_SECRET)

# Amazon 주문번호 정규식 (예: 250-1234567-1234567)
AMAZON_ORDER_PATTERN = re.compile(r'^\d{3}-\d{7}-\d{7}$')

logger = logging.getLogger(__name__)


# ========== Admin 인증 ==========

def require_admin(f):
    """Admin API 키 인증 데코레이터"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not config.ADMIN_API_KEY:
            logger.warning("ADMIN_API_KEY not set — admin access blocked")
            return jsonify({"error": "Admin access not configured"}), 403

        # Header: Authorization: Bearer <key>  또는  ?key=<key>
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided_key = auth[7:]
        else:
            provided_key = request.args.get("key", "")

        if not provided_key or not secrets.compare_digest(provided_key, config.ADMIN_API_KEY):
            logger.warning(f"Admin auth failed from {request.remote_addr}")
            return jsonify({"error": "Unauthorized"}), 401

        return f(*args, **kwargs)
    return decorated


# ========== Webhook Endpoint ==========

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info(f"Request body: {body}")

    # LINE Verify 요청 (빈 events) 대응
    if not body or body == "{}":
        return "OK"

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature")
        abort(400)
    except Exception as e:
        app.logger.error(f"Error: {e}")
        return "OK"

    return "OK"


@app.route("/health", methods=["GET"])
def health():
    """기본 헬스체크 (인증 불필요)"""
    return {"status": "ok"}


@app.route("/admin/stats", methods=["GET"])
@require_admin
def admin_stats():
    """재고 현황 (인증 필요)"""
    stats = db.get_inventory_stats()
    return jsonify({"status": "ok", "inventory": stats})


# ========== Event Handlers ==========

@handler.add(FollowEvent)
def handle_follow(event):
    """친구 추가 시 환영 메시지"""
    reply(event, msg.WELCOME_MESSAGE)


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    """텍스트 메시지 처리"""
    user_id = event.source.user_id
    text = event.message.text.strip()

    # 세션 가져오기
    session = db.get_or_create_session(user_id)
    current_step = session["current_step"]

    # ── 리치 메뉴 버튼 액션 처리 ──
    if text == msg.ACTION_QR_ISSUE:
        db.update_session(user_id, "awaiting_order_number_qr")
        reply(event, msg.ASK_ORDER_NUMBER)
        return

    if text == msg.ACTION_HOW_TO_USE:
        db.update_session(user_id, "idle")
        reply(event, msg.HOW_TO_USE_GUIDE)
        return

    if text == msg.ACTION_CHECK_ORDER:
        db.update_session(user_id, "awaiting_order_number_check")
        reply(event, msg.ASK_ORDER_NUMBER_CHECK)
        return

    if text == msg.ACTION_CONTACT:
        db.update_session(user_id, "idle")
        reply(event, msg.CONTACT_MESSAGE)
        return

    # ── QR코드 발행 플로우: 주문번호 입력 대기 중 ──
    if current_step == "awaiting_order_number_qr":
        handle_qr_issue(event, user_id, text)
        return

    # ── 주문 확인 플로우: 주문번호 입력 대기 중 ──
    if current_step == "awaiting_order_number_check":
        handle_order_check(event, user_id, text)
        return

    # ── 기본 메시지 (그 외) ──
    # "こんにちは", "hi" 등 인사에 대응
    greetings = ["こんにちは", "こんばんは", "おはよう", "hi", "hello", "ハロー"]
    if any(g in text.lower() for g in greetings):
        reply(event, msg.WELCOME_MESSAGE)
        return

    reply(event, msg.UNKNOWN_MESSAGE)


# ========== Business Logic ==========

def handle_qr_issue(event, user_id, text):
    """QR코드 발행 처리"""
    # 주문번호 형식 검증
    if not AMAZON_ORDER_PATTERN.match(text):
        reply(event, msg.ORDER_NOT_FOUND)
        return

    # 주문 조회
    order = db.get_order_by_amazon_id(text)
    if not order:
        # order_id로도 시도
        order = db.get_order_by_id(text)

    if not order:
        reply(event, msg.ORDER_NOT_FOUND)
        db.update_session(user_id, "idle")
        return

    # 이미 전달 완료된 경우
    if order["status"] == "delivered":
        reply(event, msg.ORDER_ALREADY_DELIVERED)
        db.update_session(user_id, "idle")
        return

    # eSIM이 매칭되어 있는지 확인
    if not order.get("qr_code_data"):
        reply(event, "⏳ eSIMの準備中です。しばらくお待ちください。")
        db.update_session(user_id, "idle")
        return

    # QR코드와 함께 eSIM 정보 전달
    delivery_text = msg.ESIM_DELIVERED.format(
        order_id=order.get("amazon_order_id") or order["order_id"],
        plan_name=order.get("plan_name", "—"),
        data_amount=order.get("data_amount", "—"),
        validity_days=order.get("validity_days", "—"),
    )

    messages = []

    # QR코드 이미지가 있으면 이미지 메시지 추가
    qr_code_data = order.get("qr_code_data")
    if qr_code_data and qr_code_data.startswith("http"):
        messages.append(
            ImageMessage(
                original_content_url=qr_code_data,
                preview_image_url=qr_code_data,
            )
        )

    messages.append(TextMessage(text=delivery_text))

    # 전달 완료 처리
    db.mark_esim_delivered(order["order_id"])
    db.update_session(user_id, "idle")

    reply_messages(event, messages)


def handle_order_check(event, user_id, text):
    """주문 확인 처리"""
    if not AMAZON_ORDER_PATTERN.match(text):
        reply(event, msg.ORDER_NOT_FOUND)
        return

    order = db.get_order_by_amazon_id(text)
    if not order:
        order = db.get_order_by_id(text)

    if not order:
        reply(event, msg.ORDER_NOT_FOUND)
        db.update_session(user_id, "idle")
        return

    status_text = msg.STATUS_MAP.get(order["status"], order["status"])
    info_text = msg.ORDER_STATUS_INFO.format(
        order_id=order.get("amazon_order_id") or order["order_id"],
        status_text=status_text,
        plan_name=order.get("plan_name", "—"),
        data_amount=order.get("data_amount", "—"),
    )

    reply(event, info_text)
    db.update_session(user_id, "idle")


# ========== Reply Helpers ==========

def reply(event, text):
    """단일 텍스트 메시지 응답"""
    with ApiClient(line_config) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def reply_messages(event, messages):
    """복수 메시지 응답"""
    with ApiClient(line_config) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=messages,
            )
        )


# ========== Admin: Order Sync ==========

@app.route("/admin/sync-orders", methods=["GET"])
@require_admin
def sync_orders():
    """Amazon 주문 수동 동기화"""
    hours = request.args.get("hours", 24, type=int)
    result = amazon_api.sync_orders(hours_back=hours)
    return jsonify(result)


# ========== Admin: 업로드 페이지 ==========

UPLOAD_PAGE_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>eSIM Admin</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, sans-serif; background: #f5f5f5; padding: 20px; }
.card { background: #fff; border-radius: 12px; padding: 24px; margin-bottom: 16px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1); max-width: 600px; margin: 16px auto; }
h1 { font-size: 20px; margin-bottom: 16px; color: #333; }
h2 { font-size: 16px; margin-bottom: 12px; color: #555; }
.upload-area { border: 2px dashed #ccc; border-radius: 8px; padding: 32px;
               text-align: center; cursor: pointer; transition: border-color 0.2s; }
.upload-area:hover { border-color: #2196F3; }
.upload-area.dragover { border-color: #2196F3; background: #E3F2FD; }
input[type="file"] { display: none; }
.btn { background: #2196F3; color: #fff; border: none; padding: 12px 24px;
       border-radius: 8px; font-size: 15px; cursor: pointer; width: 100%; margin-top: 12px; }
.btn:hover { background: #1976D2; }
.btn:disabled { background: #ccc; cursor: not-allowed; }
.btn-danger { background: #f44336; }
.btn-danger:hover { background: #d32f2f; }
.result { margin-top: 16px; padding: 16px; border-radius: 8px; font-size: 14px; display: none; }
.result.success { background: #E8F5E9; color: #2E7D32; }
.result.error { background: #FFEBEE; color: #C62828; }
.stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.stat { background: #f9f9f9; padding: 12px; border-radius: 8px; text-align: center; }
.stat-num { font-size: 24px; font-weight: bold; color: #2196F3; }
.stat-label { font-size: 12px; color: #888; margin-top: 4px; }
.loading { display: none; text-align: center; margin-top: 12px; color: #888; }
</style>
</head>
<body>
<div class="card">
  <h1>📱 eSIM 在庫管理</h1>
  <div id="stats"><div class="loading" style="display:block">読み込み中...</div></div>
</div>

<div class="card">
  <h2>📤 在庫アップロード</h2>
  <div class="upload-area" id="dropZone" onclick="document.getElementById('fileInput').click()">
    <p style="font-size:32px;margin-bottom:8px">📁</p>
    <p>Excelファイルをドラッグ＆ドロップ</p>
    <p style="color:#aaa;font-size:13px;margin-top:4px">またはクリックして選択</p>
  </div>
  <input type="file" id="fileInput" accept=".xlsx,.xls">
  <div id="fileName" style="margin-top:8px;color:#555;font-size:14px"></div>
  <button class="btn" id="uploadBtn" onclick="upload()" disabled>アップロード</button>
  <div class="loading" id="uploadLoading">⏳ アップロード中...</div>
  <div class="result" id="uploadResult"></div>
</div>

<div class="card">
  <h2>🔄 Amazon注文同期</h2>
  <button class="btn" onclick="syncOrders()">注文を同期する</button>
  <div class="loading" id="syncLoading">⏳ 同期中...</div>
  <div class="result" id="syncResult"></div>
</div>

<script>
const KEY = new URLSearchParams(location.search).get('key') || '';
const BASE = location.origin;

// 統計
async function loadStats() {
  try {
    const r = await fetch(BASE + '/admin/stats?key=' + KEY);
    const d = await r.json();
    if (d.error) { document.getElementById('stats').innerHTML = '<p style="color:red">' + d.error + '</p>'; return; }
    const s = d.inventory;
    document.getElementById('stats').innerHTML =
      '<div class="stats">' +
      '<div class="stat"><div class="stat-num">' + s.total + '</div><div class="stat-label">総数</div></div>' +
      '<div class="stat"><div class="stat-num">' + s.unassigned + '</div><div class="stat-label">未割当</div></div>' +
      '<div class="stat"><div class="stat-num">' + s.assigned + '</div><div class="stat-label">割当済</div></div>' +
      '<div class="stat"><div class="stat-num">' + s.delivered + '</div><div class="stat-label">配信済</div></div>' +
      '</div>';
  } catch(e) { document.getElementById('stats').innerHTML = '<p style="color:red">読み込み失敗</p>'; }
}

// ファイル選択
const fileInput = document.getElementById('fileInput');
const dropZone = document.getElementById('dropZone');
let selectedFile = null;

fileInput.addEventListener('change', (e) => { selectFile(e.target.files[0]); });
dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', () => { dropZone.classList.remove('dragover'); });
dropZone.addEventListener('drop', (e) => { e.preventDefault(); dropZone.classList.remove('dragover'); selectFile(e.dataTransfer.files[0]); });

function selectFile(f) {
  if (!f) return;
  selectedFile = f;
  document.getElementById('fileName').textContent = '📎 ' + f.name;
  document.getElementById('uploadBtn').disabled = false;
}

// アップロード
async function upload() {
  if (!selectedFile) return;
  const btn = document.getElementById('uploadBtn');
  const loading = document.getElementById('uploadLoading');
  const result = document.getElementById('uploadResult');
  btn.disabled = true; loading.style.display = 'block'; result.style.display = 'none';

  try {
    const fd = new FormData();
    fd.append('file', selectedFile);
    const r = await fetch(BASE + '/admin/upload-inventory?key=' + KEY, { method: 'POST', body: fd });
    const d = await r.json();
    if (d.success) {
      result.className = 'result success';
      result.innerHTML = '✅ 追加: ' + d.added + '件 / スキップ(重複): ' + d.skipped_duplicates + '件' +
        (d.errors.length ? '<br>⚠️ エラー: ' + d.errors.join(', ') : '');
      loadStats();
    } else {
      result.className = 'result error';
      result.textContent = '❌ ' + (d.error || 'アップロード失敗');
    }
  } catch(e) {
    result.className = 'result error';
    result.textContent = '❌ 通信エラー: ' + e.message;
  }
  result.style.display = 'block'; loading.style.display = 'none'; btn.disabled = false;
}

// 注文同期
async function syncOrders() {
  const loading = document.getElementById('syncLoading');
  const result = document.getElementById('syncResult');
  loading.style.display = 'block'; result.style.display = 'none';
  try {
    const r = await fetch(BASE + '/admin/sync-orders?key=' + KEY);
    const d = await r.json();
    result.className = 'result success';
    result.innerHTML = '新規: ' + d.new + ' / マッチ: ' + d.matched + ' / スキップ: ' + d.skipped + ' / エラー: ' + d.errors;
  } catch(e) {
    result.className = 'result error';
    result.textContent = '❌ ' + e.message;
  }
  result.style.display = 'block'; loading.style.display = 'none';
}

loadStats();
</script>
</body>
</html>"""


@app.route("/admin/dashboard", methods=["GET"])
@require_admin
def admin_dashboard():
    """Admin 管理画面"""
    return UPLOAD_PAGE_HTML


# ========== Admin: eSIM 재고 업로드 ==========

def _parse_plan_days(plan_str):
    """Plan 문자열에서 유효기간(일) 파싱. 예: '1DAYS' → 1, '7DAYS' → 7, '30days' → 30"""
    if not plan_str:
        return 0
    plan_str = str(plan_str).strip().upper()
    m = re.search(r'(\d+)\s*DAY', plan_str)
    return int(m.group(1)) if m else 0


# 원본 Excel 컬럼명 → DB 컬럼명 매핑 (대소문자 무시)
COLUMN_MAP = {
    "iccid": "iccid",
    "sm-dp+address": "sm_dp_address",
    "sm_dp_address": "sm_dp_address",
    "smdp+address": "sm_dp_address",
    "activation code": "activation_code",
    "activation_code": "activation_code",
    "qr_code": "qr_code_data",
    "qr_code_data": "qr_code_data",
    "plan": "plan_name",
    "plan_name": "plan_name",
    "data_amount": "data_amount",
    "validity_days": "validity_days",
    "country": "country",
    "렌탈관리번호": "rental_number",
    "rental_number": "rental_number",
    "phone no.": "phone_number",
    "phone_number": "phone_number",
    "phone no": "phone_number",
}


@app.route("/admin/upload-inventory", methods=["POST"])
@require_admin
def upload_inventory():
    """Excel 파일로 eSIM 재고 업로드

    원본 형식 (SM-DP+Address, Activation Code, QR_CODE 등) 자동 인식
    """
    import openpyxl

    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        wb = openpyxl.load_workbook(io.BytesIO(f.read()), read_only=True)
        ws = wb.active

        # 헤더 읽기 + 컬럼 매핑
        raw_headers = [str(cell.value).strip() if cell.value else "" for cell in next(ws.iter_rows(min_row=1, max_row=1))]
        mapped_headers = []
        for h in raw_headers:
            key = h.lower().replace("+", "+")
            mapped = COLUMN_MAP.get(key, key)
            mapped_headers.append(mapped)

        # 필수 컬럼 확인
        required = {"iccid", "activation_code", "qr_code_data"}
        found = set(mapped_headers)
        if not required.issubset(found):
            missing = required - found
            return jsonify({
                "error": f"Missing columns: {', '.join(missing)}",
                "detected_columns": mapped_headers,
                "hint": "Expected: ICCID, SM-DP+Address (or sm_dp_address), Activation Code, QR_CODE (or qr_code_data)"
            }), 400

        added = 0
        skipped = 0
        errors = []

        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            data = dict(zip(mapped_headers, row))
            iccid = str(data.get("iccid", "")).strip()
            if not iccid:
                continue

            # Plan에서 validity_days 자동 파싱
            plan_name = str(data.get("plan_name", "") or "").strip()
            validity_days = data.get("validity_days")
            if not validity_days:
                validity_days = _parse_plan_days(plan_name)
            else:
                validity_days = int(validity_days)

            try:
                ok = db.add_esim(
                    iccid=iccid,
                    rental_number=str(data.get("rental_number", "") or "").strip() or None,
                    phone_number=str(data.get("phone_number", "") or "").strip() or None,
                    sm_dp_address=str(data.get("sm_dp_address", "") or "").strip(),
                    activation_code=str(data.get("activation_code", "") or "").strip(),
                    qr_code_data=str(data.get("qr_code_data", "") or "").strip(),
                    plan_name=plan_name,
                    data_amount=str(data.get("data_amount", "") or "").strip(),
                    validity_days=validity_days,
                    country=str(data.get("country", "JP") or "JP").strip(),
                )
                if ok:
                    added += 1
                else:
                    skipped += 1  # 중복 ICCID
            except Exception as e:
                errors.append(f"Row {row_idx}: {str(e)}")

        wb.close()
        return jsonify({
            "success": True,
            "added": added,
            "skipped_duplicates": skipped,
            "errors": errors,
        })

    except Exception as e:
        return jsonify({"error": f"File processing failed: {str(e)}"}), 400


# ========== Admin: Rich Menu Setup ==========

def _load_fonts():
    """CJK 폰트 로드"""
    font_paths = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    ]
    for fp in font_paths:
        try:
            return {
                "large": ImageFont.truetype(fp, 60),
                "small": ImageFont.truetype(fp, 36),
                "icon": ImageFont.truetype(fp, 40),
                "bar": ImageFont.truetype(fp, 48),
            }
        except Exception:
            continue
    df = ImageFont.load_default()
    return {"large": df, "small": df, "icon": df, "bar": df}


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _build_main_image(fonts):
    """메인 리치 메뉴 이미지 (2x2: 3기능 + チャット入力)"""
    W, H = 2500, 843
    CW, CH = W // 2, H // 2
    buttons = [
        {"label": "QRコード発行", "bg": "#2196F3", "desc": "eSIMを受け取る", "icon": "QR"},
        {"label": "使い方ガイド", "bg": "#4CAF50", "desc": "設定方法を確認", "icon": "?"},
        {"label": "注文確認",     "bg": "#FF9800", "desc": "注文状況をチェック", "icon": "!!"},
        {"label": "チャット入力", "bg": "#607D8B", "desc": "キーボードで入力", "icon": "Aa"},
    ]
    img = Image.new("RGB", (W, H), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    for i, btn in enumerate(buttons):
        col, row = i % 2, i // 2
        x0, y0 = col * CW, row * CH
        x1, y1 = x0 + CW, y0 + CH
        cx = x0 + CW // 2

        draw.rectangle([x0, y0, x1, y1], fill=_hex_rgb(btn["bg"]))
        draw.rectangle([x0, y0, x1, y1], outline="#FFFFFF", width=3)

        icon_cy = y0 + CH // 2 - 60
        r = 45
        draw.ellipse([cx-r, icon_cy-r, cx+r, icon_cy+r],
                     fill=None, outline="#FFFFFF", width=3)
        draw.text((cx, icon_cy), btn["icon"], fill="#FFFFFF",
                  font=fonts["icon"], anchor="mm")

        label_cy = y0 + CH // 2 + 25
        draw.text((cx, label_cy), btn["label"], fill="#FFFFFF",
                  font=fonts["large"], anchor="mm")
        draw.text((cx, label_cy + 55), btn["desc"], fill="#FFFFFFCC",
                  font=fonts["small"], anchor="mm")

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _build_keyboard_image(fonts):
    """키보드 모드 리치 메뉴 이미지 (메뉴로 돌아가기 바)"""
    W, H = 2500, 843
    img = Image.new("RGB", (W, H), "#37474F")
    draw = ImageDraw.Draw(img)
    draw.text((W // 2, H // 2), "▼ メニューを表示", fill="#FFFFFF",
              font=fonts["bar"], anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _line_api(method, url, token, **kwargs):
    """LINE API 호출 헬퍼"""
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    fn = getattr(http_requests, method)
    return fn(url, headers=headers, **kwargs)


@app.route("/admin/setup-richmenu", methods=["GET"])
@require_admin
def setup_rich_menu():
    """リッチメニュー (メイン ↔ キーボード切替) をワンクリック登録"""
    token = config.LINE_CHANNEL_ACCESS_TOKEN
    if not token:
        return jsonify({"error": "LINE_CHANNEL_ACCESS_TOKEN not set"}), 500

    results = {}

    # ── Step 0: 기존 리치 메뉴 & 에일리어스 전부 삭제 ──
    try:
        aliases = _line_api("get", "https://api.line.me/v2/bot/richmenu/alias/list",
                            token).json().get("aliases", [])
        for a in aliases:
            _line_api("delete",
                      f"https://api.line.me/v2/bot/richmenu/alias/{a['richMenuAliasId']}",
                      token)
    except Exception:
        pass

    try:
        old = _line_api("get", "https://api.line.me/v2/bot/richmenu/list",
                        token).json().get("richmenus", [])
        for m in old:
            _line_api("delete",
                      f"https://api.line.me/v2/bot/richmenu/{m['richMenuId']}", token)
    except Exception:
        pass

    fonts = _load_fonts()

    # ── Step 1: 메인 메뉴 (Menu A) 생성 ──
    W, H = 2500, 843
    CW, CH = W // 2, H // 2

    menu_a_data = {
        "size": {"width": W, "height": H},
        "selected": True,
        "name": "メインメニュー",
        "chatBarText": "メニューを開く",
        "areas": [
            {"bounds": {"x": 0,  "y": 0,  "width": CW, "height": CH},
             "action": {"type": "message", "label": "QRコード発行",
                        "text": "QRコード発行"}},
            {"bounds": {"x": CW, "y": 0,  "width": CW, "height": CH},
             "action": {"type": "message", "label": "使い方ガイド",
                        "text": "使い方ガイド"}},
            {"bounds": {"x": 0,  "y": CH, "width": CW, "height": CH},
             "action": {"type": "message", "label": "注文確認",
                        "text": "注文確認"}},
            {"bounds": {"x": CW, "y": CH, "width": CW, "height": CH},
             "action": {"type": "richmenuswitch",
                        "richMenuAliasId": "richmenu-keyboard",
                        "data": "switch-to-keyboard"}},
        ],
    }

    r = _line_api("post", "https://api.line.me/v2/bot/richmenu", token,
                  headers={"Content-Type": "application/json"}, json=menu_a_data)
    if r.status_code != 200:
        return jsonify({"error": "Menu A create failed", "detail": r.text}), 500
    menu_a_id = r.json()["richMenuId"]
    results["menuA"] = menu_a_id

    # Menu A 이미지 업로드
    r = _line_api("post",
                  f"https://api-data.line.me/v2/bot/richmenu/{menu_a_id}/content",
                  token, headers={"Content-Type": "image/png"},
                  data=_build_main_image(fonts))
    if r.status_code != 200:
        return jsonify({"error": "Menu A image failed", "detail": r.text}), 500

    # ── Step 2: 키보드 메뉴 (Menu B) 생성 ──
    menu_b_data = {
        "size": {"width": W, "height": H},
        "selected": False,
        "name": "キーボードモード",
        "chatBarText": "メニューを表示",
        "areas": [
            {"bounds": {"x": 0, "y": 0, "width": W, "height": H},
             "action": {"type": "richmenuswitch",
                        "richMenuAliasId": "richmenu-main",
                        "data": "switch-to-main"}},
        ],
    }

    r = _line_api("post", "https://api.line.me/v2/bot/richmenu", token,
                  headers={"Content-Type": "application/json"}, json=menu_b_data)
    if r.status_code != 200:
        return jsonify({"error": "Menu B create failed", "detail": r.text}), 500
    menu_b_id = r.json()["richMenuId"]
    results["menuB"] = menu_b_id

    # Menu B 이미지 업로드
    r = _line_api("post",
                  f"https://api-data.line.me/v2/bot/richmenu/{menu_b_id}/content",
                  token, headers={"Content-Type": "image/png"},
                  data=_build_keyboard_image(fonts))
    if r.status_code != 200:
        return jsonify({"error": "Menu B image failed", "detail": r.text}), 500

    # ── Step 3: 에일리어스 등록 (richmenuswitch 연결) ──
    for alias_id, mid in [("richmenu-main", menu_a_id),
                          ("richmenu-keyboard", menu_b_id)]:
        r = _line_api("post", "https://api.line.me/v2/bot/richmenu/alias", token,
                      headers={"Content-Type": "application/json"},
                      json={"richMenuAliasId": alias_id, "richMenuId": mid})
        if r.status_code != 200:
            return jsonify({"error": f"Alias {alias_id} failed",
                            "detail": r.text}), 500

    # ── Step 4: Menu A를 기본 리치 메뉴로 설정 ──
    r = _line_api("post",
                  f"https://api.line.me/v2/bot/user/all/richmenu/{menu_a_id}", token)
    if r.status_code != 200:
        return jsonify({"error": "set default failed", "detail": r.text}), 500

    results["success"] = True
    results["message"] = "リッチメニュー(メイン↔キーボード切替)が正常に登録されました！"
    return jsonify(results)


# ========== Startup ==========

if __name__ == "__main__":
    db.init_db()
    print("=" * 50)
    print("  LINE eSIM Bot Started")
    print(f"  Webhook URL: http://{config.HOST}:{config.PORT}/callback")
    print("=" * 50)
    app.run(host=config.HOST, port=config.PORT, debug=True)
