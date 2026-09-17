"""
LINE eSIM Bot - メインアプリケーション
Amazon eSIM自動発行システム
"""
import re
from flask import Flask, request, abort

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

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature")
        abort(400)

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


# ========== Startup ==========

if __name__ == "__main__":
    db.init_db()
    print("=" * 50)
    print("  LINE eSIM Bot Started")
    print(f"  Webhook URL: http://{config.HOST}:{config.PORT}/callback")
    print("=" * 50)
    app.run(host=config.HOST, port=config.PORT, debug=True)
