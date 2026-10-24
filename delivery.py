"""Delivery tracking and receipt validation."""

from datetime import datetime, timezone

from models import DeliveryStatus, can_transition


def _status(value):
    if isinstance(value, DeliveryStatus):
        return value
    return DeliveryStatus(value)


def transition_delivery(delivery, new_status, reason=""):
    """Validate and apply a legal delivery status transition."""
    current = _status(delivery.status)
    target = _status(new_status)

    if not can_transition(current, target):
        raise ValueError(
            f"Invalid delivery transition: "
            f"{current.value} -> {target.value}"
        )

    delivery.status = target

    if target == DeliveryStatus.DELAYED:
        delivery.delay_reason = reason.strip() or "Delay reported"

    if target == DeliveryStatus.FAILED:
        delivery.failure_reason = reason.strip() or "Delivery failed"

    return delivery


def confirm_delivery(delivery, received_qty, confirmed_by):
    """
    Record a recipient's reported quantity.
    Mismatches are flagged rather than silently accepted.
    """
    if _status(delivery.status) not in {
        DeliveryStatus.DISPATCHED,
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.DELAYED,
        DeliveryStatus.REROUTED,
    }:
        raise ValueError(
            "Delivery must be dispatched or in transit before confirmation"
        )

    if isinstance(received_qty, bool) or not isinstance(received_qty, int):
        raise ValueError("Received quantity must be a whole number")

    if received_qty < 0:
        raise ValueError("Received quantity cannot be negative")

    if received_qty > delivery.sent_qty:
        raise ValueError("Received quantity cannot exceed sent quantity")

    if not confirmed_by or not confirmed_by.strip():
        raise ValueError("Recipient confirmation name is required")

    delivery.received_qty = received_qty
    delivery.confirmed_by = confirmed_by.strip()
    delivery.confirmed_at = datetime.now(timezone.utc).isoformat()

    if received_qty != delivery.sent_qty:
        return {
            "ok": False,
            "mismatch": True,
            "expected_qty": delivery.sent_qty,
            "received_qty": received_qty,
            "message": "Quantity mismatch: investigate before closing delivery.",
            "delivery": delivery,
        }

    transition_delivery(delivery, DeliveryStatus.DELIVERED)

    return {
        "ok": True,
        "mismatch": False,
        "expected_qty": delivery.sent_qty,
        "received_qty": received_qty,
        "message": "Delivery confirmed.",
        "delivery": delivery,
    }