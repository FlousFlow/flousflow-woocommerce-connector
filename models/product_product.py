import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class ProductProduct(models.Model):
    """Extend product.product with WooCommerce-related helpers.
    
    Note: The core sync_to_woo flag and woo_product_id are on product.template
    because WooCommerce products are template-level. Variant-level data
    (like individual variant IDs) is stored in woo_variant_mapping on template.
    """
    _inherit = 'product.product'

    def _get_woo_sync_template(self):
        """Helper to get the sync-to-woo status from the template."""
        self.ensure_one()
        return self.product_tmpl_id.sync_to_woo
