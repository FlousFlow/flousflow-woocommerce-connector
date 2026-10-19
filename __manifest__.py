{
    'name': 'FlousFlow WooCommerce Connector',
    'version': '19.0.4.3.2',
    'category': 'Sales/Sales',
    'summary': 'Bidirectional sync between Odoo 19 and WooCommerce',
    'description': """
FlousFlow WooCommerce Connector
===============================
Production-grade bidirectional synchronization between Odoo 19 and WooCommerce.

Features:
---------
* Multi-instance WooCommerce configuration
* Product sync (Woo ↔ Odoo, configuration-controlled)
* Native product attribute and variation mapping
* Primary image and WooCommerce gallery import
* Accounting-safe Odoo category with Woo categories mapped as tags
* Stock/Quantity sync (Odoo → Woo, configuration-controlled)
* Customer sync (Woo → Odoo)
* Order sync (Woo → Odoo, as Quotations only)
* Webhook receiver with HMAC signature verification
* Fallback polling via scheduled actions
* Sync queue with retry logic
* Comprehensive audit log (woo.sync.log)
* Per-store product, variant, and customer bindings
* Barcode-first duplicate prevention with identity conflict blocking
* Tax mismatch detection and configurable handling
* Config-driven, multi-company ready

Key Design Decisions:
---------------------
* Orders ALWAYS created as Quotations (draft) — NEVER auto-confirmed
* Product/customer identities are scoped by WooCommerce configuration
* queue_job fallback: custom woo.sync.queue with ir.cron
* Webhook HMAC verification mandatory
* All sync operations logged in woo.sync.log
    """,
    'author': 'FlousFlow / Mohamed Gamal',
    'website': 'https://flousflow.com',
    'support': 'support@flousflow.com',
    'maintainer': 'FlousFlow',
    'depends': [
        'base',
        'sale_management',
        'stock',
        'product',
        'account',
    ],
    'external_dependencies': {
        'python': ['requests'],
    },
    'data': [
        'security/woo_security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'data/woo_sequence_data.xml',
        'views/woo_config_views.xml',
        'views/woo_sync_log_views.xml',
        'views/woo_sync_queue_views.xml',
        'views/product_template_views.xml',
        'views/sale_order_views.xml',
    ],
    'demo': [],
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
    'images': ['static/description/icon.png'],
}
