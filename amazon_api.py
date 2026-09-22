"""
Amazon SP-API 연동 모듈
주문 자동 수집 + eSIM 매칭
"""
import time
import logging
from datetime import datetime, timedelta, timezone

from sp_api.api import Orders
from sp_api.base import Marketplaces

import config
import database as db

logger = logging.getLogger(__name__)

# Amazon JP マーケットプレイス
MARKETPLACE = Marketplaces.JP


def get_credentials():
    """SP-API 인증 정보"""
    return {
        "refresh_token": config.SP_API_REFRESH_TOKEN,
        "lwa_app_id": config.SP_API_CLIENT_ID,
        "lwa_client_secret": config.SP_API_CLIENT_SECRET,
    }


def fetch_recent_orders(hours_back=24):
    """최근 주문 가져오기

    Args:
        hours_back: 몇 시간 전부터 가져올지 (기본 24시간)

    Returns:
        list: 주문 목록
    """
    creds = get_credentials()
    if not creds["refresh_token"]:
        logger.warning("SP-API credentials not configured")
        return []

    try:
        orders_api = Orders(credentials=creds, marketplace=MARKETPLACE)

        # 조회 시작 시간 (ISO8601 Z형식)
        after = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime('%Y-%m-%dT%H:%M:%SZ')

        response = orders_api.get_orders(
            CreatedAfter=after,
            OrderStatuses=["Unshipped", "PartiallyShipped", "Shipped"],
            MarketplaceIds=[MARKETPLACE.marketplace_id],
        )

        orders = response.payload.get("Orders", [])
        logger.info(f"Fetched {len(orders)} orders from Amazon")
        return orders

    except Exception as e:
        logger.error(f"SP-API order fetch failed: {e}")
        return []


def get_order_items(amazon_order_id):
    """주문의 상품 목록 가져오기"""
    creds = get_credentials()
    try:
        orders_api = Orders(credentials=creds, marketplace=MARKETPLACE)
        response = orders_api.get_order_items(amazon_order_id)
        items = response.payload.get("OrderItems", [])
        return items
    except Exception as e:
        logger.error(f"Failed to get items for {amazon_order_id}: {e}")
        return []


def is_esim_product(item):
    """eSIM 상품인지 확인 (SKU 기반)"""
    sku = item.get("SellerSKU", "")
    # eSIM SKU 패턴 매칭 (config에서 설정)
    esim_skus = config.ESIM_SKU_LIST
    if not esim_skus:
        # SKU 리스트 미설정 시 전체 주문 처리
        return True
    return sku in esim_skus


# SKU → 플랜명 매핑
SKU_TO_PLAN = {
    "SKUN1": "1DAYS",
    "SKUN3": "3DAYS",
    "SKUN5": "5DAYS",
    "SKUN7": "7DAYS",
    "SKUN10": "10DAYS",
    "SKUN15": "15DAYS",
    "SKUN20": "20DAYS",
    "SKUN30": "30DAYS",
    "SKUN60": "60DAYS",
    "SKUN90": "90DAYS",
}


def sync_orders(hours_back=24):
    """주문 동기화: Amazon → DB + eSIM 자동 매칭

    Returns:
        dict: 동기화 결과 {new, matched, skipped, errors}
    """
    result = {"new": 0, "matched": 0, "skipped": 0, "errors": 0}

    orders = fetch_recent_orders(hours_back)

    for order in orders:
        amazon_order_id = order.get("AmazonOrderId")
        if not amazon_order_id:
            continue

        # 이미 DB에 있는 주문은 스킵
        existing = db.get_order_by_amazon_id(amazon_order_id)
        if existing:
            result["skipped"] += 1
            continue

        try:
            # 주문 상품 확인
            items = get_order_items(amazon_order_id)

            # SP-API 레이트 리밋 대응 (1초 대기)
            time.sleep(1)

            esim_items = [it for it in items if is_esim_product(it)]
            if not esim_items:
                result["skipped"] += 1
                continue

            # 각 eSIM 상품에 대해 주문 생성 + 매칭
            for item in esim_items:
                qty = int(item.get("QuantityOrdered", 1))
                sku = item.get("SellerSKU", "")

                for q in range(qty):
                    # 주문 ID 생성 (수량 > 1이면 -1, -2 등 붙임)
                    suffix = f"-{q+1}" if qty > 1 else ""
                    order_id = f"{amazon_order_id}{suffix}"

                    # 주문 생성
                    buyer_email = order.get("BuyerInfo", {}).get("BuyerEmail", "")
                    created = db.create_order(
                        order_id=order_id,
                        amazon_order_id=amazon_order_id,
                        buyer_email=buyer_email,
                    )

                    if not created:
                        continue

                    result["new"] += 1

                    # SKU → 플랜 매칭
                    plan_name = SKU_TO_PLAN.get(sku)

                    # eSIM 자동 매칭 (플랜별)
                    esim = db.get_unassigned_esim(plan_name=plan_name)
                    if esim:
                        db.assign_esim_to_order(esim["id"], order_id)
                        result["matched"] += 1
                        logger.info(
                            f"Matched: {amazon_order_id} → eSIM {esim['iccid']}"
                        )
                    else:
                        logger.warning(
                            f"No eSIM available for order {amazon_order_id}"
                        )

        except Exception as e:
            logger.error(f"Error processing order {amazon_order_id}: {e}")
            result["errors"] += 1

    logger.info(f"Sync complete: {result}")
    return result


if __name__ == "__main__":
    """수동 실행 테스트"""
    logging.basicConfig(level=logging.INFO)
    print("=" * 50)
    print("  Amazon SP-API Order Sync")
    print("=" * 50)

    result = sync_orders(hours_back=48)
    print(f"\nResults:")
    print(f"  New orders:  {result['new']}")
    print(f"  Matched:     {result['matched']}")
    print(f"  Skipped:     {result['skipped']}")
    print(f"  Errors:      {result['errors']}")
