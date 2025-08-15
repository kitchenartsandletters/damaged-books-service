Damaged Books Service

A FastAPI service that reacts to Shopify inventory level updates and applies business rules for damaged books: decide whether to publish/unpublish the damaged‑book product, set SEO canonicals, and create/remove redirects to the “new book” product. It’s designed to be fed by your Webhook Gateway, which forwards Shopify webhooks with the original raw body and headers intact.

⸻

What’s working today
	•	✅ Inbound webhook: POST /webhooks/inventory-levels
Verifies Shopify HMAC using the App secret key (SHOPIFY_API_SECRET), parses JSON, and processes the event.
	•	✅ Variant & product resolution:
	1.	REST: variants.json?inventory_item_ids= with defensive post‑filtering; REST can return unrelated variants (full page), so post-filtering is applied to isolate correct variants. If filtered count is zero, a GraphQL fallback is always attempted to reliably map inventory_item_id → variant_id, product_id, handle. Logs include counts for both REST and GQL calls. Defensive handling for API rate limits and retries is in place.
	2.	GraphQL fallback: reliably maps inventory_item_id → variant_id, product_id, handle
	•	✅ Business rule entry point: used_book_manager.process_inventory_change(...)
Currently identifies damaged‑book handles and skips non‑damaged products (observed in logs).
	•	✅ Gateway integration:
Gateway forwards the raw JSON + Shopify headers and (optionally) an X-Available-Hint derived from the payload. Service accepts and logs this hint; it’s not yet damaged in decisions.
	•	✅ Railway deployment:
Correct start command and port; health endpoint responds {"status":"ok"}.

⸻

What’s intentionally not done yet
	•	⏳ Applying the available_hint to decision logic (we only log it today).
	•	⏳ Finalized inventory_service, seo_service, and redirect_service behaviors in production scenarios.
	•	⏳ Broader webhook topics (we currently focus on inventory_levels/update).
	•	⏳ Pagination for batch scans and richer notification channels.

⸻

High‑level flow

Shopify ──(inventory_levels/update)──> Webhook Gateway
   • Gateway validates Shopify HMAC
   • External Delivery Service → Signed POST with raw Shopify body + headers
   • Logs to Supabase, including delivery results with retries
   • Forwards raw body + Shopify headers to Damaged Books Service
              │
              ▼
Damaged Books Service
   1) Verifies Shopify HMAC again (defense‑in‑depth)
   2) Parses payload, extracts inventory_item_id (+ logs optional available_hint)
   3) Resolve variant/product:
        a) REST variants.json with post‑filtering
        b) GQL fallback if REST returns noisy page or filtered count is zero
   4) If handle indicates a damaged book:
        - Check stock (inventory_service)
        - Publish/unpublish (product_service)
        - Update canonical (seo_service)
        - Create/remove redirect (redirect_service)
   5) Return 200 always for app errors to avoid Shopify auto‑retries


⸻

Endpoints
	•	GET /health → {"status":"ok"}
	•	POST /webhooks/inventory-levels
Verified by X-Shopify-Hmac-Sha256. Expects Shopify inventory payload. Accepts forwarded requests from the Gateway with preserved Shopify headers and optional X-Gateway-* headers. Idempotency is expected if X-Gateway-Event-ID is present.
	•	(Utility) POST /api/products/check
Manually invoke process_inventory_change with product_id, variant_id, inventory_item_id.
	•	(Utility) POST /api/products/scan-all
Placeholder batch scan.
	•	(Utility) GET /api/products, GET /api/products/{product_id}, PUT /api/products/{product_id}/publish|unpublish
	•	(Utility) GET/POST/DELETE /api/redirects[...]
Redirect helpers (scaffolded).

⸻

Environment

Create .env (or set env vars in Railway):

SHOP_URL=your-store.myshopify.com
SHOPIFY_API_KEY=...              # required by client scaffolding
SHOPIFY_API_SECRET=...           # App secret key – must match the store’s webhook secret (distinct from Admin API secret)
SHOPIFY_ACCESS_TOKEN=shpat_...   # Admin API access token
LOG_LEVEL=INFO                   # optional
GATEWAY_HMAC_SECRET=...          # optional, for verifying Gateway signatures

Important: HMAC verification for Shopify webhooks must use the webhook secret, previoulsy described as App secret key (variable still retains name SHOPIFY_API_SECRET). This service expects the Gateway to pass through Shopify’s X-Shopify-Hmac-Sha256 header and the original raw body. No separate webhook secret is used here.

⸻

Run locally

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set env (or use .env)
export SHOP_URL=...
export SHOPIFY_API_SECRET=...
export SHOPIFY_ACCESS_TOKEN=...

python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
# http://127.0.0.1:8000/health  -> {"status":"ok"}

Local HMAC test

cat >/tmp/body.json <<'EOF'
{"inventory_item_id":123,"location_id":40052293765,"available":5}
EOF

HMAC=$(node -e "const fs=require('fs'),c=require('crypto');const b=fs.readFileSync('/tmp/body.json');console.log(c.createHmac('sha256', process.env.SHOPIFY_API_SECRET).update(b).digest('base64'))")

curl -i -X POST http://127.0.0.1:8000/webhooks/inventory-levels \
  -H "Content-Type: application/json" \
  -H "X-Shopify-Hmac-Sha256: $HMAC" \
  -H "X-Shopify-Topic: inventory_levels/update" \
  -H "X-Shopify-Shop-Domain: your-store.myshopify.com" \
  --data-binary @/tmp/body.json


⸻

Deploy on Railway
	•	Start command:
uvicorn backend.app.main:app --host 0.0.0.0 --port $PORT
	•	Ensure Public Networking is enabled and the service listens on $PORT.
	•	Set env vars (SHOP_URL, SHOPIFY_API_SECRET, SHOPIFY_ACCESS_TOKEN, etc.).
	•	Health check: GET /health should return {"status":"ok"}.

⸻

Gateway integration (what we expect)

The Webhook Gateway forwards the exact Shopify request:

Headers forwarded:
	•	X-Shopify-Hmac-Sha256 (required)
	•	X-Shopify-Topic (optional but logged)
	•	X-Shopify-Shop-Domain (optional but logged)

Optional extras (ignored for auth, useful for provenance/observability):
	•	X-Gateway-Signature / X-Gateway-Timestamp (gateway‑side HMAC)
	•	X-Available-Hint (a convenience header mirroring available from payload)

Body:
	•	Exact raw bytes from Shopify (no re‑serialization). We rely on this for correct HMAC verification and to avoid “null body” issues.

⸻

Implementation notes

HMAC verification
	•	We compute base64(hmac_sha256(app_secret, raw_body)) and compare with X-Shopify-Hmac-Sha256.
	•	A 401 is returned only when the signature doesn’t match. App errors return 200 to avoid Shopify retries, but we log them.

Variant/product resolution
	•	Shopify REST variants.json?inventory_item_ids= is occasionally noisy (can return a full page including unrelated variants).
	•	We post‑filter by inventory_item_id to isolate relevant variants.
	•	If the filtered count is zero, we always use a GraphQL fallback to resolve the variant and product exactly.
	•	Logs include counts of variants returned and filtered for REST, and variant/product mapping for GQL.
	•	Defensive handling for API rate limits and retries is implemented to ensure reliability.

Damaged‑book detection
	•	A product is considered a damaged book if its handle ends with one of:

-damaged-(light|moderate|heavy)

	•	Detection is case-insensitive.
	•	In the future, tag-based detection may be supported.
	•	If the product isn’t a damaged book, we skip (observed in production logs).

Business rules (scaffolded)
	•	inventory_service.is_variant_in_stock(variant_id, inventory_item_id) — source of truth for stock checks.
	•	product_service.set_product_publish_status(product_id, publish=True|False) — publish/unpublish damaged product.
	•	seo_service.update_used_book_canonicals(product, new_book_handle) — set canonical to the “new book.”
	•	redirect_service.create_redirect(from_handle, to_handle) / delete_redirect(redirect_id) — manage redirects.
	•	Publish/unpublish, SEO canonical, and redirect logic are idempotent and skip work if state is already correct.
	•	These run only when the handle is recognized as a damaged book.

⸻

Testing guide
	1.	Health
		curl -s https://<railway-domain>/health
	2.	Signed local webhook (above)
	3.	Gateway → Service end‑to‑end
	•	Trigger a real inventory change in Shopify (or send a signed test via Gateway).
	•	Confirm on the service:
		•	HMAC validated
		•	Variant/product resolved (GQL fallback if needed)
		•	Damaged‑book detection either “skip” or business rule applied
		•	200 OK response (Shopify won’t retry)
		•	Confirm on Gateway:
			•	webhook_logs row written
			•	external_deliveries row shows 200 and outcome body (e.g., "no-op" or "success")
	4.	Forwarded request from Gateway
	•	Verify both Shopify HMAC and optional Gateway HMAC signatures.
	•	Confirm Supabase logs the external delivery with retries.

⸻

Troubleshooting
	•	502 “Application failed to respond” (Railway):
		Start command must bind to 0.0.0.0:$PORT (not a fixed port like 8000) and Public Networking must be enabled.
	•	401 “Invalid HMAC signature”:
		Make sure you’re using the App secret key (SHOPIFY_API_SECRET) and that the body is the exact raw bytes Shopify sent. Don’t re‑stringify.
	•	401 with valid Shopify HMAC but invalid Gateway HMAC (if verifying both):
		Check GATEWAY_HMAC_SECRET and Gateway signature verification logic.
	•	400 “Missing HMAC header”:
		The Gateway must forward X-Shopify-Hmac-Sha256. If you hit the service directly with curl, you must compute and pass it.
	•	variants_count=250 with filtered=0:
		This indicates the REST endpoint returned a page that didn’t include your inventory_item_id. The service will use GraphQL fallback automatically. If you never see the GQL logs, deploy the latest build.
	•	“Product is not a damaged book, skipping”:
		The product handle didn’t match the damaged‑book pattern. That’s expected for most store inventory.
	•	Duplicate deliveries with X-Gateway-Event-ID:
		The service expects idempotency keyed by this header to prevent duplicate processing.

⸻

Roadmap
	•	Use available_hint to short‑circuit/validate stock checks.
	•	Harden inventory_service with Shopify Inventory APIs and/or GraphQL inventory queries.
	•	Expand to additional topics (e.g., variant updates affecting damaged/new mapping).
	•	Add structured logging and richer notifications (Slack/email).
	•	Batch pagination for scan_all_used_books.
	•	End‑to‑end integration tests.
	•	Implement optional Gateway HMAC verification.
	•	Support idempotency table keyed by X-Gateway-Event-ID.

⸻

Changelog (recent)
	•	Added GraphQL fallback for inventory_item_id → variant/product resolution.
	•	Gateway now forwards raw Shopify bytes + headers; service verifies HMAC reliably.
	•	Introduced optional X-Available-Hint pass‑through; service logs it.
	•	Fixed Railway start command to use $PORT.
	•	Cleaned up async usage (awaited client calls) and improved diagnostics.
	•	Added Gateway forwarding integration (raw body + headers).
	•	Implemented optional Gateway HMAC verification.
	•	Added external delivery logging with retries.
	•	Hardened REST + GraphQL variant resolution with defensive filtering and retry logic.

⸻

License

Private/internal.