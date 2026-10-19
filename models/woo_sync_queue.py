import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WooSyncQueue(models.Model):
    """Fallback queue for async WooCommerce sync operations.
    
    Odoo 19 core does NOT include queue_job (OCA module). This provides a
    lightweight alternative: a model-based queue processed by ir.cron.
    
    Supports:
    - Retry with exponential backoff
    - Batch processing
    - Priority ordering
    - Rate limiting awareness
    """
    _name = 'woo.sync.queue'
    _description = 'WooCommerce Sync Queue'
    _order = 'priority asc, create_date asc, id asc'

    # --- Identity ---
    config_id = fields.Many2one(
        'woo.config', string=('WooCommerce Config'),
        required=True, ondelete='cascade', index=True)

    # --- What to Process ---
    model_name = fields.Char(
        string=('Model'), required=True, index=True)
    record_id = fields.Integer(
        string=('Record ID'), index=True)
    woo_id = fields.Char(
        string=('WooCommerce ID'), index=True)
    webhook_delivery_id = fields.Char(
        string=('Webhook Delivery ID'), copy=False, index=True)
    operation = fields.Char(
        string=('Operation'), required=True)
    direction = fields.Selection(
        [('woo_to_odoo', ('WooCommerce → Odoo')),
         ('odoo_to_woo', ('Odoo → WooCommerce'))],
        string=('Direction'), required=True)

    # --- Payload ---
    payload = fields.Text(
        string=('Payload'),
        help=('JSON payload for the sync operation.'))

    # --- State ---
    state = fields.Selection(
        [('pending', ('Pending')),
         ('processing', ('Processing')),
         ('done', ('Done')),
         ('failed', ('Failed')),
         ('cancelled', ('Cancelled'))],
        string=('State'), required=True, default='pending', index=True)

    # --- Retry ---
    retry_count = fields.Integer(
        string=('Retry Count'), default=0)
    max_retries = fields.Integer(
        string=('Max Retries'), default=5)
    next_retry_at = fields.Datetime(
        string=('Next Retry At'),
        help=('Exponential backoff: next retry scheduled at this time.'))
    last_error = fields.Text(
        string=('Last Error'))

    # --- Priority ---
    priority = fields.Integer(
        string=('Priority'), default=10,
        help=('Lower number = higher priority. Stock updates: 5, Orders: 10, Products: 15.'))

    # --- Timing ---
    create_date = fields.Datetime(
        string=('Created'), readonly=True, index=True)
    process_date = fields.Datetime(
        string=('Processed At'), readonly=True)

    # --- Constraints ---
    _webhook_delivery_uniq = models.Constraint(
        'unique(config_id, webhook_delivery_id)',
        ('This WooCommerce webhook delivery was already queued.'),
    )

    def unlink(self):
        for record in self:
            if record.state == 'processing':
                raise UserError(_('Cannot delete a queue item that is currently being processed!'))
        return super().unlink()

    @api.model
    def _process_pending_batch(self, batch_size=20):
        """Process a batch of pending queue items.
        
        Called by ir.cron every few minutes.
        
        Args:
            batch_size: Max number of items to process in one cron run.
        
        Returns:
            Number of items processed.
        """
        # Find items ready to process
        now = fields.Datetime.now()
        self.env.cr.execute("""
            SELECT id
              FROM woo_sync_queue
             WHERE state = 'pending'
               AND retry_count < max_retries
               AND (next_retry_at IS NULL OR next_retry_at <= %s)
             ORDER BY priority, create_date, id
             FOR UPDATE SKIP LOCKED
             LIMIT %s
        """, [now, batch_size])
        items = self.browse([row[0] for row in self.env.cr.fetchall()])
        
        if not items:
            return 0
        
        processed = 0
        for item in items:
            try:
                item._process_one()
                processed += 1
            except Exception as e:
                _logger.exception('Error processing queue item %s: %s', item.id, e)
                # Will be retried via exponential backoff
        
        return processed

    def _process_one(self):
        """Process a single queue item."""
        self.ensure_one()
        self.state = 'processing'
        
        try:
            # A database error must roll back before retry metadata is written.
            with self.env.cr.savepoint():
                self._dispatch_payload()
            
            self.write({
                'state': 'done',
                'process_date': fields.Datetime.now(),
                'last_error': False,
                'next_retry_at': False,
            })
            
        except Exception as e:
            error_msg = str(e)
            self.retry_count += 1
            
            if self.retry_count >= self.max_retries:
                self.write({
                    'state': 'failed',
                    'last_error': error_msg,
                })
                _logger.error('Queue item %s permanently failed after %s retries: %s',
                             self.id, self.retry_count, error_msg)
            else:
                # Exponential backoff: 2^retry_count seconds, max 1 hour
                import datetime
                delay_seconds = min(2 ** self.retry_count * 10, 3600)
                next_retry = fields.Datetime.now() + datetime.timedelta(seconds=delay_seconds)
                
                self.write({
                    'state': 'pending',
                    'last_error': error_msg,
                    'next_retry_at': next_retry,
                })
                _logger.info('Queue item %s retry #%s scheduled at %s (delay: %ss)',
                            self.id, self.retry_count, next_retry, delay_seconds)

    def _dispatch_payload(self):
        """Dispatch one job inside a transaction savepoint."""
        self.ensure_one()
        connector = self.env['woo.connector']._get_connector(self.config_id)

        import json
        payload = json.loads(self.payload) if self.payload else {}

        if self.operation == 'stock_update':
            connector._sync_stock_batch(payload.get('product_ids', []))
        elif self.operation == 'product_export':
            connector._sync_product_to_woo(payload.get('product_id'))
        elif self.operation == 'order_import':
            connector._import_order_from_webhook(payload)
        elif self.operation == 'customer_import':
            connector._match_or_create_partner(payload)
        elif self.operation == 'product_import':
            if self.config_id.sync_direction_products not in ('woo_to_odoo', 'both'):
                raise UserError(_('Product import is disabled for this store.'))
            connector._import_product_from_woo(payload)
        elif self.operation == 'import_products':
            imported, failed = 0, 0
            for wp in connector._get_all_pages('products', per_page=30):
                try:
                    with self.env.cr.savepoint():
                        result = connector._import_product_from_woo(wp)
                    if result:
                        imported += 1
                except Exception as exc:
                    failed += 1
                    self.env['woo.sync.log'].log_sync(
                        self.config_id, 'product.template', 'error', 'woo_to_odoo',
                        _('Product import failed for WooCommerce #%(woo)s: %(error)s')
                        % {'woo': wp.get('id'), 'error': str(exc)},
                        woo_id=str(wp.get('id') or ''), state='error')
            self.env['woo.sync.log'].log_sync(
                self.config_id, 'product.template', 'create', 'woo_to_odoo',
                _('Batch import completed: %(imported)s imported, %(failed)s failed.')
                % {'imported': imported, 'failed': failed},
                state='warning' if failed else 'success')
        elif self.operation == 'import_customers':
            for customer in connector._get_all_pages('customers', per_page=50):
                connector._match_or_create_partner(customer)
        elif self.operation == 'retry_from_log':
            raise UserError(_('Legacy retry jobs cannot be dispatched safely. Create a new sync job.'))
        else:
            raise UserError(_('Unknown queue operation: %s') % self.operation)

    def action_cancel(self):
        """Cancel pending queue items."""
        for record in self:
            if record.state in ('pending', 'failed'):
                record.state = 'cancelled'

    def action_retry_now(self):
        """Force immediate retry."""
        for record in self:
            if record.state == 'failed':
                record.write({
                    'state': 'pending',
                    'next_retry_at': False,
                    'last_error': False,
                })
