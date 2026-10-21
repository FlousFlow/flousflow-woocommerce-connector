{
    'name': 'FlousFlow WooCommerce Connector',
    'version': '19.0.4.3.7',
    'category': 'Sales/Sales',
    'summary': 'Bidirectional sync between Odoo 19 and WooCommerce',
    'description': (
        'FlousFlow WooCommerce Connector\n\n'
        'Production-grade bidirectional synchronization between Odoo 19 and WooCommerce.\n\n'
        'Features:\n'
        '- Multi-instance WooCommerce configuration\n'
        '- Product sync (WooCommerce <-> Odoo) with native variants and images\n'
        '- Warehouse stock export, customer sync, and draft quotation import\n'
        '- HMAC webhooks, fallback polling, retry queue, and audit logs\n'
        '- Store-scoped bindings, barcode identity, tax validation, and multi-company support\n\n'
        'Orders are always imported as draft quotations and require human review.\n'
        'Automatic sync uses incremental watermarks and bounded maintenance batches.'
    ),
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
    'images': [
        'static/description/cover.png',
        'static/description/icon.png',
    ],
}
