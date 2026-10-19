import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WooSyncLog(models.Model):
    """Audit trail for every synchronization operation.
    
    Records every create/update/skip/error event for full traceability.
    """
    _name = 'woo.sync.log'
    _description = 'WooCommerce Sync Log'
    _order = 'timestamp desc, id desc'
    _rec_name = 'message'

    # --- Identity ---
    model_name = fields.Char(
        string=('Model'), required=True, index=True,
        help=('Odoo model involved (e.g., sale.order, product.product).'))
    record_id = fields.Integer(
        string=('Record ID'), index=True,
        help=('Odoo record ID, if applicable.'))
    woo_id = fields.Char(
        string=('WooCommerce ID'), index=True,
        help=('WooCommerce entity ID (order ID, product ID, customer ID).'))

    # --- Operation ---
    operation = fields.Selection(
        [('create', ('Create')),
         ('update', ('Update')),
         ('skip', ('Skip')),
         ('error', ('Error')),
         ('cancel', ('Cancel')),
         ('line_update', ('Line Update')),
         ('manual_review_required', ('Manual Review Required')),
         ('connection_test', ('Connection Test')),
         ('stock_update', ('Stock Update')),
         ('unlinked', ('Unlinked')),
         ('archived', ('Archived'))],
        string=('Operation'), required=True, default='create')
    direction = fields.Selection(
        [('woo_to_odoo', ('WooCommerce → Odoo')),
         ('odoo_to_woo', ('Odoo → WooCommerce'))],
        string=('Direction'), required=True)

    # --- Status ---
    state = fields.Selection(
        [('success', ('Success')),
         ('warning', ('Warning')),
         ('error', ('Error')),
         ('skipped', ('Skipped'))],
        string=('Status'), required=True, default='success')

    # --- Details ---
    message = fields.Text(
        string=('Message'), required=True,
        help=('Human-readable description of what happened.'))
    raw_request = fields.Text(
        string=('Raw Request'),
        help=('The full request payload sent (for debugging).'))
    raw_response = fields.Text(
        string=('Raw Response'),
        help=('The full response received (for debugging).'))

    # --- Timing ---
    timestamp = fields.Datetime(
        string=('Timestamp'), required=True,
        default=fields.Datetime.now, index=True)
    duration = fields.Float(
        string=('Duration (seconds)'),
        help=('How long the sync operation took.'))

    # --- Link to Config ---
    config_id = fields.Many2one(
        'woo.config', string=('WooCommerce Config'),
        required=True, ondelete='cascade', index=True)

    # --- Link to User ---
    user_id = fields.Many2one(
        'res.users', string=('User'),
        default=lambda self: self.env.user)

    # --- Retry ---
    retry_count = fields.Integer(
        string=('Retry Count'), default=0,
        help=('Number of times this operation has been retried.'))

    @api.model
    def log_sync(self, config, model_name, operation, direction, message,
                 record_id=False, woo_id=False, state='success',
                 raw_request=False, raw_response=False, duration=0.0):
        """Convenience method to create a log entry.
        
        Args:
            config: woo.config record or ID
            model_name: string, e.g., 'sale.order'
            operation: 'create'|'update'|'skip'|'error'|'cancel'|'line_update'|'stock_update'|...
            direction: 'woo_to_odoo'|'odoo_to_woo'
            message: Human-readable description
            record_id: Odoo record ID (optional)
            woo_id: WooCommerce entity ID (optional)
            state: 'success'|'warning'|'error'|'skipped'
            raw_request: Full request payload (optional)
            raw_response: Full response payload (optional)
            duration: Time taken in seconds
        
        Returns:
            woo.sync.log record
        """
        config_id = config.id if hasattr(config, 'id') else config
        return self.create({
            'config_id': config_id,
            'model_name': model_name,
            'record_id': record_id,
            'woo_id': woo_id,
            'operation': operation,
            'direction': direction,
            'state': state,
            'message': message,
            'raw_request': raw_request,
            'raw_response': raw_response,
            'duration': duration,
        })

    def action_retry(self):
        """Retry failed sync operations."""
        for record in self:
            if record.state != 'error':
                continue
            record.retry_count += 1
            operation_map = {
                ('sale.order', 'woo_to_odoo'): 'order_import',
                ('res.partner', 'woo_to_odoo'): 'customer_import',
                ('product.template', 'woo_to_odoo'): 'product_import',
                ('product.template', 'odoo_to_woo'): 'product_export',
                ('product.product', 'odoo_to_woo'): 'stock_update',
            }
            operation = operation_map.get((record.model_name, record.direction))
            if not operation or not record.raw_request:
                raise UserError(_('This log does not contain enough data for a safe retry.'))
            self.env['woo.sync.queue'].create({
                'config_id': record.config_id.id,
                'model_name': record.model_name,
                'record_id': record.record_id,
                'woo_id': record.woo_id,
                'operation': operation,
                'direction': record.direction,
                'payload': record.raw_request,
                'retry_count': record.retry_count,
            })
            record.message = _('Retry #%s queued.') % record.retry_count + '\n' + (record.message or '')
