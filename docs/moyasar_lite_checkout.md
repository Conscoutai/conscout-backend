# Moyasar checkout for ConScout Lite

## Intended purchase flow

1. The customer chooses `tier_1`, `tier_2`, or `tier_3` on the public Lite page.
2. The customer signs in and confirms billing details.
3. The landing server calls `POST /subscriptions/checkout-session`.
4. The backend reads the price, currency, and project limit from
   `core/subscription_plans.py`; browser-supplied price or allowance fields are
   rejected.
5. The customer completes the Moyasar-hosted card form.
6. The callback and `payment_paid` webhook fetch the payment from Moyasar and
   require matching payment ID, status, amount, currency, payment method, user,
   plan, and checkout metadata.
7. The backend changes the user's plan only after that verification succeeds.
8. Project creation enforces the server plan's 1/4/10/unlimited allowance.
   A client-side plan value cannot increase this allowance.
9. The saved Moyasar token is charged at `next_charge_at`. Successful renewal
   advances the billing period by one calendar month. Failed renewals become
   `past_due`, are retried, and eventually become `inactive`.
10. A signed-in customer can stop renewal from the plans page. Access remains
    active only through the already-paid `current_period_end`.

Admin approval is not part of this Lite purchase path. Both Lite admin-approval
backend entry points return `410`; legacy request records remain readable only
so they can be audited or rejected.

Each checkout includes a Moyasar `given_id`, and the backend requires the
returned payment ID to match it. Renewal attempts retain the same `given_id`
after a timeout, network interruption, or Moyasar `5xx` response. Checkout
configuration is encoded safely before being embedded in the hosted page.

## Local sandbox configuration

Obtain the test publishable and secret keys from the Moyasar dashboard. Store
them in the backend process environment; do not commit them.

```env
APP_SURFACE=lite
PUBLIC_API_BASE_URL=https://your-public-test-api.example
SUBSCRIPTION_PAYMENT_CURRENCY=USD
MOYASAR_PUBLISHABLE_KEY=pk_test_replace_me
MOYASAR_SECRET_KEY=sk_test_replace_me
MOYASAR_WEBHOOK_SECRET=replace_with_a_long_random_value
SUBSCRIPTION_RENEWAL_SCHEDULER_ENABLED=false
SUBSCRIPTION_RENEWAL_POLL_SECONDS=300
SUBSCRIPTION_RENEWAL_MAX_RETRIES=3
```

The landing server must point `LITE_API_BASE_URL` to that backend. Use HTTPS
outside local development; the landing proxy rejects a public HTTP API so login
credentials and bearer tokens are never forwarded over plaintext.

## Moyasar dashboard webhook

Create an HTTPS webhook:

```text
POST https://your-public-test-api.example/subscriptions/moyasar/webhook
```

Use the exact value configured as `MOYASAR_WEBHOOK_SECRET` and select:

- `payment_paid`
- payment failed
- `payment_refunded`
- `payment_voided`

The webhook body `live` flag must match the configured key environment.

## Required sandbox checks

- Tier 1 creates a USD 10.00 payment, Tier 2 USD 39.00, and Tier 3 USD 100.00.
- Changing a price in browser developer tools does not change the amount.
- A failed, initiated, mismatched, or unrelated payment does not change the plan.
- A paid payment with exact checkout metadata changes the plan and project limit.
- Replaying the callback or webhook does not activate twice.
- Submitting the same checkout twice does not create two payment IDs.
- The same payment ID cannot activate a different checkout.
- A superseded checkout that nevertheless reaches `paid` is fulfilled rather
  than leaving the customer charged without access.
- Closing the browser after payment still activates through the webhook.
- The central Admin Console and Lite API cannot approve a Lite plan without
  payment.
- Free access stops at one project, Tier 1 at four, Tier 2 at ten, and Tier 3
  remains unlimited when project creation is attempted directly against the API.
- Refund and void events deactivate the subscription tied to that payment.
- A deliberately due test subscription charges its saved token once and advances
  the period by one month.
- A simulated renewal timeout or `5xx` retry uses the same `given_id`.
- A delayed webhook from an older billing period cannot roll the subscription
  back from a newer period.
- A failed renewal enters `past_due`, retries according to configuration, and
  becomes inactive after the retry limit.
- Stopping renewal clears `next_charge_at`, does not refund the current period,
  and paid project access expires at `current_period_end`.

Keep `SUBSCRIPTION_RENEWAL_SCHEDULER_ENABLED=false` until the deliberately due
sandbox renewal succeeds.

## Controlled production switch

1. Audit existing Lite users whose subscription has
   `source=admin_approval` or `payment_status=approved`. Decide whether each
   account should receive a temporary grace period or complete a new Moyasar
   payment; these legacy records intentionally do not unlock paid limits.
2. Confirm the public API and callback use valid HTTPS.
3. Replace `pk_test_` and `sk_test_` with `pk_live_` and `sk_live_` in the
   deployment secret store.
4. Create the equivalent live webhook and secret.
5. Perform one controlled real plan purchase and verify the Moyasar dashboard,
   checkout session, user subscription, project limit, and webhook attempt.
6. Create projects up to the purchased allowance and verify the next creation is
   rejected by the Lite API.
7. Refund the controlled payment and verify access becomes inactive.
8. Verify the customer-facing **Stop renewal** action before enabling automatic
   renewal.
9. Enable the renewal scheduler only after the live first-payment lifecycle is
   confirmed.

Never place `MOYASAR_SECRET_KEY` in the landing application, Flutter application,
Git history, screenshots, or support messages.
