"""
WooCommerce Webhook Receiver Controller.

Receives webhook events from WooCommerce, verifies HMAC signatures, and
queues sync operations for processing.

Supported webhook topics:
- order.created / order.updated / order.deleted
- customer.created / customer.updated
- product.created / product.updated
"""

import json
import logging
import hmac
import hashlib
import base64

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class WooWebhookController(http.Controller):
    """HTTP controller for receiving WooCommerce webhooks."""

    @http.route('/woo/webhook/<int:config_id>', type='http', auth='public',
                methods=['POST'], csrf=False)
    def woo_webhook_receiver(self, config_id, **kwargs):
        """Receive and process a WooCommerce webhook.
        
        URL format: /woo/webhook/{config_id}
        
        Headers expected:
        - X-WC-Webhook-Topic: e.g., 'order.created'
        - X-WC-Webhook-Signature: HMAC-SHA256 base64-encoded signature
        - X-WC-Webhook-ID: Unique webhook delivery ID
        - Content-Type: application/json
        
        Body: Raw JSON payload from WooCommerce.
        
        Security: HMAC-SHA256 signature verification using the webhook_secret
        stored in woo.config.
        """
        # --- 1. Load the config ---
        config = request.env['woo.config'].sudo().browse(config_id)
        if not config.exists() or not config.active:
            _logger.warning('Webhook received for inactive/nonexistent config #%s', config_id)
            return request.make_json_response(
                {'status': 'error', 'message': 'Config not found or inactive'}, status=404)

        # --- 2. Verify HMAC Signature ---
        if not self._verify_webhook_signature(config):
            _logger.error('Webhook signature verification FAILED for config #%s', config_id)
            return request.make_json_response(
                {'status': 'error', 'message': 'Invalid signature'}, status=401)

        # --- 3. Extract webhook metadata ---
        topic = request.httprequest.headers.get('X-WC-Webhook-Topic', '')
        webhook_id = request.httprequest.headers.get('X-WC-Webhook-ID', '')
        raw_body = request.httprequest.data
        source_ip = request.httprequest.remote_addr

        _logger.info(
            'Webhook received: config=%s, topic=%s, webhook_id=%s, ip=%s',
            config_id, topic, webhook_id, source_ip)

        # --- 4. Parse payload ---
        try:
            payload = json.loads(raw_body) if raw_body else kwargs
        except json.JSONDecodeError as e:
            _logger.error('Webhook JSON decode error: %s', e)
            return request.make_json_response(
                {'status': 'error', 'message': f'Invalid JSON: {str(e)}'}, status=400)

        # --- 5. Dispatch based on topic ---
        try:
            result = self._dispatch_webhook(config, topic, payload, webhook_id)
            return request.make_json_response({'status': 'success', **result}, status=202)
        except Exception as e:
            _logger.exception('Webhook processing error: %s', e)
            return request.make_json_response(
                {'status': 'error', 'message': str(e)}, status=500)

    # -------------------------------------------------------------------
    # HMAC Signature Verification
    # -------------------------------------------------------------------

    def _verify_webhook_signature(self, config):
        """Verify the WooCommerce webhook HMAC-SHA256 signature.
        
        WooCommerce signs the payload with the webhook secret using HMAC-SHA256
        and sends the base64-encoded signature in the X-WC-Webhook-Signature header.
        
        Args:
            config: woo.config record
        
        Returns:
            bool: True only when a configured secret matches the signature.
        """
        secret = config.webhook_secret
        if not secret:
            _logger.error('No webhook secret configured for config "%s".', config.name)
            return False

        expected_signature = request.httprequest.headers.get('X-WC-Webhook-Signature', '')
        if not expected_signature:
            _logger.error('Missing X-WC-Webhook-Signature header')
            return False

        raw_body = request.httprequest.data
        if not raw_body:
            _logger.error('Empty webhook body')
            return False

        # Compute HMAC-SHA256
        computed = base64.b64encode(
            hmac.new(
                secret.encode('utf-8'),
                raw_body,
                hashlib.sha256
            ).digest()
        ).decode('utf-8')

        is_valid = hmac.compare_digest(computed, expected_signature)
        if not is_valid:
            _logger.error(
                'HMAC signature mismatch. Computed=%s..., Expected=%s...',
                computed[:20], expected_signature[:20])
        
        return is_valid

    # -------------------------------------------------------------------
    # Webhook Dispatcher
    # -------------------------------------------------------------------

    def _dispatch_webhook(self, config, topic, payload, webhook_id):
        """Dispatch a webhook to the appropriate handler based on topic.
        
        Args:
            config: woo.config record
            topic: Webhook topic string (e.g., 'order.created')
            payload: Parsed JSON data
            webhook_id: Unique webhook delivery ID
        
        Returns:
            dict with result info
        """
        # Log the incoming webhook
        request.env['woo.sync.log'].sudo().log_sync(
            config, 'webhook', 'create', 'woo_to_odoo',
            f'Webhook received: {topic} (ID: {webhook_id})',
            raw_request=json.dumps(payload, indent=2))

        # --- Order Webhooks ---
        if topic.startswith('order.'):
            return self._handle_order_webhook(config, topic, payload, webhook_id)

        # --- Customer Webhooks ---
        elif topic.startswith('customer.'):
            return self._handle_customer_webhook(config, topic, payload, webhook_id)

        # --- Product Webhooks ---
        elif topic.startswith('product.'):
            return self._handle_product_webhook(config, topic, payload, webhook_id)

        else:
            _logger.info('Unhandled webhook topic: %s', topic)
            return {'message': f'Unhandled topic: {topic}', 'action': 'ignored'}

    def _enqueue_webhook(self, config, operation, model_name, payload, webhook_id, priority=10):
        queue = request.env['woo.sync.queue'].sudo()
        existing = webhook_id and queue.search([
            ('config_id', '=', config.id),
            ('webhook_delivery_id', '=', webhook_id),
        ], limit=1)
        if existing:
            return existing, False
        item = queue.create({
            'config_id': config.id,
            'model_name': model_name,
            'woo_id': str(payload.get('id') or ''),
            'operation': operation,
            'direction': 'woo_to_odoo',
            'payload': json.dumps(payload),
            'webhook_delivery_id': webhook_id or False,
            'priority': priority,
        })
        return item, True

    def _handle_order_webhook(self, config, topic, payload, webhook_id):
        """Handle order-related webhooks."""
        if topic in ('order.created', 'order.updated'):
            item, created = self._enqueue_webhook(
                config, 'order_import', 'sale.order', payload, webhook_id)
            return {
                'message': 'Order webhook queued' if created else 'Duplicate webhook ignored',
                'queue_id': item.id,
            }
        elif topic == 'order.deleted':
            woo_id = payload.get('id')
            if woo_id:
                order = request.env['sale.order'].sudo().search(
                    [('woo_order_id', '=', woo_id),
                     ('woo_config_id', '=', config.id)], limit=1)
                if order and order.state == 'draft':
                    order.action_cancel()
                    return {'message': f'Order {order.name} cancelled', 'order_id': order.id}
            return {'message': 'Order not found or already processed', 'action': 'ignored'}
        else:
            return {'message': f'Unhandled order topic: {topic}', 'action': 'ignored'}

    def _handle_customer_webhook(self, config, topic, payload, webhook_id):
        """Handle customer-related webhooks."""
        if topic in ('customer.created', 'customer.updated'):
            item, created = self._enqueue_webhook(
                config, 'customer_import', 'res.partner', payload, webhook_id)
            return {
                'message': 'Customer webhook queued' if created else 'Duplicate webhook ignored',
                'queue_id': item.id,
            }
        return {'message': f'Unhandled customer topic: {topic}', 'action': 'ignored'}

    def _handle_product_webhook(self, config, topic, payload, webhook_id):
        """Handle product-related webhooks."""
        # For now, product webhooks from Woo → Odoo are logged but not auto-processed
        # (sync direction is primarily Odoo → Woo for products)
        if config.sync_direction_products not in ('woo_to_odoo', 'both'):
            return {'message': 'Product webhook ignored by sync direction', 'action': 'ignored'}
        item, created = self._enqueue_webhook(
            config, 'product_import', 'product.template', payload, webhook_id, priority=15)
        return {
            'message': 'Product webhook queued' if created else 'Duplicate webhook ignored',
            'queue_id': item.id,
        }
