# FlousFlow WooCommerce Connector

**Version:** 19.0.4.3.2  
**Author:** FlousFlow / Mohamed Gamal  
**License:** LGPL-3

## Overview

Production-grade bidirectional synchronization between Odoo 19 and WooCommerce. Supports multiple WooCommerce instances, webhook + polling dual transport, HMAC signature verification, exponential backoff retry, comprehensive audit logging, and config-driven behavior.

## Features

| Feature | Direction | Notes |
|---------|-----------|-------|
| **Product Sync** | Configurable | Store-scoped bindings, barcode identity, images, tags, and native variants |
| **Stock/Quantity Sync** | Odoo → Woo | Configured warehouse only; never writes Odoo quants on import |
| **Customer Sync** | Woo → Odoo | Store-scoped idempotent customer binding |
| **Order Sync** | Woo → Odoo | Draft quotation with currency and total validation |

## Architecture

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────┐
│  WooCommerce     │────────▶│  Webhook Controller│────────▶│  Odoo 19    │
│  (orders,        │  HMAC   │  /woo/webhook/:id  │         │             │
│   customers)     │         └──────────────────┘         │  Quotations │
└─────────────────┘                                       │  Products   │
        ▲                                                 │  Stock      │
        │                                                 │  Partners   │
        │  REST API (OAuth 1.0a)                          └─────────────┘
        │                                                        │
┌───────┴─────────┐                                      ┌───────┴──────┐
│  WooCommerce     │◀──────── Product/Stock Sync ────────│  Sync Queue  │
│  API             │                                      │  + ir.cron   │
└─────────────────┘                                      └──────────────┘
```

## Key Design Decisions

1. **Orders NEVER auto-confirmed:** All WooCommerce orders become Quotations (`state='draft'`). Manual review required.
2. **Store-scoped identity:** Dedicated bindings prevent IDs from different stores colliding
3. **Idempotency:** Store + Woo ID/delivery ID are unique; duplicate webhooks are safe
4. **Accounting category safety:** Imported products use the configured Odoo category; Woo categories become product tags
5. **Selective export:** A product is exported only after `Sync to WooCommerce` is enabled manually
6. **Strict SKU/barcode authority:** A Woo SKU is stored in the Odoo variant `barcode` field. It is never moved into `default_code`, and an Odoo-generated barcode is never pushed into an empty Woo SKU.
7. **Binding-first order identity:** A store-scoped variation binding is authoritative when importing order lines; barcode lookup is a fallback only.
8. **No `queue_job` dependency:** Custom `woo.sync.queue` model with `ir.cron` fallback
9. **Config-driven:** All settings per `woo.config` record, multi-instance ready
10. **Incremental auto-sync:** Product and customer imports use persisted high-water marks so completed runs request only records changed since the previous run. Product exports use the Odoo template `write_date` watermark.
11. **Bounded maintenance:** Log cleanup deletes at most 5,000 rows per SQL batch to avoid loading a million-row audit table into ORM memory.
12. **Operator-owned cron state:** Scheduled actions are loaded as `noupdate` data and never force an administrator's active/inactive choice during module upgrades.

## Installation

```bash
# 1. Place module in addons path
cp -r flousflow_woo_connector /path/to/odoo/addons/

# 2. Install dependencies
pip install requests

# 3. Install module
docker exec odoo_clean_web odoo --db_host=db --db_user=odoo --db_password=odoo \
  -i flousflow_woo_connector -d YOUR_DB --stop-after-init

# 4. Update (after changes)
docker exec odoo_clean_web odoo --db_host=db --db_user=odoo --db_password=odoo \
  -u flousflow_woo_connector -d YOUR_DB --stop-after-init
```

## Configuration

1. Go to **WooCommerce → Configurations**
2. Create a new configuration with:
   - **Website URL:** e.g., `https://yourstore.com`
   - **Consumer Key & Secret:** From WooCommerce → Settings → Advanced → REST API
   - **Webhook Secret:** Generate a random string, configure in both WooCommerce and Odoo
   - **Warehouse:** The Odoo warehouse to sync stock from
   - **Pricelist:** Must use the same currency as WooCommerce orders
   - **Shipping and Discount Products:** Service products used for financial order lines
   - **Sales Tax:** Optional explicit order-line tax; leave empty when Woo totals are tax-free
3. Click **Test Connection** to verify
4. Set up webhooks in WooCommerce pointing to:
   ```
   https://your-odoo-instance.com/woo/webhook/{config_id}
   ```
   Topics: `order.created`, `order.updated`, `customer.created`, `customer.updated`, `product.created`, `product.updated`

In **Auto Sync**, choose an API batch size from 1 to 100. The default (100)
minimizes HTTP pagination overhead while respecting WooCommerce's API limit.

## Models

| Model | Purpose |
|-------|---------|
| `woo.config` | WooCommerce instance configuration |
| `woo.sync.log` | Audit trail for every sync operation |
| `woo.sync.queue` | Async job queue with retry logic |
| `woo.connector` | AbstractModel — sync engine (no DB table) |
| `woo.product.binding` | Per-store Woo product to Odoo template identity |
| `woo.variant.binding` | Per-store Woo variation to Odoo variant identity |
| `woo.partner.binding` | Per-store Woo customer to Odoo partner identity |
| `woo.category.binding` | Per-store Woo category to Odoo product tag identity |
| `woo.product.image` | Idempotent local WooCommerce product gallery |

## Extended Standard Models

| Model | New Fields |
|-------|-----------|
| `product.template` | `sync_to_woo`, `woo_product_id`, `woo_last_sync`, `woo_sync_status`, `woo_variant_mapping` |
| `product.product` | Helper `_get_woo_sync_template()` |
| `res.partner` | `woo_customer_id`, `woo_last_sync`, `woo_raw_data` |
| `sale.order` | `woo_order_id`, `woo_payment_status`, `woo_payment_method`, `woo_transaction_id`, `woo_gateway_fee`, `woo_shipping_total`, `woo_raw_payload`, `woo_order_url`, `woo_config_id` |

## Scheduled Actions (ir.cron)

| Name | Interval | Purpose |
|------|----------|---------|
| Process Sync Queue | 5 min | Process pending queue items |
| Poll Recent Orders | 15 min | Fallback polling (safety net) |
| Cleanup Old Sync Logs | 7 days | Remove logs older than retention period |

## Running Tests

```bash
docker exec odoo_clean_web odoo --db_host=db --db_user=odoo --db_password=odoo \
  --test-enable -u flousflow_woo_connector -d aaa --stop-after-init
```

The automated suite covers configuration, products, native variants, customers,
orders, totals/currency, queue behavior, stock safety, images, categories,
barcode conflicts, selective export, and signed HTTP webhooks.

For HTTP webhook tests, run Odoo with `--workers=0`, a dedicated `--http-port`,
and a database filter selecting only the test database. The verified test run
completed 47 connector tests with 0 failures and 0 errors.

## Source control

This deployment currently lives under `/opt/odoo19/custom_addons` inside the
Odoo checkout. It is not a standalone Git repository and is not tracked by the
Odoo upstream remote. Create a dedicated private repository before the next
release so module changes, reviews, tags, and rollback artifacts are auditable.

## Security

- Webhook HMAC-SHA256 signature verification (secret is mandatory)
- Multi-company isolation via `company_id` + `_check_company_auto`
- Access groups: `WooCommerce User` (view) / `WooCommerce Manager` (configure)
- Consumer Secret stored in Odoo (encrypted if available)
- Raw payloads stored for audit but accessible only to authorized users

## Known Limitations

1. **No `queue_job`:** Uses custom queue; upgrade to OCA `queue_job` for production scale
2. **Tax mapping:** One explicit configured sales tax; complex cross-border mappings need separate rules
4. **No auto-confirm:** By design — all orders go through human review
5. **Image sync:** Relies on Odoo image URL being publicly accessible; binary upload not implemented yet

## Roadmap / Future

- [ ] OCA `queue_job` integration for production scale
- [x] Full product variant/attribute mapping and variation bindings
- [ ] Binary image upload (not URL-based)
- [ ] Advanced tax mapping with configurable rules
- [x] Two-way product sync (direction controlled per configuration)
- [ ] Dashboard with sync statistics
- [ ] Refund/return sync
