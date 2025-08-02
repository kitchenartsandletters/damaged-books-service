Here’s your comprehensive README.md for the refactored damaged-books-service:

# Damaged Books Service

This service manages the automated publication and redirection logic for **used/damaged books** in a Shopify store. When inventory status changes, the system determines whether to publish or unpublish the product, set SEO canonicals, and create or remove redirects as appropriate.

---

## 🧱 Project Structure

damaged-books-service/
├── backend/
│   └── app/
│       ├── main.py
│       └── routes.py
├── services/
│   ├── shopify_client.py
│   ├── product_service.py
│   ├── redirect_service.py
│   ├── notification_service.py
│   ├── seo_service.py         ← (Newly scaffolded)
│   ├── inventory_service.py   ← (Newly scaffolded)
│   └── used_book_manager.py
├── tasks/
│   └── …
├── config.py
├── .env
├── requirements.txt
└── README.md

---

## 🧠 Core Logic

### `used_book_manager.py`

- **process_inventory_change**
  - Gets product info from Shopify
  - Detects if it's a used book by handle pattern
  - Checks stock status via `inventory_service`
  - Updates SEO canonicals via `seo_service`
  - Publishes/unpublishes the product
  - Creates/removes redirect via `redirect_service`
  - Sends notifications via `notification_service`

- **scan_all_used_books**
  - Placeholder for batch processing all used book products

---

## 🔧 Shopify Integration

### Authentication

Shopify Admin API access is handled via environment variables:

```env
SHOP_URL=your-store.myshopify.com
SHOPIFY_API_KEY=xxx
SHOPIFY_API_SECRET=xxx
SHOPIFY_ACCESS_TOKEN=shpat_xxx

Webhook HMAC Verification

Shopify webhooks are verified using the Shopify API Secret Key (SHOPIFY_API_SECRET). No separate WEBHOOK_SECRET is used by Shopify.

⸻

⚙️ Environment Variables (.env)

SHOP_URL=your-store.myshopify.com
SHOPIFY_API_KEY=xxx
SHOPIFY_API_SECRET=xxx
SHOPIFY_ACCESS_TOKEN=xxx


⸻

🧪 Development

Requirements

pip install -r requirements.txt

Running Locally

uvicorn backend.app.main:app --reload

Railway Deployment Notes
	•	Project uses uvicorn with an ASGI FastAPI app.
	•	nixpacks will fail unless the directory includes a recognizable main.py entry point and requirements.txt.

⸻

✅ Recent Refactor Highlights
	•	Centralized Shopify configuration using config.py and pydantic.BaseSettings
	•	Removed redundant usage of WEBHOOK_SECRET, using SHOPIFY_API_SECRET for HMAC verification
	•	Converted shopify_client to a class-based structure
	•	Updated import references to use proper method access on instantiated shopify_client
	•	Scaffolding added:
	•	seo_service.py for canonical updates
	•	inventory_service.py for stock checks
	•	routes.py and shopify_client.py now import config properly via:

from config import settings



⸻

🧼 TODO
	•	Finalize scan_all_used_books() with real Shopify product pagination
	•	Expand notification_service to support email or Slack alerts
	•	Add webhook route for inventory updates
	•	Harden webhook HMAC validation and error logging