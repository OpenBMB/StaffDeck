from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.db import get_session
from app.db.models import MockOrder, utc_now
from app.security.internal_service import require_internal_service

router = APIRouter(
    prefix="/api/mock",
    tags=["mock"],
    dependencies=[Depends(require_internal_service)],
)

PRODUCT_CATALOG = {
    "SKU-001": {
        "product_id": "SKU-001",
        "display_name": "SKU-001",
        "brand": "Mock",
        "price": Decimal("99.00"),
        "currency": "CNY",
        "spec": "standard",
    },
    "SKU-002": {
        "product_id": "SKU-002",
        "display_name": "SKU-002",
        "brand": "Mock",
        "price": Decimal("199.00"),
        "currency": "CNY",
        "spec": "standard",
    },
    "SKU-003": {
        "product_id": "SKU-003",
        "display_name": "SKU-003",
        "brand": "Mock",
        "price": Decimal("299.00"),
        "currency": "CNY",
        "spec": "standard",
    },
}

PRODUCT_NAME_CATALOG = {
    "iphone 15": {
        "product_id": "PHONE-IP15",
        "display_name": "iPhone 15",
        "brand": "Apple",
        "price": Decimal("4599.00"),
        "currency": "CNY",
        "spec": "128GB",
    },
    "三星s24": {
        "product_id": "PHONE-S24",
        "display_name": "三星 Galaxy S24",
        "brand": "Samsung",
        "price": Decimal("3999.00"),
        "currency": "CNY",
        "spec": "256GB",
    },
    "小米14": {
        "product_id": "PHONE-MI14",
        "display_name": "小米 14",
        "brand": "Xiaomi",
        "price": Decimal("3299.00"),
        "currency": "CNY",
        "spec": "256GB",
    },
    "a1": {
        "product_id": "A1",
        "display_name": "A1 标准商品",
        "brand": "Mock",
        "price": Decimal("129.00"),
        "currency": "CNY",
        "spec": "standard",
    },
    "a3": {
        "product_id": "A3",
        "display_name": "A3 高阶商品",
        "brand": "Mock",
        "price": Decimal("239.00"),
        "currency": "CNY",
        "spec": "pro",
    },
}

PRIMARY_ORDER_CENTER = {
    "ORDER-1001": {"status": "signed", "signed_days": 3, "refundable": True},
    "ORDER-1002": {"status": "signed", "signed_days": 16, "refundable": False},
}

ARCHIVE_ORDER_CENTER = {
    "ARCHIVE-1001": {
        "status": "signed",
        "signed_days": 4,
        "refundable": True,
        "archive_reason": "订单已归档到历史订单中心",
        "recommendation": "该历史订单签收 4 天，当前可继续发起售后退款审核。",
    }
}


class MockOrderQueryRequest(BaseModel):
    order_id: str


class MockOrderRefundRequest(BaseModel):
    order_id: str
    refund_reason: str


class MockProductPurchaseRequest(BaseModel):
    user_id: str = "user_demo"
    product_id: str
    sku_id: str | None = None
    quantity: int = Field(default=1, ge=1, le=99)
    payment_method: str = "mock_balance"


class MockProductPriceQueryRequest(BaseModel):
    product_name: str


class MockOrderAddRequest(BaseModel):
    user_id: str = "user_demo"
    order_id: str | None = None
    product_id: str
    sku_id: str | None = None
    quantity: int = Field(default=1, ge=1, le=99)
    status: str = "created"


class MockBenefitReconcileRequest(BaseModel):
    user_id: str
    order_id: str
    member_level: str | None = None
    benefit_type: str | None = None
    benefit_campaign_id: str | None = None


class MockFulfillmentReroutePlanRequest(BaseModel):
    order_id: str
    user_id: str | None = None
    target_address: str | None = None
    expected_delivery_time: str | None = None
    allow_split_package: bool = False
    blocked_carriers: list[str] = Field(default_factory=list)
    member_level: str | None = None


@router.post("/order/query")
def mock_order_query(
    request: MockOrderQueryRequest, db: Session = Depends(get_session)
) -> dict[str, Any]:
    order_id = _normalize_id(request.order_id)
    dynamic_record = _find_dynamic_order(db, order_id)
    if dynamic_record:
        return _order_hit(order_id, "primary_order_center", dynamic_record)
    record = PRIMARY_ORDER_CENTER.get(order_id)
    if not record:
        return _order_miss(order_id, "primary_order_center")
    return _order_hit(order_id, "primary_order_center", record)


@router.post("/order/archive-query")
def mock_order_archive_query(request: MockOrderQueryRequest) -> dict[str, Any]:
    order_id = _normalize_id(request.order_id)
    record = ARCHIVE_ORDER_CENTER.get(order_id)
    if not record:
        return _order_miss(order_id, "archive_order_center")
    return _order_hit(order_id, "archive_order_center", record)


@router.post("/order/refund")
def mock_order_refund(
    request: MockOrderRefundRequest, db: Session = Depends(get_session)
) -> dict[str, Any]:
    order_id = _normalize_id(request.order_id)
    row = db.get(MockOrder, order_id)
    if row is None:
        return {
            **_order_miss(order_id, "primary_order_center"),
            "refunded": False,
        }
    if row.status == "refunded":
        return {
            **_order_hit(
                order_id,
                "primary_order_center",
                _find_dynamic_order(db, order_id) or {},
            ),
            "refunded": True,
            "refund_reason": str(
                (row.metadata_json or {}).get("refund_reason") or request.refund_reason
            ),
            "idempotent_replay": True,
        }
    if not row.refundable:
        return {
            **_order_hit(
                order_id,
                "primary_order_center",
                _find_dynamic_order(db, order_id) or {},
            ),
            "refunded": False,
            "refund_reason": request.refund_reason,
            "failure_reason": "order_not_refundable",
        }
    metadata = dict(row.metadata_json or {})
    metadata.update(
        {
            "refund_id": f"REF{uuid4().hex[:10].upper()}",
            "refund_reason": request.refund_reason,
            "refunded_at": _now_iso(),
        }
    )
    row.status = "refunded"
    row.order_status = "refunded"
    row.payment_status = "refunded"
    row.refundable = False
    row.metadata_json = metadata
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        **_order_hit(order_id, "primary_order_center", _find_dynamic_order(db, order_id) or {}),
        "refunded": True,
        "refund_reason": request.refund_reason,
    }


@router.post("/product/purchase")
def mock_product_purchase(
    request: MockProductPurchaseRequest, db: Session = Depends(get_session)
) -> dict[str, Any]:
    record = _find_product_record(request.product_id)
    if not record:
        return _product_miss(request.product_id)
    unit_price = record["price"]
    total_amount = unit_price * Decimal(request.quantity)
    order_id = f"MOCK{uuid4().hex[:10].upper()}"
    result = {
        "found": True,
        "order_id": order_id,
        "purchase_id": f"PUR{uuid4().hex[:10].upper()}",
        "user_id": request.user_id,
        "product_id": record["product_id"],
        "display_name": record["display_name"],
        "sku_id": request.sku_id,
        "quantity": request.quantity,
        "unit_price": float(unit_price),
        "total_amount": float(total_amount),
        "currency": "CNY",
        "payment_method": request.payment_method,
        "payment_status": "paid",
        "order_status": "paid",
        "created_at": _now_iso(),
    }
    _upsert_dynamic_order(
        db,
        order_id=order_id,
        user_id=request.user_id,
        product_id=record["product_id"],
        sku_id=request.sku_id,
        quantity=request.quantity,
        status="paid",
        payment_status="paid",
        order_status="paid",
        total_amount=float(total_amount),
        currency="CNY",
        metadata={"purchase_id": result["purchase_id"], "payment_method": request.payment_method},
    )
    return result


@router.post("/member/benefit-reconcile")
def mock_member_benefit_reconcile(request: MockBenefitReconcileRequest) -> dict[str, Any]:
    order_id = _normalize_id(request.order_id)
    benefit_type = (request.benefit_type or "coupon").strip().lower()
    member_level = (request.member_level or "").strip().lower()
    eligible = member_level in {"black", "黑金", "vip_black", "black_card"}
    expected = [
        {
            "benefit_id": f"{benefit_type}_vip_shipping_delay",
            "benefit_type": benefit_type,
            "display_name": "会员履约保障券",
            "amount": 30,
            "currency": "CNY",
        }
    ]
    delivered = [] if eligible else expected
    missing = expected if eligible else []
    return {
        "found": True,
        "source": "mock_member_benefit_reconcile",
        "user_id": request.user_id,
        "order_id": order_id,
        "member_level": request.member_level,
        "benefit_campaign_id": request.benefit_campaign_id,
        "eligible": eligible,
        "expected_benefits": expected,
        "delivered_benefits": delivered,
        "missing_benefits": missing,
        "difference_reason": "benefit_delivery_task_failed" if eligible else "member_level_not_eligible",
        "recommended_action": "auto_reissue" if eligible else "explain_ineligible",
        "can_auto_compensate": eligible,
        "checked_at": _now_iso(),
    }


@router.post("/fulfillment/reroute-plan")
def mock_fulfillment_reroute_plan(request: MockFulfillmentReroutePlanRequest) -> dict[str, Any]:
    order_id = _normalize_id(request.order_id)
    high_priority = (request.member_level or "").strip().lower() in {"black", "黑金", "vip_black", "black_card"}
    reroutable = bool(request.target_address or request.expected_delivery_time or high_priority)
    plans = []
    if reroutable:
        plans = [
            {
                "plan_id": "same_city_priority",
                "plan_type": "upgrade_priority",
                "carrier": "mock_same_city",
                "estimated_delivery_time": request.expected_delivery_time or "2026-06-04T21:00:00+08:00",
                "risk": "可能受同城仓库存和骑手排班影响",
                "extra_fee": 0,
                "requires_split_package": bool(request.allow_split_package),
            },
            {
                "plan_id": "keep_current_route",
                "plan_type": "keep_route_with_urge",
                "carrier": "mock_standard",
                "estimated_delivery_time": "2026-06-05T12:00:00+08:00",
                "risk": "无需改仓，时效较慢但稳定",
                "extra_fee": 0,
                "requires_split_package": False,
            },
        ]
    return {
        "found": True,
        "source": "mock_fulfillment_reroute_plan",
        "order_id": order_id,
        "user_id": request.user_id,
        "reroutable": reroutable,
        "current_route": {
            "warehouse": "mock_east_warehouse",
            "carrier": "mock_standard",
            "status": "allocated",
        },
        "plans": plans,
        "recommended_plan_id": plans[0]["plan_id"] if plans else None,
        "requires_confirmation": reroutable,
        "failure_reason": None if reroutable else "order_not_in_reroute_window",
        "checked_at": _now_iso(),
    }


@router.post("/product/price-query", operation_id="mock_product_price_query")
@router.post("/product/price_query", operation_id="mock_product_price_query_legacy")
def mock_product_price_query(request: MockProductPriceQueryRequest) -> dict[str, Any]:
    product_name = request.product_name.strip()
    record = _find_product_record(product_name)
    if not record:
        return _product_miss(product_name)
    return {
        "product_name": product_name,
        "found": True,
        "source": "mock_product_price_catalog",
        "product_id": record["product_id"],
        "display_name": record["display_name"],
        "brand": record["brand"],
        "price": float(record["price"]),
        "currency": record["currency"],
        "spec": record["spec"],
        "updated_at": _now_iso(),
    }


@router.post("/order/add")
def mock_order_add(
    request: MockOrderAddRequest, db: Session = Depends(get_session)
) -> dict[str, Any]:
    record = _find_product_record(request.product_id)
    if not record:
        return _product_miss(request.product_id)
    unit_price = record["price"]
    total_amount = unit_price * Decimal(request.quantity)
    order_id = _normalize_id(request.order_id) if request.order_id else f"ADD{uuid4().hex[:10].upper()}"
    result = {
        "found": True,
        "order_id": order_id,
        "user_id": request.user_id,
        "product_id": record["product_id"],
        "display_name": record["display_name"],
        "sku_id": request.sku_id,
        "quantity": request.quantity,
        "unit_price": float(unit_price),
        "total_amount": float(total_amount),
        "currency": "CNY",
        "status": request.status,
        "created_at": _now_iso(),
    }
    _upsert_dynamic_order(
        db,
        order_id=order_id,
        user_id=request.user_id,
        product_id=record["product_id"],
        sku_id=request.sku_id,
        quantity=request.quantity,
        status=request.status,
        payment_status=None,
        order_status=request.status,
        total_amount=float(total_amount),
        currency="CNY",
        metadata={},
    )
    return result


def _find_product_record(value: str) -> dict[str, Any] | None:
    normalized_id = _normalize_id(value)
    if normalized_id in PRODUCT_CATALOG:
        return PRODUCT_CATALOG[normalized_id]

    normalized_name = _normalize_product_name(value)
    if normalized_name in PRODUCT_NAME_CATALOG:
        return PRODUCT_NAME_CATALOG[normalized_name]

    for record in (*PRODUCT_CATALOG.values(), *PRODUCT_NAME_CATALOG.values()):
        if _normalize_id(record["product_id"]) == normalized_id:
            return record
        if _normalize_product_name(record["display_name"]) == normalized_name:
            return record
    return None


def _product_miss(product_name: str) -> dict[str, Any]:
    return {
        "product_name": product_name,
        "found": False,
        "results": [],
        "miss_reason": "product_not_found",
        "hint": "可尝试使用 iPhone 15、三星S24、小米14、A1、A3 或 SKU-001/SKU-002/SKU-003 作为 mock 商品名。",
    }


def _normalize_id(value: str) -> str:
    return value.strip().upper()


def _normalize_product_name(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _order_hit(order_id: str, source: str, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "found": True,
        "source": source,
        **record,
    }


def _order_miss(order_id: str, source: str) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "found": False,
        "source": source,
        "results": [],
        "miss_reason": "source_miss",
        "hint": "当前订单中心未命中，可尝试其他已配置的订单查询工具。",
    }


def _find_dynamic_order(db: object, order_id: str) -> dict[str, Any] | None:
    if not isinstance(db, Session):
        return None
    row = db.get(MockOrder, order_id)
    if not row:
        return None
    return {
        "status": row.status,
        "signed_days": row.signed_days,
        "refundable": row.refundable,
        "user_id": row.user_id,
        "product_id": row.product_id,
        "sku_id": row.sku_id,
        "quantity": row.quantity,
        "payment_status": row.payment_status,
        "order_status": row.order_status,
        "total_amount": row.total_amount,
        "currency": row.currency,
        "created_at": row.created_at.isoformat(),
        "recommendation": "该订单已在 mock 订单中心创建，可继续进行订单查询、取消或售后流程。",
        **(row.metadata_json or {}),
    }


def _upsert_dynamic_order(
    db: object,
    *,
    order_id: str,
    user_id: str,
    product_id: str,
    sku_id: str | None,
    quantity: int,
    status: str,
    payment_status: str | None,
    order_status: str | None,
    total_amount: float,
    currency: str,
    metadata: dict[str, Any],
) -> None:
    if not isinstance(db, Session):
        return
    normalized_order_id = _normalize_id(order_id)
    row = db.get(MockOrder, normalized_order_id)
    now = utc_now()
    if not row:
        row = MockOrder(order_id=normalized_order_id, created_at=now)
    row.user_id = user_id
    row.product_id = product_id
    row.sku_id = sku_id
    row.quantity = quantity
    row.status = status
    row.payment_status = payment_status
    row.order_status = order_status
    row.signed_days = 0
    row.refundable = True
    row.total_amount = total_amount
    row.currency = currency
    row.metadata_json = metadata
    row.updated_at = now
    db.add(row)
    db.commit()


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()
