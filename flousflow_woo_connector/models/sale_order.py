import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    """Extend sale.order with WooCommerce order tracking fields.
    
    Key Design Decision (confirmed by Mohamed):
    ALL WooCommerce orders are created as Quotations (state='draft').
    NEVER auto-confirm. Manual review required before confirmation.
    """
    _inherit = 'sale.order'

    woo_order_id = fields.Integer(
        string=('WooCommerce Order ID'),
        copy=False, index=True,
        help=('The order ID in WooCommerce. Primary key for idempotency — '
               'ensures the same order is never imported twice.'))

    woo_order_number = fields.Char(
        string=('WooCommerce Order Number'),
        copy=False,
        help=('The human-readable order number from WooCommerce (e.g., #1234).'))

    woo_payment_status = fields.Char(
        string=('WooCommerce Payment Status'),
        copy=False, tracking=True,
        help=('Payment status as reported by WooCommerce.'))

    woo_payment_method = fields.Char(
        string=('WooCommerce Payment Method'),
        copy=False,
        help=('Payment method used in WooCommerce '
               '(e.g., cod, stripe, paypal, bacs).'))

    woo_payment_method_title = fields.Char(
        string=('Payment Method Title'),
        copy=False,
        help=('Human-readable payment method title from WooCommerce '
               '(e.g., "Cash on Delivery", "Credit Card (Stripe)").'))

    woo_transaction_id = fields.Char(
        string=('WooCommerce Transaction ID'),
        copy=False,
        help=('Payment gateway transaction ID from WooCommerce.'))

    woo_gateway_fee = fields.Monetary(
        string=('Gateway Fee'),
        currency_field='currency_id',
               help=('Payment gateway processing fee, if reported by WooCommerce.'))

    woo_date_paid = fields.Datetime(
        string=('WooCommerce Paid At'), copy=False, readonly=True)

    woo_refund_total = fields.Monetary(
        string=('WooCommerce Refund Total'), currency_field='currency_id',
        copy=False, readonly=True)

    woo_review_status = fields.Selection(
        [('ready', ('Ready for Review')),
         ('payment_pending', ('Payment Pending')),
         ('manual_review', ('Manual Review Required')),
         ('blocked', ('Blocked'))],
        string=('WooCommerce Review Status'),
        compute='_compute_woo_review_status', store=True, copy=False)

    woo_shipping_total = fields.Monetary(
        string=('WooCommerce Shipping Total'),
        currency_field='currency_id',
        help=('Shipping cost as reported by WooCommerce.'))

    woo_discount_total = fields.Monetary(
        string=('WooCommerce Discount Total'),
        currency_field='currency_id',
        help=('Total discount amount as reported by WooCommerce.'))

    woo_raw_payload = fields.Text(
        string=('WooCommerce Raw Payload'),
        copy=False,
        help=('Complete JSON payload from the WooCommerce webhook '
               '(for debugging and audit purposes).'))

    woo_order_url = fields.Char(
        string=('WooCommerce Order URL'),
        compute='_compute_woo_order_url',
        help=('Direct link to view this order in WooCommerce admin.'))

    woo_customer_note = fields.Text(
        string=('WooCommerce Customer Note'),
        copy=False,
        help=('Customer note as entered on the WooCommerce order.'))

    woo_order_note = fields.Text(
        string=('WooCommerce Order Note'),
        copy=False,
        help=('Internal order note from WooCommerce.'))

    woo_config_id = fields.Many2one(
        'woo.config', string=('WooCommerce Config'),
        copy=False, index=True, check_company=True,
        help=('The WooCommerce configuration instance this order came from.'))

    _woo_order_uniq = models.Constraint(
        'unique(woo_order_id, woo_config_id)',
        ('This WooCommerce order has already been imported!'),
    )

    @api.depends('woo_config_id', 'woo_order_id')
    def _compute_woo_order_url(self):
        for order in self:
            if order.woo_config_id and order.woo_order_id:
                base = order.woo_config_id.website_url.rstrip('/')
                order.woo_order_url = (
                    f'{base}/wp-admin/post.php?post={order.woo_order_id}&action=edit')
            else:
                order.woo_order_url = False

    @api.depends('woo_payment_status', 'woo_refund_total')
    def _compute_woo_review_status(self):
        for order in self:
            status = (order.woo_payment_status or '').lower()
            if status in ('cancelled', 'failed', 'refunded') or order.woo_refund_total:
                order.woo_review_status = 'blocked'
            elif status in ('pending', 'on-hold'):
                order.woo_review_status = 'payment_pending'
            elif status in ('processing', 'completed'):
                order.woo_review_status = 'ready'
            else:
                order.woo_review_status = 'manual_review'

    def action_confirm(self):
        blocked = self.filtered(
            lambda order: order.woo_order_id and order.woo_review_status == 'blocked')
        if blocked:
            raise UserError(_(
                'WooCommerce order(s) %(orders)s cannot be confirmed because '
                'they are cancelled, failed, refunded, or contain refunds.') % {
                    'orders': ', '.join(blocked.mapped('display_name')),
                })
        res = super().action_confirm()
        self._push_woo_order_status('processing')
        return res

    def action_cancel(self):
        res = super().action_cancel()
        self._push_woo_order_status('cancelled')
        return res

    def _push_woo_order_status(self, status):
        """Push an Odoo order status change back to WooCommerce when enabled.

        Only fires for imported WooCommerce orders whose config has
        ``auto_update_order_status`` enabled, and only when the remote status
        differs (avoids redundant calls during import of already-cancelled
        orders).
        """
        for order in self:
            if not order.woo_order_id or not order.woo_config_id:
                continue
            if not order.woo_config_id.auto_update_order_status:
                continue
            if (order.woo_payment_status or '').strip().lower() == status:
                continue
            try:
                connector = self.env['woo.connector']._get_connector(
                    order.woo_config_id)
                connector._update_order_status_on_woo(order, status)
            except Exception as exc:
                _logger.exception(
                    'Could not push status "%s" to WooCommerce order #%s: %s',
                    status, order.woo_order_id, exc)
