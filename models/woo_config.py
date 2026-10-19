import logging
import hashlib
import hmac
import base64

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class WooConfig(models.Model):
    """Configuration for a single WooCommerce instance connection.
    
    Supports multi-instance: each WooCommerce site gets its own config record.
    """
    _name = 'woo.config'
    _description = 'WooCommerce Configuration'
    _check_company_auto = True
    _order = 'name'

    # --- Basic Identity ---
    name = fields.Char(
        string=('Name'), required=True,
        help=('Descriptive name for this WooCommerce connection (e.g., "Main Store")'))
    active = fields.Boolean(
        default=True,
        help=('Uncheck to disable sync for this instance without deleting config.'))

    # --- Connection Details ---
    website_url = fields.Char(
        string=('Website URL'), required=True,
        help=('Full URL of the WooCommerce site, e.g., https://example.com. '
               'No trailing slash.'))
    consumer_key = fields.Char(
        string=('Consumer Key'), required=True,
        groups='flousflow_woo_connector.group_woo_manager',
        help=('WooCommerce REST API Consumer Key (from WooCommerce → Settings → Advanced → REST API).'))
    consumer_secret = fields.Char(
        string=('Consumer Secret'), required=True,
        groups='flousflow_woo_connector.group_woo_manager',
        help=('WooCommerce REST API Consumer Secret. Stored encrypted if possible.'))

    # --- Warehouse Mapping ---
    warehouse_id = fields.Many2one(
        'stock.warehouse', string=('Default Warehouse'),
        required=True, check_company=True,
        help=('Warehouse whose stock quantities are synced to WooCommerce.'))

    # --- Sync Direction ---
    sync_direction_products = fields.Selection(
        [('odoo_to_woo', ('Odoo → WooCommerce (Export Only)')),
         ('woo_to_odoo', ('WooCommerce → Odoo (Import Only)')),
         ('both', ('Bidirectional'))],
        string=('Product Sync Direction'),
        default='odoo_to_woo',
        required=True)

    # --- Product Auto-Creation ---
    auto_create_products = fields.Boolean(
        string=('Auto-Create Products'),
        default=False,
        help=('If enabled, products in WooCommerce orders that do not match any '
               'existing Odoo product will be auto-created. '
               '⚠️ NOT recommended for production — requires manual review.'))

    default_category_id = fields.Many2one(
        'product.category', string=('Default Import Category'),
        help=('All products imported from WooCommerce will be assigned to '
               'this category. If left empty, products will have no category. '
               'You can change categories manually later.'))

    # --- Tax Configuration ---
    woo_prices_include_tax = fields.Boolean(
        string=('WooCommerce Prices Include Tax'),
        default=False,
        help=('Whether product prices coming from WooCommerce already include tax. '
               'Must match your WooCommerce tax settings.'))
    tax_tolerance = fields.Float(
        string=('Tax Tolerance'),
        default=0.01,
        help=('Maximum difference allowed between WooCommerce order total and '
               'Odoo-computed total before a warning is logged.'))
    sale_tax_id = fields.Many2one(
        'account.tax', string=('WooCommerce Sales Tax'), check_company=True,
        domain=[('type_tax_use', '=', 'sale')],
        help=('Tax applied to imported product lines. Leave empty for a tax-free store.'))

    # --- Log Retention ---
    log_retention_days = fields.Integer(
        string=('Log Retention (Days)'),
        default=90,
        help=('Sync logs older than this will be auto-archived/purged by '
               'the weekly cleanup cron.'))

    # --- Webhook Secret ---
    webhook_secret = fields.Char(
        string=('Webhook Secret'),
        groups='flousflow_woo_connector.group_woo_manager',
        help=('Secret key for WooCommerce webhook HMAC signature verification. '
               'Generate a random string and configure it in both WooCommerce '
               'and here.'))

    # --- Company & Currency ---
    company_id = fields.Many2one(
        'res.company', string=('Company'), required=True,
        default=lambda self: self.env.company,
        help=('Company this WooCommerce instance belongs to.'))

    pricelist_id = fields.Many2one(
        'product.pricelist', string=('WooCommerce Pricelist'),
        check_company=True,
        help=('Sets the currency and pricing context of imported orders.'))
    website_pricelist_id = fields.Many2one(
        'product.pricelist', string=('Website Pricelist'),
        check_company=True,
        help=('Dedicated pricelist for website prices, separate from the main '
               'Odoo pricelist (which uses the sale price on the product card). '
               'When set, WooCommerce prices are stored here as per-variant fixed '
               'prices and the product card price (list_price) is never overwritten '
               'by the sync. An already-filled website price is never overwritten — '
               'only empty prices are filled.'))
    shipping_product_id = fields.Many2one(
        'product.product', string=('Shipping Product'),
        check_company=True,
        domain=[('type', '=', 'service')],
        help=('Service product used for WooCommerce shipping charges.'))
    discount_product_id = fields.Many2one(
        'product.product', string=('Discount Product'),
        check_company=True,
        domain=[('type', '=', 'service')],
        help=('Service product used for WooCommerce coupon discounts.'))
    fee_product_id = fields.Many2one(
        'product.product', string=('Fee Product'),
        check_company=True,
        domain=[('type', '=', 'service')],
        help=('Service product used for WooCommerce extra fee lines (e.g. COD / payment fees).'))
    woo_order_sequence_id = fields.Many2one(
        'ir.sequence', string=('WooCommerce Order Sequence'),
        check_company=True,
        help=('Sequence used to number imported WooCommerce orders (e.g. WOO00001). '
               'Leave empty to use the default sale.order sequence.'))
    strict_total_validation = fields.Boolean(
        string=('Block Total Mismatches'), default=True,
        help=('Reject imported orders when the Odoo total differs from WooCommerce.'))

    # --- Auto Sync Settings ---
    auto_sync_enabled = fields.Boolean(
        string=('Enable Auto Sync'),
        default=False,
        help=('Master switch: enable/disable ALL automatic synchronization. '
               'When OFF, no automated sync will run regardless of individual settings.'))

    auto_sync_interval = fields.Selection(
        [('5', ('Every 5 minutes')),
         ('15', ('Every 15 minutes')),
         ('30', ('Every 30 minutes')),
         ('60', ('Every 1 hour')),
         ('120', ('Every 2 hours')),
         ('360', ('Every 6 hours')),
         ('720', ('Every 12 hours')),
         ('1440', ('Once per day'))],
        string=('Auto Sync Interval'),
        default='15',
        required=True,
        help=('How often to run automatic synchronization. '
               'Lower intervals may increase server load.'))

    auto_sync_orders = fields.Boolean(
        string=('Auto-Pull Orders'), default=True,
        help=('Automatically import new/updated orders from WooCommerce.'))

    auto_sync_products = fields.Boolean(
        string=('Auto-Sync Products'), default=False,
        help=('Automatically export products marked "Sync to WooCommerce".'))

    auto_sync_customers = fields.Boolean(
        string=('Auto-Pull Customers'), default=False,
        help=('Automatically import customers from WooCommerce.'))

    sync_customers_from_orders = fields.Boolean(
        string=('Also Sync Customers From Orders'),
        default=False,
        help=('Also create/update Odoo partners from WooCommerce order '
               'billing data. Captures guest checkouts that have no customer '
               'account. Requires Auto-Pull Customers.'))

    customer_tag_id = fields.Many2one(
        'res.partner.category', string=('WooCommerce Customer Tag'),
        help=('Tag applied to every customer synced from this store. Use '
               '"Apply Customer Tag" to tag customers that were synced '
               'before this setting was configured.'))

    auto_sync_stock = fields.Boolean(
        string=('Auto-Sync Stock'), default=True,
        help=('Automatically update stock quantities in WooCommerce when they change in Odoo. '
               'This is always-on via stock.move triggers when enabled — the interval applies to periodic full sync.'))

    # --- Order Write-back ---
    auto_update_order_status = fields.Boolean(
        string=('Update Order Status in WooCommerce'),
        default=False,
        help=('When an imported quotation is confirmed or cancelled in Odoo, '
               'push the new status back to WooCommerce (confirm → "processing", '
               'cancel → "cancelled").'))

    payment_method_map_ids = fields.One2many(
        'woo.payment.method.map', 'config_id',
        string=('Payment Method Mapping'),
        help=('Map WooCommerce payment methods to Odoo payment terms applied '
               'to imported quotations.'))

    # --- Computed Fields ---
    api_base_url = fields.Char(
        string=('API Base URL'), compute='_compute_api_base_url', store=True)
    last_connection_test = fields.Datetime(
        string=('Last Connection Test'), readonly=True)
    connection_test_result = fields.Text(
        string=('Connection Test Result'), readonly=True)
    last_auto_sync = fields.Datetime(
        string=('Last Automatic Sync'), readonly=True, copy=False)
    last_product_export_sync = fields.Datetime(
        string=('Last Product Export'), readonly=True, copy=False)
    last_product_import_sync = fields.Datetime(
        string=('Last Product Import'), readonly=True, copy=False)
    last_customer_sync = fields.Datetime(
        string=('Last Customer Sync'), readonly=True, copy=False,
        help=('High-water mark used to fetch only customers changed since the '
              'last completed customer synchronization.'))
    auto_sync_batch_size = fields.Integer(
        string=('API Batch Size'), default=100, required=True,
        help=('Number of WooCommerce records requested per API page. Larger '
              'pages reduce HTTP overhead; the WooCommerce maximum is 100.'))
    last_order_poll = fields.Datetime(
        string=('Last Order Poll'), readonly=True, copy=False,
        help=('Start time of the last successful WooCommerce order polling cycle.'))

    _website_uniq = models.Constraint(
        'unique(website_url, company_id)',
        ('A WooCommerce configuration for this URL already exists in this company!'),
    )

    @api.constrains('auto_sync_batch_size')
    def _check_auto_sync_batch_size(self):
        for record in self:
            if not 1 <= record.auto_sync_batch_size <= 100:
                raise ValidationError(
                    _('API Batch Size must be between 1 and 100.'))

    @api.depends('website_url')
    def _compute_api_base_url(self):
        for record in self:
            if record.website_url:
                record.api_base_url = record.website_url.rstrip('/') + '/wp-json/wc/v3'
            else:
                record.api_base_url = False

    # -------------------------------------------------------------------------
    # Connection Test
    # -------------------------------------------------------------------------
    def action_test_connection(self):
        """Test the WooCommerce REST API connection."""
        self.ensure_one()
        try:
            connector = self.env['woo.connector']._get_connector(self)
            result = connector._test_connection()
            self.write({
                'last_connection_test': fields.Datetime.now(),
                'connection_test_result': result,
            })
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection Successful'),
                    'message': _('Successfully connected to %s') % self.website_url,
                    'type': 'success',
                    'sticky': False,
                }
            }
        except Exception as e:
            error_msg = str(e)
            self.write({
                'last_connection_test': fields.Datetime.now(),
                'connection_test_result': _('Error: %s') % error_msg,
            })
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection Failed'),
                    'message': error_msg,
                    'type': 'danger',
                    'sticky': True,
                }
            }

    # -------------------------------------------------------------------------
    # Manual Sync Actions
    # -------------------------------------------------------------------------

    def _queue_unique_batch(self, model_name, operation):
        """Create one active full-import job per store and operation."""
        self.ensure_one()
        existing = self.env['woo.sync.queue'].search([
            ('config_id', '=', self.id),
            ('operation', '=', operation),
            ('state', 'in', ['pending', 'processing']),
        ], limit=1)
        if existing:
            return existing, False
        return self.env['woo.sync.queue'].create({
            'config_id': self.id,
            'model_name': model_name,
            'operation': operation,
            'direction': 'woo_to_odoo',
            'payload': '{}',
            'priority': 10,
        }), True

    def action_sync_all_products(self):
        """Export all products marked 'sync_to_woo' to this WooCommerce store."""
        self.ensure_one()
        products = self.env['product.template'].search([
            ('sync_to_woo', '=', True),
            ('company_id', '=', self.company_id.id),
        ])
        if not products:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('No Products'),
                    'message': _('No products marked for WooCommerce sync. '
                                'Go to Products → WooCommerce tab and enable sync_to_woo.'),
                    'type': 'warning',
                    'sticky': True,
                }
            }

        connector = self.env['woo.connector']._get_connector(self)
        synced, errors = 0, []
        for product in products:
            try:
                connector._sync_product_to_woo(product.id)
                synced += 1
            except Exception as e:
                errors.append(f'{product.name}: {str(e)}')

        if errors:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Partial Sync'),
                    'message': _('Synced %s products. %s errors: %s') % (
                        synced, len(errors), ', '.join(errors[:3])),
                    'type': 'warning',
                    'sticky': True,
                }
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sync Complete'),
                'message': _('%s products synced to WooCommerce.') % synced,
                'type': 'success',
                'sticky': False,
            }
        }

    def action_sync_stock(self):
        """Sync stock quantities for all synced products."""
        self.ensure_one()
        templates = self.env['product.template'].search([
            ('sync_to_woo', '=', True),
            ('woo_product_id', '!=', False),
            ('company_id', '=', self.company_id.id),
        ])
        if not templates:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('No Products'),
                    'message': _('No synced products found. Sync products first.'),
                    'type': 'warning',
                    'sticky': False,
                }
            }

        connector = self.env['woo.connector']._get_connector(self)
        variants = templates.mapped('product_variant_ids')
        connector._sync_stock_batch(variants.ids)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Stock Sync Queued'),
                'message': _('%s products queued for stock update.') % len(variants),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_pull_orders(self):
        """Pull recent orders from WooCommerce (last 24h)."""
        self.ensure_one()
        connector = self.env['woo.connector']._get_connector(self)
        stats = connector._poll_recent_orders(return_stats=True)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Orders Pulled'),
                'message': _(
                    'Created: %(created)s, Updated: %(updated)s, '
                    'Skipped: %(skipped)s, Failed: %(failed)s.') % stats,
                'type': 'warning' if stats['failed'] else 'success',
                'sticky': False,
            }
        }

    def action_pull_all_orders(self):
        """Pull ALL WooCommerce orders (full historical backfill)."""
        self.ensure_one()
        connector = self.env['woo.connector']._get_connector(self)
        stats = connector._import_all_orders()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Full Orders Import'),
                'message': _(
                    'Created: %(created)s, Updated: %(updated)s, '
                    'Failed: %(failed)s.') % stats,
                'type': 'warning' if stats['failed'] else 'success',
                'sticky': False,
            }
        }

    def action_pull_products(self):
        """Pull products from WooCommerce into Odoo (async via queue)."""
        self.ensure_one()
        # Queue the import instead of running synchronously (avoids timeout).
        _job, created = self._queue_unique_batch('product.template', 'import_products')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Import Queued'),
                'message': (_('Product import has been queued. Check Sync Queue for progress.')
                            if created else
                            _('A product import is already pending or running for this store.')),
                'type': 'info',
                'sticky': False,
            }
        }

    def action_pull_customers(self):
        """Pull customers from WooCommerce (async via queue)."""
        self.ensure_one()
        _job, created = self._queue_unique_batch('res.partner', 'import_customers')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Import Queued'),
                'message': (_('Customer import has been queued. Check Sync Queue for progress.')
                            if created else
                            _('A customer import is already pending or running for this store.')),
                'type': 'info',
                'sticky': False,
            }
        }

    def action_pull_customers_from_orders(self):
        """Create/update Odoo partners from WooCommerce order billing data."""
        self.ensure_one()
        connector = self.env['woo.connector']._get_connector(self)
        stats = connector._import_customers_from_orders()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Customers From Orders'),
                'message': _(
                    'Created: %(created)s, Updated: %(updated)s, Failed: %(failed)s.') % stats,
                'type': 'warning' if stats['failed'] else 'success',
                'sticky': False,
            }
        }

    def action_apply_customer_tag(self):
        """Apply the configured customer tag to all partners synced from this store."""
        self.ensure_one()
        if not self.customer_tag_id:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('No Tag Configured'),
                    'message': _('Set a WooCommerce Customer Tag first.'),
                    'type': 'warning',
                    'sticky': True,
                }
            }
        partners = self.env['res.partner'].search([
            ('woo_config_id', '=', self.id),
        ])
        applied = 0
        for partner in partners:
            if self.customer_tag_id.id not in partner.category_id.ids:
                partner.write({'category_id': [(4, self.customer_tag_id.id)]})
                applied += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Customer Tag Applied'),
                'message': _('Tagged %(applied)s of %(total)s synced customers '
                             'with "%(tag)s".') % {
                    'applied': applied,
                    'total': len(partners),
                    'tag': self.customer_tag_id.name,
                },
                'type': 'success',
                'sticky': False,
            }
        }

    # -------------------------------------------------------------------------
    # Reset Sync — DESTRUCTIVE: clears all synced data for this config
    # -------------------------------------------------------------------------
    def action_reset_sync(self):
        """Clear ALL synced data for THIS config. Also catches orphans (NULL config_id)."""
        self.ensure_one()
        counts = {'products': 0, 'orders': 0, 'partners': 0, 'logs': 0, 'queue': 0}

        bindings = self.env['woo.product.binding'].search([('config_id', '=', self.id)])
        products = bindings.mapped('product_tmpl_id')
        counts['products'] = len(products)
        products.write({
            'woo_product_id': False, 'woo_config_id': False,
            'woo_sync_status': 'pending', 'woo_last_sync': False,
        })
        bindings.unlink()

        # Orders: linked to THIS config (draft only)
        orders = self.env['sale.order'].search([
            ('woo_config_id', '=', self.id), ('state', '=', 'draft'),
        ])
        counts['orders'] = len(orders)
        orders.write({'woo_order_id': False})
        orders.action_cancel()

        partner_bindings = self.env['woo.partner.binding'].search([('config_id', '=', self.id)])
        partners = partner_bindings.mapped('partner_id')
        counts['partners'] = len(partners)
        partners.write({'woo_customer_id': False, 'woo_config_id': False})
        partner_bindings.unlink()

        # Logs for THIS config
        logs = self.env['woo.sync.log'].search([('config_id', '=', self.id)])
        counts['logs'] = len(logs)
        logs.unlink()

        # Queue for THIS config
        queue_items = self.env['woo.sync.queue'].search([
            ('config_id', '=', self.id), ('state', 'in', ['pending', 'failed']),
        ])
        counts['queue'] = len(queue_items)
        queue_items.unlink()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sync Reset Complete'),
                'message': _('Cleared: %(products)s products, %(orders)s orders, %(partners)s customers, %(logs)s logs, %(queue)s queue items.') % counts,
                'type': 'success',
                'sticky': True,
            }
        }
