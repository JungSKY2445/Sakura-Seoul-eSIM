"""
LINE リッチメニュー作成スクリプト
リッチメニュー画像の自動生成 + LINE APIへの登録
"""
import os
import sys
import json
import requests
from PIL import Image, ImageDraw, ImageFont

# 프로젝트 루트를 패스에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# ========== 설정 ==========
MENU_WIDTH = 2500
MENU_HEIGHT = 843  # 컴팩트 사이즈 (2500x843)
COLS = 2
ROWS = 2
CELL_W = MENU_WIDTH // COLS   # 1250
CELL_H = MENU_HEIGHT // ROWS  # 421

# 버튼 정의 (4칸: 2x2 그리드)
BUTTONS = [
    {"label": "QRコード発行", "action_text": "QRコード発行",
     "bg_color": "#2196F3", "desc": "eSIMを受け取る", "icon": "QR"},
    {"label": "使い方ガイド", "action_text": "使い方ガイド",
     "bg_color": "#4CAF50", "desc": "設定方法を確認", "icon": "?"},
    {"label": "注文確認",     "action_text": "注文確認",
     "bg_color": "#FF9800", "desc": "注文状況をチェック", "icon": "!!"},
    {"label": "お問い合わせ", "action_text": "お問い合わせ",
     "bg_color": "#9C27B0", "desc": "サポートへ連絡", "icon": "@"},
]

OUTPUT_IMAGE = os.path.join(os.path.dirname(__file__), "rich_menu.png")


def hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def create_rich_menu_image():
    """리치 메뉴 이미지 자동 생성 (Pillow)"""
    img = Image.new("RGB", (MENU_WIDTH, MENU_HEIGHT), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    # 시스템 폰트 탐색
    font_paths = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    font_large = None
    font_small = None
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                font_large = ImageFont.truetype(fp, 60)
                font_small = ImageFont.truetype(fp, 36)
                break
            except Exception:
                continue

    if not font_large:
        # CJK 폰트가 없으면 설치 시도
        print("⚠️  CJKフォントが見つかりません。デフォルトフォントを使用します。")
        print("   sudo apt install fonts-noto-cjk でインストールしてください。")
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # 아이콘용 폰트 (작은 볼드)
    font_icon = font_large  # fallback
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                font_icon = ImageFont.truetype(fp, 40)
                break
            except Exception:
                continue

    for i, btn in enumerate(BUTTONS):
        col = i % COLS
        row = i // COLS
        x0 = col * CELL_W
        y0 = row * CELL_H
        x1 = x0 + CELL_W
        y1 = y0 + CELL_H

        # 배경색 칠하기
        bg = hex_to_rgb(btn["bg_color"])
        draw.rectangle([x0, y0, x1, y1], fill=bg)

        # 셀 구분선
        draw.rectangle([x0, y0, x1, y1], outline="#FFFFFF", width=3)

        cx = x0 + CELL_W // 2
        # 아이콘 원 그리기 (윤곽선만, 채움 없음)
        icon_r = 45
        icon_cy = y0 + CELL_H // 2 - 60
        draw.ellipse(
            [cx - icon_r, icon_cy - icon_r, cx + icon_r, icon_cy + icon_r],
            fill=None, outline="#FFFFFF", width=3,
        )
        draw.text((cx, icon_cy), btn["icon"], fill="#FFFFFF",
                  font=font_icon, anchor="mm")

        # 메인 라벨
        label_cy = y0 + CELL_H // 2 + 25
        draw.text((cx, label_cy), btn["label"], fill="#FFFFFF",
                  font=font_large, anchor="mm")

        # 서브 설명
        desc_cy = label_cy + 55
        draw.text((cx, desc_cy), btn["desc"], fill="#FFFFFFCC",
                  font=font_small, anchor="mm")

    img.save(OUTPUT_IMAGE, "PNG")
    print(f"✓ リッチメニュー画像を生成しました: {OUTPUT_IMAGE}")
    return OUTPUT_IMAGE


def register_rich_menu():
    """LINE APIにリッチメニューを登録"""
    token = config.LINE_CHANNEL_ACCESS_TOKEN
    if not token or token == "your_channel_access_token_here":
        print("❌ LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
        print("   .env ファイルを確認してください。")
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # 리치 메뉴 구조 정의
    rich_menu_data = {
        "size": {"width": MENU_WIDTH, "height": MENU_HEIGHT},
        "selected": True,
        "name": "eSIM Japan メニュー",
        "chatBarText": "メニューを開く",
        "areas": [],
    }

    for i, btn in enumerate(BUTTONS):
        col = i % COLS
        row = i // COLS
        area = {
            "bounds": {
                "x": col * CELL_W,
                "y": row * CELL_H,
                "width": CELL_W,
                "height": CELL_H,
            },
            "action": {
                "type": "message",
                "label": btn["label"],
                "text": btn["action_text"],
            },
        }
        rich_menu_data["areas"].append(area)

    # Step 1: 리치 메뉴 생성
    print("📡 リッチメニューを作成中...")
    resp = requests.post(
        "https://api.line.me/v2/bot/richmenu",
        headers=headers,
        json=rich_menu_data,
    )

    if resp.status_code != 200:
        print(f"❌ リッチメニュー作成失敗: {resp.status_code}")
        print(resp.text)
        return None

    rich_menu_id = resp.json()["richMenuId"]
    print(f"✓ リッチメニュー作成完了: {rich_menu_id}")

    # Step 2: 이미지 업로드
    print("📡 画像をアップロード中...")
    img_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "image/png",
    }
    with open(OUTPUT_IMAGE, "rb") as f:
        resp2 = requests.post(
            f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
            headers=img_headers,
            data=f,
        )

    if resp2.status_code != 200:
        print(f"❌ 画像アップロード失敗: {resp2.status_code}")
        print(resp2.text)
        return None

    print("✓ 画像アップロード完了")

    # Step 3: 기본 리치 메뉴로 설정
    print("📡 デフォルトリッチメニューに設定中...")
    resp3 = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    if resp3.status_code != 200:
        print(f"❌ デフォルト設定失敗: {resp3.status_code}")
        print(resp3.text)
        return None

    print("✓ デフォルトリッチメニュー設定完了")
    print()
    print("=" * 50)
    print(f"  リッチメニューID: {rich_menu_id}")
    print("  全ユーザーに表示されます")
    print("=" * 50)

    return rich_menu_id


if __name__ == "__main__":
    print("=" * 50)
    print("  LINE リッチメニュー セットアップ")
    print("=" * 50)
    print()

    # Step 1: 이미지 생성
    create_rich_menu_image()
    print()

    # Step 2: LINE API 등록 (토큰이 있을 때만)
    if len(sys.argv) > 1 and sys.argv[1] == "--image-only":
        print("画像のみ生成しました（--image-only モード）")
    else:
        register_rich_menu()
