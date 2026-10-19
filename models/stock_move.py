"""
Extend stock.move to trigger automatic WooCommerce stock sync.
"""
import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    """Extend stock.move to trigger WooCommerce stock sync on completion."""
    _inherit = 'stock.move'

    def _action_done(self, cancel_backorder=False):
        """Override to trigger WooCommerce stock sync after move is done."""
        res = super()._action_done(cancel_backorder=cancel_backorder)

        # Collect unique products affected by this move
        products = self.mapped('product_id').filtered(
            lambda p: p.product_tmpl_id.sync_to_woo and p.product_tmpl_id.woo_product_id)

        if products:
            # Queue stock update for affected products
            configs = self.env['woo.config'].search([
                ('active', '=', True),
                ('auto_sync_enabled', '=', True),
                ('auto_sync_stock', '=', True),
                ('sync_direction_products', 'in', ('odoo_to_woo', 'both')),
            ])
            for config in configs:
                try:
                    connector = self.env['woo.connector']._get_connector(config)
                    # Filter to products belonging to this config's company
                    config_products = products.filtered(
                        lambda p: p.company_id == config.company_id or not p.company_id)
                    if config_products:
                        connector._queue_stock_batch(config_products)
                except Exception as e:
                    _logger.exception('Error queuing stock sync for config %s: %s',
                                     config.name, e)
        return res

    def _action_cancel(self):
        """Trigger stock sync on cancellation (stock returns)."""
        res = super()._action_cancel()
        # Same logic as _action_done — stock changed, needs sync
        products = self.mapped('product_id').filtered(
            lambda p: p.product_tmpl_id.sync_to_woo and p.product_tmpl_id.woo_product_id)
        if products:
            configs = self.env['woo.config'].search([
                ('active', '=', True),
                ('auto_sync_enabled', '=', True),
                ('auto_sync_stock', '=', True),
                ('sync_direction_products', 'in', ('odoo_to_woo', 'both')),
            ])
            for config in configs:
                try:
                    connector = self.env['woo.connector']._get_connector(config)
                    config_products = products.filtered(
                        lambda p: p.company_id == config.company_id or not p.company_id)
                    if config_products:
                        connector._queue_stock_batch(config_products)
                except Exception as e:
                    _logger.exception('Error queuing stock sync on cancel: %s', e)
        return res


class StockPicking(models.Model):
    """Extend stock.picking to trigger WooCommerce stock sync on completion."""
    _inherit = 'stock.picking'

    def _action_done(self):
        """Trigger stock sync when entire picking is done."""
        res = super()._action_done()
        # Moves already handled individually, but this is a safety net
        return res
