from __future__ import annotations

import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.auth import get_current_user
from backend.billing.plans import PLAN_LIMITS, PRICE_TO_PLAN, limits_for
from backend.config import settings
from backend.db import repositories as repo
from backend.models.schemas import Plan, PlanStatus, User

logger = logging.getLogger("checkpoint.billing")

router = APIRouter(prefix="/billing", tags=["billing"])

stripe.api_key = settings.STRIPE_SECRET_KEY


def _require_billing_enabled() -> None:
    if not settings.BILLING_ENABLED:
        raise HTTPException(503, "Billing is not configured on this deployment.")


@router.get("/plans")
def list_plans():
    """Public pricing info — no auth required so the marketing/pricing
    page can render it before the user signs in."""
    return {
        "billing_enabled": settings.BILLING_ENABLED,
        "plans": [
            {
                "id": plan.value,
                "label": limits.label,
                "max_active_sessions": limits.max_active_sessions,
                "max_actions_per_session": limits.max_actions_per_session,
            }
            for plan, limits in PLAN_LIMITS.items()
        ],
    }


@router.get("/me")
def my_billing(user: User = Depends(get_current_user)):
    limits = limits_for(user.effective_plan)
    return {
        "plan": user.plan.value,
        "plan_status": user.plan_status.value,
        "effective_plan": user.effective_plan.value,
        "has_payment_method_on_file": bool(user.stripe_customer_id),
        "limits": {
            "max_active_sessions": limits.max_active_sessions,
            "max_actions_per_session": limits.max_actions_per_session,
        },
    }


class CheckoutRequest(BaseModel):
    plan: Plan


@router.post("/checkout")
def create_checkout_session(req: CheckoutRequest, user: User = Depends(get_current_user)):
    _require_billing_enabled()

    if req.plan == Plan.FREE:
        raise HTTPException(400, "The free plan does not require checkout.")

    price_id = {
        Plan.PRO: settings.STRIPE_PRICE_PRO,
        Plan.TEAM: settings.STRIPE_PRICE_TEAM,
    }.get(req.plan)

    if not price_id:
        raise HTTPException(400, f"No Stripe price is configured for the {req.plan.value} plan.")

    customer_id = user.stripe_customer_id
    try:
        if not customer_id:
            customer = stripe.Customer.create(email=user.email, metadata={"checkpoint_user_id": user.id})
            customer_id = customer["id"]
            repo.set_stripe_customer_id(user.id, customer_id)

        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{settings.FRONTEND_BASE_URL}/app/?billing=success",
            cancel_url=f"{settings.FRONTEND_BASE_URL}/app/?billing=canceled",
            client_reference_id=user.id,
            metadata={"checkpoint_user_id": user.id},
        )
    except stripe.error.StripeError as exc:
        logger.exception("Stripe checkout session creation failed for user %s", user.id)
        raise HTTPException(502, "Could not start checkout. Please try again.") from exc

    return {"checkout_url": checkout_session["url"]}


@router.post("/portal")
def create_portal_session(user: User = Depends(get_current_user)):
    """The Stripe-hosted page for managing/canceling an existing
    subscription and viewing invoices."""
    _require_billing_enabled()

    if not user.stripe_customer_id:
        raise HTTPException(400, "No billing account on file yet — subscribe to a paid plan first.")

    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=f"{settings.FRONTEND_BASE_URL}/app/",
        )
    except stripe.error.StripeError as exc:
        logger.exception("Stripe portal session creation failed for user %s", user.id)
        raise HTTPException(502, "Could not open the billing portal. Please try again.") from exc

    return {"portal_url": portal_session["url"]}


def _plan_from_subscription(subscription) -> Plan:
    try:
        price_id = subscription["items"]["data"][0]["price"]["id"]
    except (KeyError, IndexError, TypeError):
        return Plan.FREE
    return PRICE_TO_PLAN.get(price_id, Plan.FREE)


def _status_from_subscription(subscription) -> PlanStatus:
    stripe_status = subscription.get("status")
    if stripe_status in ("active", "trialing"):
        return PlanStatus.ACTIVE
    if stripe_status in ("past_due", "unpaid", "incomplete"):
        return PlanStatus.PAST_DUE
    return PlanStatus.CANCELED


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request):
    """
    Stripe is the source of truth for subscription state. This endpoint
    is intentionally NOT behind get_current_user — Stripe calls it
    server-to-server — trust is established via signature verification
    instead, using the raw request body.

    Configure in the Stripe Dashboard:
      Endpoint URL: https://your-domain.com/billing/webhook
      Events: checkout.session.completed, customer.subscription.updated,
              customer.subscription.deleted
    """
    _require_billing_enabled()

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        logger.warning("Rejected webhook with invalid signature: %s", exc)
        raise HTTPException(400, "Invalid webhook signature.") from exc

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        user_id = data.get("client_reference_id") or data.get("metadata", {}).get("checkpoint_user_id")
        subscription_id = data.get("subscription")
        if user_id and subscription_id:
            subscription = stripe.Subscription.retrieve(subscription_id)
            plan = _plan_from_subscription(subscription)
            status_ = _status_from_subscription(subscription)
            repo.update_user_billing(user_id, plan=plan, plan_status=status_, stripe_subscription_id=subscription_id)
            logger.info("User %s upgraded to %s via checkout", user_id, plan.value)

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        customer_id = data.get("customer")
        user = repo.get_user_by_stripe_customer_id(customer_id) if customer_id else None
        if user:
            if event_type == "customer.subscription.deleted":
                repo.update_user_billing(
                    user.id, plan=Plan.FREE, plan_status=PlanStatus.CANCELED, stripe_subscription_id=None
                )
                logger.info("User %s subscription canceled, reverted to free", user.id)
            else:
                plan = _plan_from_subscription(data)
                status_ = _status_from_subscription(data)
                repo.update_user_billing(
                    user.id, plan=plan, plan_status=status_, stripe_subscription_id=data.get("id")
                )
                logger.info("User %s subscription updated: plan=%s status=%s", user.id, plan.value, status_.value)

    return {"received": True}
