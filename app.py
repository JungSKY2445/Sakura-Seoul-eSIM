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


# ========== Admin: eSIM 재고 업로드 ==========

@app.route("/admin/upload-inventory", methods=["POST"])
@require_admin
def upload_inventory():
    """Excel 파일로 eSIM 재고 업로드

    Excel 컬럼: iccid, sm_dp_address, activation_code, qr_code_data,
                plan_name, data_amount, validity_days, country(옵션)
    """
    import openpyxl

    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        wb = openpyxl.load_workbook(io.BytesIO(f.read()), read_only=True)
        ws = wb.active

        # 헤더 읽기
        headers = [str(cell.value).strip().lower() if cell.value else "" for cell in next(ws.iter_rows(min_row=1, max_row=1))]
        required = {"iccid", "activation_code", "qr_code_data"}
        if not required.issubset(set(headers)):
            missing = required - set(headers)
            return jsonify({"error": f"Missing columns: {', '.join(missing)}"}), 400

        added = 0
        skipped = 0
        errors = []

        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            data = dict(zip(headers, row))
            iccid = str(data.get("iccid", "")).strip()
            if not iccid:
                continue

            try:
                ok = db.add_esim(
                    iccid=iccid,
                    sm_dp_address=str(data.get("sm_dp_address", "") or "").strip(),
                    activation_code=str(data.get("activation_code", "") or "").strip(),
                    qr_code_data=str(data.get("qr_code_data", "") or "").strip(),
                    plan_name=str(data.get("plan_name", "") or "").strip(),
                    data_amount=str(data.get("data_amount", "") or "").strip(),
                    validity_days=int(data.get("validity_days") or 0),
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
