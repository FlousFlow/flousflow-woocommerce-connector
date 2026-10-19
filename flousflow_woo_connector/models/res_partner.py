import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    """Extend res.partner with WooCommerce customer tracking."""
    _inherit = 'res.partner'

    woo_customer_id = fields.Integer(
        string=('WooCommerce Customer ID'),
        copy=False, index=True,
        help=('The customer ID in WooCommerce. Used to prevent duplicate '
               'customer creation on subsequent syncs.'))

    woo_config_id = fields.Many2one(
        'woo.config', string=('WooCommerce Config'),
        copy=False, index=True,
        help=('The WooCommerce configuration that imported this customer.'))

    woo_last_sync = fields.Datetime(
        string=('Last WooCommerce Sync'),
        readonly=True, copy=False,
        help=('Timestamp of the last successful customer sync from WooCommerce.'))

    woo_raw_data = fields.Text(
        string=('WooCommerce Raw Data'),
        copy=False,
        help=('Raw JSON data from the last WooCommerce customer sync '
               '(for debugging/audit purposes).'))
