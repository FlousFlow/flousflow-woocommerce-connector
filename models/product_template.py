import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    """Extend product.template with WooCommerce sync fields."""
    _inherit = 'product.template'

    sync_to_woo = fields.Boolean(
        string=('Sync to WooCommerce'),
        default=False,
        help=('Enable to sync this product and its variants to WooCommerce.'))

    woo_config_id = fields.Many2one(
        'woo.config', string=('WooCommerce Config'),
        copy=False, index=True,
        help=('The WooCommerce configuration that imported/owns this product.'))

    woo_product_id = fields.Integer(
        string=('WooCommerce Product ID'),
        copy=False, readonly=True,
        help=('The product ID in WooCommerce after first successful sync.'))

    woo_last_sync = fields.Datetime(
        string=('Last WooCommerce Sync'),
        readonly=True, copy=False,
        help=('Timestamp of the last successful sync to/from WooCommerce.'))

    woo_sync_status = fields.Selection(
        [('pending', ('Pending Sync')),
         ('synced', ('Synced')),
         ('error', ('Sync Error')),
         ('unlinked', ('Unlinked from WooCommerce'))],
        string=('WooCommerce Sync Status'),
        default='pending', tracking=True, copy=False,
        help=('Current sync status with WooCommerce.\n'
               '- Pending: Not yet synced or needs re-sync.\n'
               '- Synced: Successfully synced.\n'
               '- Error: Last sync failed.\n'
               '- Unlinked: Product was removed from WooCommerce.'))

    woo_variant_mapping = fields.Text(
        string=('WooCommerce Variant Mapping'),
        copy=False,
        help=('JSON mapping of Odoo variant IDs to WooCommerce variant IDs. '
               'Format: {"odoo_variant_id": woo_variant_id, ...}'))

    woo_category_binding_ids = fields.Many2many(
        'woo.category.binding',
        'woo_category_product_template_rel',
        'product_tmpl_id', 'category_binding_id',
        string=('WooCommerce Categories'), copy=False,
        help=('Store-scoped WooCommerce classifications. They never replace the Odoo product category.'))
    woo_image_ids = fields.One2many(
        'woo.product.image', 'product_tmpl_id',
        string=('WooCommerce Gallery'), copy=False)
    woo_length = fields.Float(string=('WooCommerce Length'), copy=False)
    woo_width = fields.Float(string=('WooCommerce Width'), copy=False)
    woo_height = fields.Float(string=('WooCommerce Height'), copy=False)

    def action_sync_to_woo(self):
        """Manually sync this product to all active WooCommerce configs."""
        self.ensure_one()
        if not self.sync_to_woo:
            raise UserError(_('Please enable "Sync to WooCommerce" first.'))

        configs = self.env['woo.config'].search([('active', '=', True)])
        if not configs:
            raise UserError(_('No active WooCommerce configuration found. '
                            'Please configure one first.'))

        synced = 0
        errors = []
        for config in configs:
            try:
                connector = self.env['woo.connector']._get_connector(config)
                connector._sync_product_to_woo(self.id)
                synced += 1
            except Exception as e:
                errors.append(f'{config.name}: {str(e)}')

        if errors:
            raise UserError(
                _('Synced to %s config(s). Errors: %s') % (synced, ', '.join(errors)))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sync Successful'),
                'message': _('Product synced to %s WooCommerce store(s).') % synced,
                'type': 'success',
                'sticky': False,
            }
        }

    def write(self, vals):
        """Archive handling: when product is archived, log and stop syncing."""
        res = super().write(vals)
        if 'active' in vals and not vals['active']:
            for record in self:
                if record.sync_to_woo and record.woo_sync_status == 'synced':
                    # Log that the product was archived
                    self.env['woo.sync.log'].log_sync(
                        self.env['woo.config'].search([], limit=1),
                        'product.template', 'archived', 'odoo_to_woo',
                        _('Product "%s" archived in Odoo — excluded from future sync. '
                          'WooCommerce product #%s NOT deleted (manual action required).') % (
                            record.name, record.woo_product_id),
                        record_id=record.id,
                        woo_id=str(record.woo_product_id),
                        state='warning')
        return res
