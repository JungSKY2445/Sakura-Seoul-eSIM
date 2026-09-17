"""
LINE eSIM Bot - メインアプリケーション
Amazon eSIM自動発行システム
"""
import io
import re
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

# Flask app
app = Flask(__name__)

# DB 자동 초기화 (서버 시작 시)
db.init_db()

# LINE SDK v3 설정
line_config = Configuration(access_token=config.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(config.LINE_CHANNEL_SECRET)

# Amazon 주문번호 정규식 (예: 250-1234567-1234567)
AMAZON_ORDER_PATTERN = re.compile(r'^\d{3}-\d{7}-\d{7}$')


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
    stats = db.get_inventory_stats()
    return {
        "status": "ok",
        "inventory": stats,
    }


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


# ========== Admin: Rich Menu Setup ==========

@app.route("/admin/setup-richmenu", methods=["GET"])
def setup_rich_menu():
    """リッチメニューをワンクリックで作成・登録"""
    token = config.LINE_CHANNEL_ACCESS_TOKEN
    if not token:
        return jsonify({"error": "LINE_CHANNEL_ACCESS_TOKEN not set"}), 500

    # ── Step 0: 기존 리치 메뉴 전부 삭제 ──
    auth_h = {"Authorization": f"Bearer {token}"}
    try:
        old_menus = http_requests.get(
            "https://api.line.me/v2/bot/richmenu/list", headers=auth_h
        ).json().get("richmenus", [])
        for m in old_menus:
            http_requests.delete(
                f"https://api.line.me/v2/bot/richmenu/{m['richMenuId']}",
                headers=auth_h,
            )
    except Exception:
        pass  # 기존 메뉴 없으면 무시

    # ── Step 1: 리치 메뉴 이미지 생성 (메모리) ──
    MENU_W, MENU_H = 2500, 843
    COLS, ROWS = 2, 2
    CELL_W, CELL_H = MENU_W // COLS, MENU_H // ROWS

    buttons = [
        {"label": "QRコード発行", "action": "QRコード発行",
         "bg": "#2196F3", "desc": "eSIMを受け取る", "icon": "QR"},
        {"label": "使い方ガイド", "action": "使い方ガイド",
         "bg": "#4CAF50", "desc": "設定方法を確認", "icon": "?"},
        {"label": "注文確認", "action": "注文確認",
         "bg": "#FF9800", "desc": "注文状況をチェック", "icon": "!!"},
        {"label": "お問い合わせ", "action": "お問い合わせ",
         "bg": "#9C27B0", "desc": "サポートへ連絡", "icon": "@"},
    ]

    img = Image.new("RGB", (MENU_W, MENU_H), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    # 폰트 로드
    font_large = font_small = font_icon = None
    font_paths = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    ]
    for fp in font_paths:
        try:
            font_large = ImageFont.truetype(fp, 60)
            font_small = ImageFont.truetype(fp, 36)
            font_icon = ImageFont.truetype(fp, 40)
            break
        except Exception:
            continue

    if not font_large:
        font_large = font_small = font_icon = ImageFont.load_default()

    def hex_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

    for i, btn in enumerate(buttons):
        col, row = i % COLS, i // COLS
        x0, y0 = col * CELL_W, row * CELL_H
        x1, y1 = x0 + CELL_W, y0 + CELL_H
        cx = x0 + CELL_W // 2

        draw.rectangle([x0, y0, x1, y1], fill=hex_rgb(btn["bg"]))
        draw.rectangle([x0, y0, x1, y1], outline="#FFFFFF", width=3)

        icon_cy = y0 + CELL_H // 2 - 60
        r = 45
        draw.ellipse([cx-r, icon_cy-r, cx+r, icon_cy+r],
                     fill=None, outline="#FFFFFF", width=3)
        draw.text((cx, icon_cy), btn["icon"], fill="#FFFFFF",
                  font=font_icon, anchor="mm")

        label_cy = y0 + CELL_H // 2 + 25
        draw.text((cx, label_cy), btn["label"], fill="#FFFFFF",
                  font=font_large, anchor="mm")
        draw.text((cx, label_cy + 55), btn["desc"], fill="#FFFFFFCC",
                  font=font_small, anchor="mm")

    # 이미지를 바이트로 변환
    img_bytes = io.BytesIO()
    img.save(img_bytes, "PNG")
    img_data = img_bytes.getvalue()

    # ── Step 2: LINE API에 리치 메뉴 등록 ──
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    rich_menu = {
        "size": {"width": MENU_W, "height": MENU_H},
        "selected": True,
        "name": "eSIM Japan メニュー",
        "chatBarText": "メニューを開く",
        "areas": [],
    }
    for i, btn in enumerate(buttons):
        col, row = i % COLS, i // COLS
        rich_menu["areas"].append({
            "bounds": {"x": col*CELL_W, "y": row*CELL_H,
                       "width": CELL_W, "height": CELL_H},
            "action": {"type": "message", "label": btn["label"],
                       "text": btn["action"]},
        })

    # 2-1: 리치 메뉴 생성
    r1 = http_requests.post("https://api.line.me/v2/bot/richmenu",
                            headers=headers, json=rich_menu)
    if r1.status_code != 200:
        return jsonify({"error": "create failed", "detail": r1.text}), 500

    menu_id = r1.json()["richMenuId"]

    # 2-2: 이미지 업로드
    r2 = http_requests.post(
        f"https://api-data.line.me/v2/bot/richmenu/{menu_id}/content",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "image/png"},
        data=img_data,
    )
    if r2.status_code != 200:
        return jsonify({"error": "image upload failed", "detail": r2.text}), 500

    # 2-3: 기본 리치 메뉴 설정
    r3 = http_requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{menu_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if r3.status_code != 200:
        return jsonify({"error": "set default failed", "detail": r3.text}), 500

    return jsonify({
        "success": True,
        "richMenuId": menu_id,
        "message": "リッチメニューが正常に登録されました！LINEアプリで確認してください。",
    })


# ========== Startup ==========

if __name__ == "__main__":
    db.init_db()
    print("=" * 50)
    print("  LINE eSIM Bot Started")
    print(f"  Webhook URL: http://{config.HOST}:{config.PORT}/callback")
    print("=" * 50)
    app.run(host=config.HOST, port=config.PORT, debug=True)
