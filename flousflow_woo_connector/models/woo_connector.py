"""
FlousFlow WooCommerce Connector — Core Sync Engine.

This is the main connector class that handles all WooCommerce REST API
communication and synchronization logic.

Key Design Principles:
1. ALL orders are created as Quotations (draft) — NEVER auto-confirmed.
2. Product matching: product.product.barcode == WooCommerce SKU.
3. Customer matching: email first, then woo_customer_id.
4. Idempotency: woo_order_id is the unique key for orders.
5. All operations logged in woo.sync.log for full audit trail.
6. HMAC signature verification for webhooks.
"""

import datetime
import json
import logging
import time
import hmac
import hashlib
import base64

import requests
from requests.auth import HTTPBasicAuth

from odoo import api, fields, models, Command, _
from odoo.tools import BinaryBytes
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# WooCommerce REST API endpoints (relative to base URL)
WOO_ENDPOINTS = {
    'products': 'products',
    'products_batch': 'products/batch',
    'orders': 'orders',
    'customers': 'customers',
    'system_status': 'system_status',
}

# Rate limiting: max requests per second per config
WOO_RATE_LIMIT_PER_SECOND = 5

# Default request timeout (seconds)
WOO_REQUEST_TIMEOUT = 30

# Exponential backoff base delay (seconds) for retries
WOO_RETRY_BASE_DELAY = 10
WOO_MAX_RETRY_DELAY = 3600  # 1 hour

# Known payment method mappings WooCommerce → Odoo journal (configurable)
WOO_PAYMENT_METHOD_MAP = {
    'cod': 'Cash on Delivery',
    'stripe': 'Credit Card (Stripe)',
    'paypal': 'PayPal',
    'bacs': 'Bank Transfer',
    'cheque': 'Cheque',
    'bog': 'Bank of Georgia',
}


class WooConnector(models.AbstractModel):
    """Core WooCommerce REST API connector and sync engine.
    
    This is an AbstractModel (no DB table) that provides all the methods
    for communicating with WooCommerce and performing sync operations.
    It is instantiated via _get_connector(config) to get a bound connector
    for a specific woo.config record.
    """
    _name = 'woo.connector'
    _description = 'WooCommerce Connector Engine'

    # -----------------------------------------------------------------------
    # Factory: Get a connector instance bound to a specific config
    # -----------------------------------------------------------------------

    @api.model
    def _get_connector(self, config):
        """Return a Connector instance bound to the given woo.config.
        
        Args:
            config: woo.config record
        
        Returns:
            _WooApiConnector instance
        """
        config.ensure_one()
        return _WooApiConnector(self.env, config)

    # -----------------------------------------------------------------------
    # Cron: Process pending queue items
    # -----------------------------------------------------------------------

    @api.model
    def _cron_process_queue(self):
        """Scheduled action: process pending sync queue items."""
        queue = self.env['woo.sync.queue']
        processed = queue._process_pending_batch(batch_size=20)
        if processed:
            _logger.info('WooCommerce sync cron: processed %s queue items', processed)
        return processed

    @api.model
    def _cron_auto_sync(self):
        """Scheduled action: run auto-sync for all enabled configs.
        
        Respects auto_sync_enabled flag and individual sync type toggles.
        Runs at the interval specified in each config's auto_sync_interval.
        """
        configs = self.env['woo.config'].search([
            ('active', '=', True),
            ('auto_sync_enabled', '=', True),
        ])
        total_orders = 0
        total_products = 0
        total_customers = 0
        
        for config in configs:
            try:
                sync_started_at = fields.Datetime.now()
                batch_size = min(max(config.auto_sync_batch_size or 100, 1), 100)
                interval = int(config.auto_sync_interval or '15')
                if config.last_auto_sync and fields.Datetime.now() < (
                        config.last_auto_sync + datetime.timedelta(minutes=interval)):
                    continue
                connector = self._get_connector(config)
                
                # Auto-pull orders
                if config.auto_sync_orders:
                    imported = connector._poll_recent_orders()
                    total_orders += (imported or 0)
                
                # Auto-pull customers
                if config.auto_sync_customers:
                    customer_params = {}
                    if config.last_customer_sync:
                        customer_params['modified_after'] = (
                            config.last_customer_sync.isoformat()
                            if hasattr(config.last_customer_sync, 'isoformat')
                            else str(config.last_customer_sync))
                    for customer in connector._get_all_pages(
                            'customers', params=customer_params,
                            per_page=batch_size):
                        connector._match_or_create_partner(customer)
                        total_customers += 1
                    # Also capture shoppers that used guest checkout (no
                    # customer account) by scanning order billing data.
                    if config.sync_customers_from_orders:
                        order_stats = connector._import_customers_from_orders()
                        total_customers += (
                            order_stats['created'] + order_stats['updated'])
                    config.last_customer_sync = sync_started_at
                
                # Auto-sync products (export Odoo → Woo)
                if (config.auto_sync_products
                        and config.sync_direction_products in ('odoo_to_woo', 'both')):
                    export_completed = True
                    product_domain = [
                        ('sync_to_woo', '=', True),
                        ('company_id', '=', config.company_id.id),
                    ]
                    if config.last_product_export_sync:
                        product_domain.append(('write_date', '>=', config.last_product_export_sync))
                    products = self.env['product.template'].search(product_domain)
                    for product in products:
                        try:
                            connector._sync_product_to_woo(product.id)
                            total_products += 1
                        except Exception as exc:
                            export_completed = False
                            self.env['woo.sync.log'].log_sync(
                                config, 'product.template', 'error', 'odoo_to_woo',
                                _('Automatic product export failed for %(product)s: %(error)s')
                                % {'product': product.display_name, 'error': str(exc)},
                                record_id=product.id, state='error')
                    if export_completed:
                        config.last_product_export_sync = sync_started_at
                
                # Auto-import products (Woo → Odoo)
                if (config.auto_sync_products
                        and config.sync_direction_products in ('woo_to_odoo', 'both')):
                    import_completed = True
                    product_params = {}
                    if config.last_product_import_sync:
                        product_params['modified_after'] = (
                            config.last_product_import_sync.isoformat()
                            if hasattr(config.last_product_import_sync, 'isoformat')
                            else str(config.last_product_import_sync))
                    for woo_product in connector._get_all_pages(
                            'products', params=product_params,
                            per_page=batch_size):
                        try:
                            result = connector._import_product_from_woo(woo_product)
                            if result:
                                total_products += 1
                        except Exception as exc:
                            import_completed = False
                            self.env['woo.sync.log'].log_sync(
                                config, 'product.template', 'error', 'woo_to_odoo',
                                _('Automatic product import failed for WooCommerce #%(woo)s: %(error)s')
                                % {'woo': woo_product.get('id'), 'error': str(exc)},
                                woo_id=str(woo_product.get('id') or ''), state='error')
                    if import_completed:
                        config.last_product_import_sync = sync_started_at
                
                config.last_auto_sync = fields.Datetime.now()
            except Exception as e:
                _logger.exception('Error in auto-sync for config %s: %s', config.name, e)
        
        if total_orders or total_products or total_customers:
            _logger.info(
                'Auto-sync completed: %s orders, %s products, %s customers',
                total_orders, total_products, total_customers)
        
        return total_orders + total_products + total_customers

    @api.model
    def _cron_poll_orders(self):
        """Scheduled action: fallback polling for new/updated orders.
        
        Polls WooCommerce for orders changed since last successful poll.
        This is a safety net in case webhooks fail.
        """
        configs = self.env['woo.config'].search([
            ('active', '=', True),
            ('auto_sync_enabled', '=', True),
            ('auto_sync_orders', '=', True),
        ])
        total_imported = 0
        for config in configs:
            try:
                connector = self._get_connector(config)
                imported = connector._poll_recent_orders()
                total_imported += imported
            except Exception as e:
                _logger.exception('Error polling orders for config %s: %s', config.name, e)
        if total_imported:
            _logger.info('WooCommerce poll cron: imported %s orders', total_imported)
        return total_imported

    @api.model
    def _cron_cleanup_logs(self):
        """Scheduled action: archive/delete old sync logs."""
        configs = self.env['woo.config'].search([])
        for config in configs:
            days = config.log_retention_days or 90
            cutoff = fields.Datetime.now() - datetime.timedelta(days=days)
            total_removed = 0
            # Delete in bounded batches so a large audit table does not load
            # millions of ORM records into memory or hold one huge lock.
            while True:
                self.env.cr.execute("""
                    DELETE FROM woo_sync_log
                     WHERE id IN (
                         SELECT id
                           FROM woo_sync_log
                          WHERE config_id = %s
                            AND timestamp < %s
                          ORDER BY timestamp, id
                          LIMIT 5000
                     )
                    RETURNING id
                """, [config.id, cutoff])
                removed = self.env.cr.rowcount
                total_removed += removed
                if removed < 5000:
                    break
                self.env.cr.commit()
            if total_removed:
                _logger.info(
                    'WooCommerce cleanup cron: removed %s old log entries for config %s',
                    total_removed, config.name)


# ===========================================================================
# Internal connector class (not an Odoo model — plain Python)
# ===========================================================================

class _WooApiConnector:
    """Low-level WooCommerce REST API client with sync logic.
    
    This is instantiated per woo.config record and holds the HTTP session,
    auth credentials, rate limiting state, etc.
    
    Usage:
        connector = self.env['woo.connector']._get_connector(config)
        connector.sync_product_to_woo(product)
    """

    def __init__(self, env, config):
        self.env = env
        self.config = config
        self._session = None
        self._last_request_time = 0
        self._request_count = 0

    @staticmethod
    def _woo_datetime_to_odoo(value):
        """Convert WooCommerce ISO-8601 timestamps to Odoo UTC datetimes."""
        if not value:
            return fields.Datetime.now()
        if isinstance(value, datetime.datetime):
            parsed = value
        else:
            try:
                parsed = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            except ValueError as exc:
                raise ValidationError(
                    _('Invalid WooCommerce order date: %s') % value
                ) from exc
        if parsed.tzinfo:
            parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return fields.Datetime.to_string(parsed)

    # -------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------

    @property
    def base_url(self):
        return self.config.api_base_url

    @property
    def auth(self):
        """HTTP Basic Auth using Consumer Key + Consumer Secret."""
        return HTTPBasicAuth(self.config.consumer_key, self.config.consumer_secret)

    @property
    def session(self):
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            })
        return self._session

    # -------------------------------------------------------------------
    # Rate Limiting
    # -------------------------------------------------------------------

    def _rate_limit(self):
        """Enforce rate limiting between requests."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < (1.0 / WOO_RATE_LIMIT_PER_SECOND):
            time.sleep((1.0 / WOO_RATE_LIMIT_PER_SECOND) - elapsed)
        self._last_request_time = time.time()

    # -------------------------------------------------------------------
    # Low-level HTTP helpers
    # -------------------------------------------------------------------

    def _request(self, method, endpoint, data=None, params=None, timeout=None):
        """Make an HTTP request to the WooCommerce REST API.
        
        Args:
            method: 'GET', 'POST', 'PUT', 'DELETE'
            endpoint: API endpoint (e.g., 'products', 'orders/123')
            data: JSON-serializable dict for POST/PUT body
            params: URL query parameters
            timeout: Request timeout in seconds
        
        Returns:
            Parsed JSON response
        
        Raises:
            requests.RequestException: On HTTP/network errors
            UserError: On API errors with meaningful messages
        """
        timeout = timeout or WOO_REQUEST_TIMEOUT
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        
        self._rate_limit()
        
        kwargs = {
            'auth': self.auth,
            'timeout': timeout,
        }
        if params:
            kwargs['params'] = params
        if data is not None:
            kwargs['data'] = json.dumps(data)
        
        _logger.debug('WooCommerce API %s %s', method, url)
        
        response = self.session.request(method, url, **kwargs)
        
        # WooCommerce returns meaningful error messages
        if response.status_code >= 400:
            try:
                error_data = response.json()
                error_msg = error_data.get('message', response.text)
            except (ValueError, AttributeError):
                error_msg = response.text or f'HTTP {response.status_code}'
            
            raise UserError(
                _('WooCommerce API Error (%s): %s') % (response.status_code, error_msg))
        
        # 204 No Content (e.g., DELETE success)
        if response.status_code == 204:
            return {'success': True}
        
        try:
            return response.json()
        except ValueError:
            return {'raw': response.text}

    def _get_all_pages(self, endpoint, params=None, per_page=100):
        """Fetch all pages of a paginated WooCommerce endpoint.
        
        Args:
            endpoint: API endpoint
            params: Additional query parameters
            per_page: Items per page (max 100)
        
        Yields:
            Individual items from all pages
        """
        page = 1
        params = dict(params or {})
        params['per_page'] = per_page
        
        while True:
            params['page'] = page
            items = self._request('GET', endpoint, params=params)
            if not items:
                break
            yield from items
            if len(items) < per_page:
                break
            page += 1

    # -------------------------------------------------------------------
    # Connection Test
    # -------------------------------------------------------------------

    def _test_connection(self):
        """Test the API connection and return a status message."""
        try:
            result = self._request('GET', 'system_status')
            # WooCommerce exposes the plugin version under
            # ``system_status.environment.version``. Older connector code
            # only checked ``data.wc_version`` and therefore reported
            # successful connections as "Unknown".
            environment = result.get('environment') or {}
            data = result.get('data') or {}
            store_name = (
                result.get('name')
                or data.get('name')
                or environment.get('home_url')
                or environment.get('site_url')
                or self.config.website_url
                or 'WooCommerce'
            )
            wc_version = (
                result.get('wc_version')
                or result.get('version')
                or data.get('wc_version')
                or data.get('version')
                or environment.get('version')
                or 'unknown'
            )
            msg = _('Connected to "%s" (WooCommerce v%s)') % (store_name, wc_version)
            self.env['woo.sync.log'].log_sync(
                self.config, 'woo.config', 'connection_test', 'odoo_to_woo',
                msg, state='success')
            return msg
        except Exception as e:
            error_msg = _('Connection failed: %s') % str(e)
            self.env['woo.sync.log'].log_sync(
                self.config, 'woo.config', 'connection_test', 'odoo_to_woo',
                error_msg, state='error')
            raise

    # ===================================================================
    # MATCHING LOGIC (Section 4 of the spec)
    # ===================================================================

    def _match_product_by_sku(self, sku):
        """Match a WooCommerce product/variant to an Odoo product by SKU/Barcode.
        
        Matching logic:
        1. Search product.product WHERE barcode == SKU.
        2. If exactly one match → return it.
        3. If multiple matches (duplicate barcodes in Odoo) → block the import.
        4. If no match → return empty recordset.
        
        Args:
            sku: WooCommerce SKU string
        
        Returns:
            product.product recordset (may be empty)
        """
        if not sku:
            return self.env['product.product']
        
        products = self.env['product.product'].with_company(self.config.company_id).search([
            ('barcode', '=', sku),
            '|', ('company_id', '=', False), ('company_id', '=', self.config.company_id.id),
        ], limit=2)
        
        if len(products) == 1:
            return products
        elif len(products) > 1:
            _logger.warning(
                'Multiple Odoo products found with barcode "%s": %s. Cannot determine unique match.',
                sku, products.ids)
            self.env['woo.sync.log'].log_sync(
                self.config, 'product.product', 'skip', 'woo_to_odoo',
                _('Multiple products found with barcode "%s" (IDs: %s). '
                  'Please fix duplicate barcodes in Odoo.') % (sku, products.ids),
                woo_id=sku, state='error')
            raise ValidationError(
                _('Multiple products found with barcode "%s" (IDs: %s). '
                  'Fix the duplicate barcode before synchronization.')
                % (sku, products.ids))
        
        # No match
        return self.env['product.product']

    def _resolve_import_template(self, woo_product_data):
        """Resolve product identity without unsafe name-based matching."""
        woo_id = woo_product_data.get('id')
        sku = (woo_product_data.get('sku') or '').strip()
        binding = self.env['woo.product.binding'].with_context(active_test=False).search([
            ('config_id', '=', self.config.id),
            ('woo_product_id', '=', woo_id),
        ], limit=1)
        bound_template = binding.product_tmpl_id
        barcode_product = self._match_product_by_sku(sku) if sku else self.env['product.product']
        if bound_template and barcode_product and barcode_product.product_tmpl_id != bound_template:
            raise ValidationError(
                _('WooCommerce product %(woo)s is linked to %(bound)s, but SKU %(sku)s '
                  'belongs to %(barcode)s. Resolve this identity conflict before syncing.')
                % {
                    'woo': woo_id,
                    'bound': bound_template.display_name,
                    'sku': sku,
                    'barcode': barcode_product.display_name,
                })
        if not binding and barcode_product:
            other_binding = self.env['woo.product.binding'].with_context(active_test=False).search([
                ('config_id', '=', self.config.id),
                ('product_tmpl_id', '=', barcode_product.product_tmpl_id.id),
                ('woo_product_id', '!=', woo_id),
            ], limit=1)
            if other_binding:
                raise ValidationError(
                    _('Barcode %(sku)s already links Odoo product %(product)s to '
                      'WooCommerce product %(woo)s in this store.') % {
                        'sku': sku,
                        'product': barcode_product.display_name,
                        'woo': other_binding.woo_product_id,
                    })
        return bound_template or barcode_product.product_tmpl_id, binding

    def _match_partner_by_email(self, email):
        """Match a WooCommerce customer to an Odoo partner by email.
        
        Priority:
        1. Search by email (case-insensitive).
        2. If found, return the first match.
        3. If not found, return empty recordset.
        
        Args:
            email: Customer email address
        
        Returns:
            res.partner recordset (may be empty)
        """
        if not email:
            return self.env['res.partner']
        
        # Search by email
        partner = self.env['res.partner'].with_company(self.config.company_id).search([
            ('email', '=ilike', email.strip()),
            '|', ('company_id', '=', False), ('company_id', '=', self.config.company_id.id),
        ], limit=1)
        return partner

    def _match_partner_by_woo_id(self, woo_customer_id):
        """Match a WooCommerce customer to an Odoo partner by woo_customer_id.
        
        Args:
            woo_customer_id: WooCommerce customer ID (integer)
        
        Returns:
            res.partner recordset (may be empty)
        """
        if not woo_customer_id:
            return self.env['res.partner']
        
        binding = self.env['woo.partner.binding'].search([
            ('config_id', '=', self.config.id),
            ('woo_customer_id', '=', woo_customer_id),
        ], limit=1)
        if binding:
            return binding.partner_id
        return self.env['res.partner'].with_company(self.config.company_id).search([
            ('woo_customer_id', '=', woo_customer_id),
            '|', ('woo_config_id', '=', self.config.id), ('woo_config_id', '=', False),
            '|', ('company_id', '=', False), ('company_id', '=', self.config.company_id.id),
        ], limit=1)

    def _match_or_create_partner(self, woo_customer_data):
        """Match or create an Odoo partner from WooCommerce customer data.
        
        Strategy:
        1. Try matching by woo_customer_id first.
        2. Then try matching by email.
        3. If not found, create a new partner.
        4. Update partner fields with latest WooCommerce data.
        
        Args:
            woo_customer_data: Dict from WooCommerce API (customer object)
        
        Returns:
            res.partner record
        """
        woo_id = woo_customer_data.get('id')
        email = (woo_customer_data.get('email') or '').strip()
        billing = woo_customer_data.get('billing', {})
        shipping = woo_customer_data.get('shipping', {})
        
        partner = self.env['res.partner']
        
        # 1. Try woo_customer_id first
        if woo_id:
            partner = self._match_partner_by_woo_id(woo_id)
        
        # 2. Try email
        if not partner and email:
            partner = self._match_partner_by_email(email)
        
        # 2b. Try phone (guest checkouts often carry a phone but no email).
        #     Normalize digits so 9905-9878 / 99059878 match the same partner.
        #     ملاحظة Odoo 19: حقل mobile أُزيل من res.partner (اندمج داخل حقل phone)
        if not partner:
            import re as _re
            phone = (billing.get('phone') or '').strip()
            phone_digits = _re.sub(r'\D', '', phone)
            if phone_digits:
                # نبحث عن الشركاء الذين لديهم رقم هاتف فقط (لا نستخدم حقل mobile المحذوف)
                for cand in self.env['res.partner'].search(
                        [('phone', '!=', False)],
                        limit=1000):
                    # نقارن الأرقام بعد إزالة الرموز غير الرقمية (الـ mobile أصبح جزءاً من phone)
                    if _re.sub(r'\D', '', cand.phone or '') == phone_digits:
                        partner = cand
                        break
        
        if partner:
            partner = partner[0]  # Take first match
        
        # Prepare values
        vals = {
            'woo_customer_id': woo_id,
            'woo_config_id': self.config.id,
            'woo_last_sync': fields.Datetime.now(),
            'woo_raw_data': json.dumps(woo_customer_data, indent=2),
        }
        
        # Only set name/contact fields if not already set or if creating new
        if not partner:
            # Create new partner
            first_name = woo_customer_data.get('first_name', '')
            last_name = woo_customer_data.get('last_name', '')
            username = woo_customer_data.get('username', '')
            full_name = f"{first_name} {last_name}".strip()
            if not full_name:
                full_name = username or email or _('WooCommerce Customer #%s') % woo_id
            
            vals.update({
                'name': full_name,
                'email': email,
                'phone': (billing.get('phone') or '').strip(),
                'street': (billing.get('address_1') or '').strip(),
                'street2': (billing.get('address_2') or '').strip(),
                'city': (billing.get('city') or '').strip(),
                'zip': (billing.get('postcode') or '').strip(),
                'customer_rank': 1,  # Mark as customer
            })
            
            # Country
            country_code = billing.get('country', '')
            if country_code:
                country = self.env['res.country'].search(
                    [('code', '=', country_code.upper())], limit=1)
                if country:
                    vals['country_id'] = country.id
            
            # State
            state_name = billing.get('state', '')
            if state_name and vals.get('country_id'):
                state = self.env['res.country.state'].search([
                    ('country_id', '=', vals['country_id']),
                    '|',
                    ('code', '=', state_name),
                    ('name', '=ilike', state_name),
                ], limit=1)
                if state:
                    vals['state_id'] = state.id
            
            vals['company_id'] = self.config.company_id.id
            partner = self.env['res.partner'].with_company(self.config.company_id).create(vals)
            
            self.env['woo.sync.log'].log_sync(
                self.config, 'res.partner', 'create', 'woo_to_odoo',
                _('Created partner "%s" from WooCommerce customer #%s') % (partner.name, woo_id),
                record_id=partner.id, woo_id=str(woo_id))
        else:
            # Update existing partner (don't overwrite manually-set fields)
            update_vals = {k: v for k, v in vals.items() if v}
            # Only update name if it looks generic
            if partner.name.startswith('WooCommerce Customer #'):
                first_name = woo_customer_data.get('first_name', '')
                last_name = woo_customer_data.get('last_name', '')
                full_name = f"{first_name} {last_name}".strip()
                if full_name:
                    update_vals['name'] = full_name
            partner.with_company(self.config.company_id).write(update_vals)
            
            self.env['woo.sync.log'].log_sync(
                self.config, 'res.partner', 'update', 'woo_to_odoo',
                _('Updated partner "%s" from WooCommerce customer #%s') % (partner.name, woo_id),
                record_id=partner.id, woo_id=str(woo_id))
        
        if woo_id:
            binding = self.env['woo.partner.binding'].search([
                ('config_id', '=', self.config.id), ('partner_id', '=', partner.id),
            ], limit=1)
            binding_vals = {
                'config_id': self.config.id,
                'partner_id': partner.id,
                'woo_customer_id': woo_id,
                'last_sync': fields.Datetime.now(),
            }
            if binding:
                binding.write(binding_vals)
            else:
                self.env['woo.partner.binding'].create(binding_vals)

        # Tag the partner as a WooCommerce customer.
        tag = self.config.customer_tag_id
        if tag and tag.id not in partner.category_id.ids:
            partner.write({'category_id': [(4, tag.id)]})
        return partner

    # ===================================================================
    # ORDER SYNC (WooCommerce → Odoo) — Section 5
    # ===================================================================

    def _import_order_from_webhook(self, woo_order_data):
        """Import a single WooCommerce order as an Odoo Quotation.
        
        This is the main entry point for order sync. Called by:
        - Webhook controller (order.created / order.updated)
        - Fallback polling cron
        
        Idempotency: If woo_order_id already exists in Odoo → update the
        existing Quotation (only if still in draft state).
        
        Args:
            woo_order_data: Dict from WooCommerce API or webhook payload
        
        Returns:
            sale.order record (created or updated)
        """
        woo_id = woo_order_data.get('id')
        woo_number = woo_order_data.get('number', '')
        
        if not woo_id:
            raise UserError(_('WooCommerce order data missing "id" field.'))
        
        # --- Idempotency Check ---
        existing_order = self.env['sale.order'].search(
            [('woo_order_id', '=', woo_id),
             ('woo_config_id', '=', self.config.id)], limit=1)
        
        if existing_order:
            return self._update_existing_order(existing_order, woo_order_data)
        
        # --- Create new Quotation ---
        return self._create_order_from_woo_data(woo_order_data)

    def _create_order_from_woo_data(self, woo_data):
        """Create a new sale.order (Quotation) from WooCommerce order data.
        
        IMPORTANT: The order is created as state='draft' (Quotation).
        NEVER auto-confirm — manual review is required by company policy.
        """
        woo_id = woo_data.get('id')
        
        start_time = time.time()
        log_prefix = _('Order #%s') % woo_id
        
        try:
            if not self.config.pricelist_id:
                raise ValidationError(_('Configure a WooCommerce pricelist before importing orders.'))
            woo_currency = (woo_data.get('currency') or '').upper()
            odoo_currency = self.config.pricelist_id.currency_id.name.upper()
            if woo_currency and woo_currency != odoo_currency:
                raise ValidationError(
                    _('WooCommerce order currency %(woo)s does not match configured pricelist currency %(odoo)s.')
                    % {'woo': woo_currency, 'odoo': odoo_currency})
            # 1. Match/Create Customer
            customer_data = woo_data.get('customer', {})
            billing = woo_data.get('billing', {})
            
            # WooCommerce sometimes puts customer info in billing instead of
            # the customer object — notably guest checkouts, which may have a
            # name/phone/address but NO email. Always fall back to billing when
            # the order carries no customer id, so the partner is not lost.
            if not customer_data.get('id') and (
                    billing.get('email') or billing.get('first_name')
                    or billing.get('last_name') or billing.get('phone')):
                customer_data = {
                    'id': woo_data.get('customer_id', 0),
                    'email': billing.get('email', ''),
                    'first_name': billing.get('first_name', ''),
                    'last_name': billing.get('last_name', ''),
                    'username': billing.get('email', '').split('@')[0] if billing.get('email') else '',
                    'billing': billing,
                    'shipping': woo_data.get('shipping', {}),
                }
            
            partner = self._match_or_create_partner(customer_data)
            
            # 2. Determine fiscal position and pricelist
            # (Use Odoo defaults — can be enhanced with WooCommerce-specific logic)
            fiscal_position_id = partner.property_account_position_id.id if partner.property_account_position_id else False
            pricelist_id = self.config.pricelist_id.id
            
            # 3. Prepare order header
            refunds = woo_data.get('refunds') or []
            refund_total = sum(abs(float(refund.get('total', 0) or 0)) for refund in refunds)
            order_vals = {
                'partner_id': partner.id,
                'partner_invoice_id': partner.address_get(['invoice'])['invoice'],
                'partner_shipping_id': partner.address_get(['delivery'])['delivery'],
                'company_id': self.config.company_id.id,
                'woo_order_id': woo_id,
                'woo_order_number': woo_data.get('number', ''),
                'woo_config_id': self.config.id,
                'woo_payment_status': woo_data.get('status', 'pending'),
                'woo_payment_method': woo_data.get('payment_method', ''),
                'woo_payment_method_title': woo_data.get('payment_method_title', ''),
                'woo_transaction_id': woo_data.get('transaction_id', ''),
                'woo_date_paid': (
                    self._woo_datetime_to_odoo(
                        woo_data.get('date_paid_gmt') or woo_data.get('date_paid'))
                    if woo_data.get('date_paid_gmt') or woo_data.get('date_paid')
                    else False
                ),
                'woo_refund_total': refund_total,
                'woo_shipping_total': float(woo_data.get('shipping_total', '0') or '0'),
                'woo_discount_total': float(woo_data.get('discount_total', '0') or '0'),
                'woo_customer_note': woo_data.get('customer_note') or '',
                'woo_order_note': woo_data.get('note') or '',
                'woo_raw_payload': json.dumps(woo_data, indent=2),
                'client_order_ref': f'WooCommerce #{woo_data.get("number", woo_id)}',
                'date_order': self._woo_datetime_to_odoo(woo_data.get('date_created_gmt') or woo_data.get('date_created')),
                'state': 'draft',
            }
            
            # Use the dedicated WooCommerce order sequence (e.g. WOO00001) so
            # website sales are clearly separated from regular Odoo quotations.
            if self.config.woo_order_sequence_id:
                order_vals['name'] = self.config.woo_order_sequence_id.next_by_id()
            
            if fiscal_position_id:
                order_vals['fiscal_position_id'] = fiscal_position_id
            order_vals['pricelist_id'] = pricelist_id

            # Apply the configured Odoo payment term for this payment method.
            payment_term = self._get_payment_term_for_method(
                woo_data.get('payment_method', ''))
            if payment_term:
                order_vals['payment_term_id'] = payment_term.id
            
            # 4. Create the order
            order = self.env['sale.order'].create(order_vals)
            
            # 5. Add line items
            self._add_order_lines(order, woo_data)
            
            # 6. Handle shipping as a separate line
            shipping_total = float(woo_data.get('shipping_total', '0') or '0')
            if shipping_total > 0:
                self._add_shipping_line(order, woo_data)
            
            # 7. Handle discounts (if not already per-line)
            discount_total = float(woo_data.get('discount_total', '0') or '0')
            if discount_total > 0:
                self._add_discount_line(order, woo_data, -discount_total)
            
            # 7b. Handle extra fee lines (COD / payment fees)
            self._add_fee_lines(order, woo_data)
            
            # 8. Verify total matches (within tolerance)
            self._verify_order_total(order, woo_data)

            # A newly discovered cancelled/failed Woo order must never remain
            # as a confirmable draft in Odoo.
            if woo_data.get('status') in ('cancelled', 'failed'):
                order.action_cancel()
            
            duration = time.time() - start_time
            self.env['woo.sync.log'].log_sync(
                self.config, 'sale.order', 'create', 'woo_to_odoo',
                _('%(prefix)s: Created %(document)s %(order)s') % {
                    'prefix': log_prefix,
                    'document': _('cancelled quotation') if order.state == 'cancel' else _('quotation'),
                    'order': order.name,
                },
                record_id=order.id, woo_id=str(woo_id),
                raw_request=json.dumps(woo_data, indent=2),
                duration=duration)
            
            _logger.info('%s: Created Quotation %s (ID: %s)', log_prefix, order.name, order.id)
            
            return order
            
        except Exception as e:
            duration = time.time() - start_time
            _logger.exception('%s: Error creating order', log_prefix)
            self.env['woo.sync.log'].log_sync(
                self.config, 'sale.order', 'error', 'woo_to_odoo',
                _('%s: Error: %s') % (log_prefix, str(e)),
                woo_id=str(woo_id),
                raw_request=json.dumps(woo_data, indent=2),
                state='error', duration=duration)
            raise

    def _update_existing_order(self, order, woo_data):
        """Update an existing sale.order draft with new WooCommerce data.
        
        ONLY updates if the order is still in 'draft' state. If the order has
        been confirmed or cancelled, logs a 'manual_review_required' message.
        
        Section 8.1: Handles line changes, cancellations, and partial updates.
        """
        woo_id = woo_data.get('id')
        log_prefix = _('Order #%s') % woo_id
        
        if order.state != 'draft':
            # Cannot auto-update confirmed/cancelled orders
            self.env['woo.sync.log'].log_sync(
                self.config, 'sale.order', 'manual_review_required', 'woo_to_odoo',
                _('%s: Order is already %s in Odoo (ID: %s). Webhook update '
                  'requires manual review. WooCommerce status: %s') % (
                    log_prefix, order.state, order.id,
                    woo_data.get('status', 'unknown')),
                record_id=order.id, woo_id=str(woo_id),
                state='warning')
            _logger.warning(
                '%s: Skipping update — Odoo order %s is in state "%s" (not draft)',
                log_prefix, order.name, order.state)
            return order

        woo_currency = (woo_data.get('currency') or '').upper()
        if woo_currency and woo_currency != order.currency_id.name.upper():
            raise ValidationError(
                _('WooCommerce currency %(woo)s does not match quotation currency %(odoo)s.')
                % {'woo': woo_currency, 'odoo': order.currency_id.name})
        
        woo_status = woo_data.get('status', '')
        refunds = woo_data.get('refunds') or []
        refund_total = sum(abs(float(refund.get('total', 0) or 0)) for refund in refunds)

        # Update payment and displayed amount information before handling status.
        update_vals = {
            'woo_payment_status': woo_status,
            'woo_payment_method': woo_data.get('payment_method', ''),
            'woo_payment_method_title': woo_data.get('payment_method_title', ''),
            'woo_transaction_id': woo_data.get('transaction_id', ''),
            'woo_date_paid': (
                self._woo_datetime_to_odoo(
                    woo_data.get('date_paid_gmt') or woo_data.get('date_paid'))
                if woo_data.get('date_paid_gmt') or woo_data.get('date_paid')
                else False
            ),
            'woo_refund_total': refund_total,
            'woo_shipping_total': float(woo_data.get('shipping_total', '0') or '0'),
            'woo_discount_total': float(woo_data.get('discount_total', '0') or '0'),
            'woo_customer_note': woo_data.get('customer_note') or '',
            'woo_order_note': woo_data.get('note') or '',
            'woo_raw_payload': json.dumps(woo_data, indent=2),
        }
        order.write(update_vals)

        # Keep the configured payment term in sync.
        payment_term = self._get_payment_term_for_method(
            woo_data.get('payment_method', ''))
        if payment_term and order.payment_term_id != payment_term:
            order.payment_term_id = payment_term

        if woo_status in ('cancelled', 'failed'):
            order.action_cancel()
            self.env['woo.sync.log'].log_sync(
                self.config, 'sale.order', 'cancel', 'woo_to_odoo',
                _('%s: Order cancelled/failed in WooCommerce → cancelled Quotation %s') % (
                    log_prefix, order.name),
                record_id=order.id, woo_id=str(woo_id))
            return order
        
        # Update order lines (replace existing with new)
        # Delete existing non-section lines, then re-add
        existing_lines = order.order_line.filtered(lambda l: not l.display_type)
        existing_lines.unlink()
        self._add_order_lines(order, woo_data)
        
        # Re-add shipping if applicable
        shipping_total = float(woo_data.get('shipping_total', '0') or '0')
        if shipping_total > 0:
            self._add_shipping_line(order, woo_data)
        
        # Re-add discount if applicable
        discount_total = float(woo_data.get('discount_total', '0') or '0')
        if discount_total > 0:
            self._add_discount_line(order, woo_data, -discount_total)

        # Re-add extra fee lines (COD / payment fees)
        self._add_fee_lines(order, woo_data)

        self._verify_order_total(order, woo_data)
        
        self.env['woo.sync.log'].log_sync(
            self.config, 'sale.order', 'line_update', 'woo_to_odoo',
            _('%s: Updated Quotation %s from webhook') % (log_prefix, order.name),
            record_id=order.id, woo_id=str(woo_id))
        
        _logger.info('%s: Updated Quotation %s (ID: %s)', log_prefix, order.name, order.id)
        
        return order

    def _add_order_lines(self, order, woo_data):
        """Add product line items from WooCommerce order to Odoo Quotation."""
        line_items = woo_data.get('line_items', [])
        unmapped_product = None
        
        for item in line_items:
            sku = (item.get('sku') or '').strip()
            product_name = item.get('name', _('Unknown Product'))
            qty = float(item.get('quantity', 0) or 0)
            subtotal = float(item.get('subtotal', item.get('total', 0)) or 0)
            price = subtotal / qty if qty else 0.0
            total = float(item.get('total', 0) or 0)
            woo_line_id = item.get('id')
            
            if qty <= 0:
                continue  # Skip zero-quantity lines
            
            # A Woo variation binding is the authoritative identity for an
            # imported order line.  Looking up the SKU first can select a
            # stale/duplicate Odoo record that happens to hold the barcode.
            product = self.env['product.product']
            variation_id = item.get('variation_id')
            if variation_id:
                variant_binding = self.env['woo.variant.binding'].search([
                    ('config_id', '=', self.config.id),
                    ('woo_variant_id', '=', variation_id),
                ], limit=1)
                product = variant_binding.product_id

            # SKU is the fallback identity when Woo has no known variation
            # binding (for example, the first order before product import).
            if not product and sku:
                product = self._match_product_by_sku(sku)
            
            if not product:
                # A template binding is only safe for simple products.  A
                # variable product binding points to one representative
                # variant and must never be used for another variation.
                woo_product_id = item.get('product_id')
                if woo_product_id and not variation_id:
                    binding = self.env['woo.product.binding'].search([
                        ('config_id', '=', self.config.id),
                        ('woo_product_id', '=', woo_product_id),
                    ], limit=1)
                    template = binding.product_tmpl_id
                    if template:
                        product = template.product_variant_ids[:1]
                
                if not product:
                    if self.config.auto_create_products:
                        # Auto-create (not recommended for production)
                        product = self._auto_create_product(item)
                    else:
                        # Use unmapped product placeholder
                        if not unmapped_product:
                            unmapped_product = self._get_or_create_unmapped_product()
                        product = unmapped_product
                        _logger.warning(
                            'Order #%s: Product SKU "%s" not found in Odoo. Using placeholder.',
                            woo_data.get('id'), sku)
                        self.env['woo.sync.log'].log_sync(
                            self.config, 'product.product', 'skip', 'woo_to_odoo',
                            _('Product SKU "%s" not found for order #%s. Using placeholder.') % (
                                sku, woo_data.get('id')),
                            woo_id=str(woo_data.get('id')), state='warning')
            
            # Create order line
            line_vals = {
                'order_id': order.id,
                'product_id': product.id if product else False,
                'name': f"{product_name} (SKU: {sku})" if sku else product_name,
                'product_uom_qty': qty,
                'price_unit': price,
            }
            
            line_vals['tax_ids'] = [(6, 0, self.config.sale_tax_id.ids)]
            
            self.env['sale.order.line'].create(line_vals)

    def _add_shipping_line(self, order, woo_data):
        """Add shipping as a separate order line."""
        shipping_total = float(woo_data.get('shipping_total', '0') or '0')
        if shipping_total <= 0:
            return
        
        shipping_lines = woo_data.get('shipping_lines', [])
        shipping_method = ''
        if shipping_lines:
            shipping_method = shipping_lines[0].get('method_title', '') or \
                            shipping_lines[0].get('method_id', 'Shipping')
        
        line_name = _('Shipping') + (f' ({shipping_method})' if shipping_method else '')
        
        product = self.config.shipping_product_id
        if not product:
            raise ValidationError(_('Configure a shipping product before importing orders with shipping.'))
        self.env['sale.order.line'].create({
            'order_id': order.id,
            'product_id': product.id,
            'name': line_name,
            'product_uom_qty': 1,
            'price_unit': shipping_total,
            'tax_ids': [(6, 0, product.taxes_id.filtered(
                lambda tax: not tax.company_id or tax.company_id == order.company_id).ids)],
        })

    def _add_discount_line(self, order, woo_data, discount_total):
        """Add discount as a separate (negative) order line."""
        if discount_total >= 0:
            return  # Discounts should be negative; positive discount = error
        
        coupon_lines = woo_data.get('coupon_lines', [])
        coupons = ', '.join([c.get('code', '') for c in coupon_lines if c.get('code')])
        line_name = _('Discount') + (f' ({coupons})' if coupons else '')
        
        product = self.config.discount_product_id
        if not product:
            raise ValidationError(_('Configure a discount product before importing discounted orders.'))
        self.env['sale.order.line'].create({
            'order_id': order.id,
            'product_id': product.id,
            'name': line_name,
            'product_uom_qty': 1,
            'price_unit': discount_total,  # Negative value
            'tax_ids': [(6, 0, product.taxes_id.filtered(
                lambda tax: not tax.company_id or tax.company_id == order.company_id).ids)],
        })

    def _add_fee_lines(self, order, woo_data):
        """Add WooCommerce fee lines (COD / payment fees, etc.) as order lines."""
        fee_lines = woo_data.get('fee_lines') or []
        for fee in fee_lines:
            fee_total = float(fee.get('total', 0) or 0)
            if abs(fee_total) < 1e-9:
                continue
            fee_name = fee.get('name') or _('Fee')
            # 'name' in WooCommerce fee lines often looks like '1.500 KWD fee'
            line_name = _('Fee') + (f' ({fee_name})' if fee_name else '')
            product = self.config.fee_product_id
            if not product:
                raise ValidationError(
                    _('Configure a fee product before importing orders with fees.'))
            self.env['sale.order.line'].create({
                'order_id': order.id,
                'product_id': product.id,
                'name': line_name,
                'product_uom_qty': 1,
                'price_unit': fee_total,
                'tax_ids': [(6, 0, product.taxes_id.filtered(
                    lambda tax: not tax.company_id or tax.company_id == order.company_id).ids)],
            })

    def _verify_order_total(self, order, woo_data):
        """Compare WooCommerce order total with Odoo-computed total.
        
        Logs a warning if the difference exceeds the configured tolerance.
        """
        woo_total = float(woo_data.get('total', '0') or '0')
        odoo_total = order.amount_total
        
        diff = abs(woo_total - odoo_total)
        if diff > self.config.tax_tolerance:
            _logger.warning(
                'Order #%s total mismatch: WooCommerce=%s, Odoo=%s (diff=%s, tolerance=%s)',
                woo_data.get('id'), woo_total, odoo_total, diff, self.config.tax_tolerance)
            self.env['woo.sync.log'].log_sync(
                self.config, 'sale.order', 'update', 'woo_to_odoo',
                _('Total mismatch for order #%s: WooCommerce=%s, Odoo=%s, diff=%s') % (
                    woo_data.get('id'), woo_total, odoo_total, diff),
                record_id=order.id, woo_id=str(woo_data.get('id')), state='warning')
            if self.config.strict_total_validation:
                raise ValidationError(
                    _('Order total mismatch. WooCommerce: %(woo)s, Odoo: %(odoo)s, difference: %(diff)s.')
                    % {'woo': woo_total, 'odoo': odoo_total, 'diff': diff})

    def _update_order_status_on_woo(self, order, status):
        """Push an order status back to WooCommerce (write-back)."""
        woo_id = order.woo_order_id
        if not woo_id:
            return
        try:
            self._request('PUT', f'orders/{woo_id}', data={'status': status})
            _logger.info('WooCommerce order #%s status updated to "%s"',
                         woo_id, status)
            self.env['woo.sync.log'].log_sync(
                self.config, 'sale.order', 'update', 'odoo_to_woo',
                _('Pushed status "%(status)s" to WooCommerce order #%(woo)s.')
                % {'status': status, 'woo': woo_id},
                record_id=order.id, woo_id=str(woo_id), state='success')
        except Exception as exc:
            _logger.warning('Could not update WooCommerce order #%s status: %s',
                            woo_id, exc)
            self.env['woo.sync.log'].log_sync(
                self.config, 'sale.order', 'error', 'odoo_to_woo',
                _('Could not push status "%(status)s" to WooCommerce order '
                  '#%(woo)s: %(error)s')
                % {'status': status, 'woo': woo_id, 'error': exc},
                record_id=order.id, woo_id=str(woo_id), state='error')
            raise

    def _get_payment_term_for_method(self, method):
        """Return the configured Odoo payment term for a Woo payment method."""
        if not method:
            return self.env['account.payment.term']
        mapping = self.env['woo.payment.method.map'].search([
            ('config_id', '=', self.config.id),
            ('woo_payment_method', '=ilike', method.strip()),
        ], limit=1)
        return mapping.payment_term_id

    def _import_all_orders(self):
        """Import ALL WooCommerce orders (full backfill).

        Unlike _poll_recent_orders this ignores the last-poll high water mark
        and walks every order page. Idempotency is preserved via woo_order_id.
        """
        stats = {'created': 0, 'updated': 0, 'failed': 0}
        for order_data in self._get_all_pages('orders', per_page=50):
            try:
                existing = self.env['sale.order'].search([
                    ('woo_order_id', '=', order_data.get('id')),
                    ('woo_config_id', '=', self.config.id),
                ], limit=1)
                self._import_order_from_webhook(order_data)
                if not existing:
                    stats['created'] += 1
                elif existing.state == 'draft':
                    stats['updated'] += 1
            except Exception as exc:
                stats['failed'] += 1
                _logger.exception(
                    'Error importing order #%s during full backfill',
                    order_data.get('id'))
        return stats

    def _import_customers_from_orders(self):
        """Create/update Odoo partners from WooCommerce order billing data.

        Captures every shopper including guest checkouts that have no customer
        record in the /customers endpoint. Idempotent by billing email.
        """
        stats = {'created': 0, 'updated': 0, 'failed': 0}
        seen = set()
        for order_data in self._get_all_pages('orders', per_page=50):
            try:
                billing = order_data.get('billing') or {}
                email = (billing.get('email') or '').strip()
                email_key = email.lower() if email else ''
                if not email_key or email_key in seen:
                    continue
                seen.add(email_key)
                customer_data = {
                    'id': order_data.get('customer_id', 0) or 0,
                    'email': email,
                    'first_name': billing.get('first_name', '') or '',
                    'last_name': billing.get('last_name', '') or '',
                    'username': email.split('@')[0] if email else '',
                    'billing': billing,
                    'shipping': order_data.get('shipping', {}) or {},
                }
                existing = self.env['res.partner'].with_company(
                    self.config.company_id).search([
                        ('email', '=ilike', email),
                        '|', ('company_id', '=', False),
                             ('company_id', '=', self.config.company_id.id),
                    ], limit=1)
                self._match_or_create_partner(customer_data)
                if existing:
                    stats['updated'] += 1
                else:
                    stats['created'] += 1
            except Exception as exc:
                stats['failed'] += 1
                _logger.exception(
                    'Error importing customer from order #%s: %s',
                    order_data.get('id'), exc)
        return stats

    def _auto_create_product(self, item):
        """Auto-create a product from WooCommerce line item data.
        
        ⚠️ NOT recommended for production without manual review.
        Only called when auto_create_products is enabled.
        """
        sku = item.get('sku', '')
        name = item.get('name', _('Imported Product'))
        price = float(item.get('price', 0) or 0)
        
        template = self.env['product.template'].create({
            'name': name,
            'list_price': price,
            'sync_to_woo': False,  # Don't re-export auto-created products
            'woo_product_id': item.get('product_id', 0),
            'woo_config_id': self.config.id,
        })
        
        product = template.product_variant_ids[:1]
        if sku:
            product.barcode = sku
        if item.get('product_id'):
            self.env['woo.product.binding'].create({
                'config_id': self.config.id,
                'product_tmpl_id': template.id,
                'product_id': product.id,
                'woo_product_id': item['product_id'],
                'last_sync': fields.Datetime.now(),
            })
        
        self.env['woo.sync.log'].log_sync(
            self.config, 'product.template', 'create', 'woo_to_odoo',
            _('Auto-created product "%s" from WooCommerce order item') % name,
            record_id=template.id, woo_id=str(item.get('product_id', '')),
            state='warning')
        
        return product

    def _get_or_create_unmapped_product(self):
        """Get or create the placeholder 'Unmapped Product' for unmatched items."""
        # Search for existing unmapped product
        existing = self.env['product.template'].search(
            [('name', '=', 'Unmapped WooCommerce Product')], limit=1)
        if existing:
            return existing.product_variant_ids[:1]
        
        # Create on the fly
        template = self.env['product.template'].create({
            'name': _('Unmapped WooCommerce Product'),
            'list_price': 0.0,
            'sync_to_woo': False,
            'active': True,
        })
        return template.product_variant_ids[:1]

    # ===================================================================
    # PRODUCT IMPORT (WooCommerce → Odoo)
    # ===================================================================

    def _push_sku_to_woo(self, endpoint, sku, woo_id, context_label='product',
                         odoo_variant=None):
        """Mirror a local Odoo barcode back to WooCommerce as the SKU.

        Used when a WooCommerce product/variation arrives without a SKU: the
        imported Odoo variant keeps its (possibly auto-generated) barcode, and
        we push that barcode back to WooCommerce so both catalogs stay aligned.
        This NEVER overwrites an existing WooCommerce SKU - it only fills in
        missing ones.

        If WooCommerce rejects the SKU as a duplicate, a fresh barcode is drawn
        from the product.barcode.auto sequence, the Odoo variant is updated to
        match, and the push is retried once.
        """
        if not endpoint or not sku:
            return
        for attempt in range(2):
            try:
                self._request('PUT', endpoint, data={'sku': sku})
                _logger.info('WooCommerce %s #%s: SKU set to "%s"',
                             context_label, woo_id, sku)
                self.env['woo.sync.log'].log_sync(
                    self.config, 'product.template', 'update', 'odoo_to_woo',
                    _('Pushed barcode "%(sku)s" to WooCommerce %(label)s #%(woo)s as SKU.')
                    % {'sku': sku, 'label': context_label, 'woo': woo_id},
                    woo_id=str(woo_id), state='success')
                return
            except Exception as exc:
                err_msg = str(exc)
                duplicate = 'duplicat' in err_msg.lower() or 'invalid' in err_msg.lower()
                if attempt == 0 and duplicate and odoo_variant:
                    old_sku = sku
                    try:
                        new_sku = self.env['ir.sequence'].next_by_code(
                            'flousflow.product.barcode')
                        if not new_sku:
                            new_sku = self.env['ir.sequence'].next_by_code(
                                'product.barcode.auto')
                        if new_sku and new_sku != old_sku:
                            odoo_variant.barcode = new_sku
                            _logger.warning(
                                'WooCommerce %s #%s: SKU "%s" was a duplicate; '
                                'regenerated barcode to "%s" and retrying.',
                                context_label, woo_id, old_sku, new_sku)
                            sku = new_sku
                            continue
                    except Exception as regen_exc:
                        _logger.warning('Could not regenerate a fresh SKU: %s', regen_exc)
                _logger.warning('Could not push SKU "%s" to WooCommerce %s #%s: %s',
                                sku, context_label, woo_id, err_msg)
                self.env['woo.sync.log'].log_sync(
                    self.config, 'product.template', 'error', 'odoo_to_woo',
                    _('Could not push barcode "%(sku)s" as SKU to WooCommerce '
                      '%(label)s #%(woo)s: %(error)s')
                    % {'sku': sku, 'label': context_label, 'woo': woo_id, 'error': err_msg},
                    woo_id=str(woo_id), state='error')
                return

    # -------------------------------------------------------------------
    # Website Pricelist helpers (no-overwrite pricing)
    # -------------------------------------------------------------------

    def _get_website_price(self, variant):
        """Return the fixed website price for a variant, if set.

        Website prices live in the dedicated website pricelist
        (woo.config.website_pricelist_id) as per-variant fixed-price items,
        completely separate from the main Odoo pricelist that uses the sale
        price on the product card.
        """
        pricelist = self.config.website_pricelist_id
        if not pricelist or not variant:
            return None
        item = self.env['product.pricelist.item'].search([
            ('pricelist_id', '=', pricelist.id),
            ('product_id', '=', variant.id),
            ('applied_on', '=', '0_product_variant'),
        ], limit=1)
        if item and item.fixed_price is not None:
            return float(item.fixed_price)
        return None

    def _set_website_price(self, variant, price):
        """Store a price in the website pricelist WITHOUT overwriting.

        Creates a per-variant fixed-price item only when none exists yet.
        If a price is already stored, it is left untouched (no overwrite).
        Returns True when a new price was created, False otherwise.
        """
        pricelist = self.config.website_pricelist_id
        if not pricelist or not variant or price is None:
            return False
        existing = self.env['product.pricelist.item'].search([
            ('pricelist_id', '=', pricelist.id),
            ('product_id', '=', variant.id),
            ('applied_on', '=', '0_product_variant'),
        ], limit=1)
        if existing:
            return False
        self.env['product.pricelist.item'].create({
            'pricelist_id': pricelist.id,
            'product_id': variant.id,
            'applied_on': '0_product_variant',
            'compute_price': 'fixed',
            'fixed_price': float(price),
        })
        return True

    def _import_product_from_woo(self, woo_product_data):
        """Import a single product from WooCommerce into Odoo with full data.
        
        Imports: name, price, sale_price, description, short_description,
        SKU, categories, images, stock quantity, weight, dimensions, status.
        
        Matching: store binding → unique SKU/barcode. Product names are never
        used as identifiers.
        """
        sku = (woo_product_data.get('sku') or '').strip()
        woo_id = woo_product_data.get('id')
        woo_name = woo_product_data.get('name', _('Imported Product'))
        woo_price = float(woo_product_data.get('price', 0) or woo_product_data.get('regular_price', 0) or 0)
        woo_sale_price = float(woo_product_data.get('sale_price', 0) or 0)
        woo_desc = woo_product_data.get('description', '') or ''
        woo_short_desc = woo_product_data.get('short_description', '') or ''
        woo_status = woo_product_data.get('status', 'publish')
        woo_stock = int(woo_product_data.get('stock_quantity', 0) or 0)
        
        template = self.env['product.template']
        is_new = False

        if self.config.sync_direction_products not in ('woo_to_odoo', 'both'):
            raise UserError(_('Product import is disabled for this WooCommerce store.'))
        if woo_product_data.get('type') == 'variable':
            return self._import_variable_product_from_woo(woo_product_data)

        template, binding = self._resolve_import_template(woo_product_data)
        
        # --- Build create/update values ---
        vals = {
            'woo_product_id': woo_id,
            'woo_last_sync': fields.Datetime.now(),
            'woo_sync_status': 'synced',
        }
        
        if not template:
            # New product — include all fields
            is_new = True
            if not self.config.auto_create_products:
                self.env['woo.sync.log'].log_sync(
                    self.config, 'product.template', 'skip', 'woo_to_odoo',
                    _('Product "%s" (SKU: %s) skipped — auto-create is OFF.') % (woo_name, sku or 'N/A'),
                    woo_id=str(woo_id), state='warning')
                return None
            
            vals.update({
                'name': woo_name,
                'type': 'consu',
                'description': woo_desc,
                'description_sale': woo_short_desc,
                'sync_to_woo': False,
                'active': woo_status == 'publish',
                'woo_config_id': self.config.id,
                'company_id': self.config.company_id.id,
                'taxes_id': [Command.set(self.config.sale_tax_id.ids)],
            })
            # ✅ ALWAYS use config's default category (accounting-linked)
            if self.config.default_category_id:
                vals['categ_id'] = self.config.default_category_id.id
            template = self.env['product.template'].create(vals)
            # ✅ Enable inventory tracking
            if hasattr(template, 'is_storable'):
                template.is_storable = True
        else:
            # Preserve accounting category and manual export choice on updates.
            template = template[0]
            update_vals = dict(vals)
            update_vals.update({
                'name': woo_name,
                'description': woo_desc,
                'description_sale': woo_short_desc,
                'active': woo_status == 'publish',
            })
            template.write(update_vals)
        
        # --- Variant data ---
        variant = template.product_variant_ids[:1]
        if sku:
            if variant.barcode != sku:
                variant.barcode = sku
        elif variant.barcode:
            # The website product has no SKU: mirror the linked Odoo barcode
            # back as its SKU. Existing SKUs are never overwritten.
            self._push_sku_to_woo(
                f'products/{woo_id}', variant.barcode, woo_id,
                context_label='product', odoo_variant=variant)

        # --- Price handling (no-overwrite, separate website pricelist) ---
        # Website prices are stored in the dedicated website pricelist, never
        # in the product card price. Filled prices are never overwritten.
        import_price = woo_price or woo_sale_price or 0.0
        if self.config.website_pricelist_id:
            self._set_website_price(variant, import_price)
        else:
            # Legacy behaviour (no website pricelist configured): keep the
            # product card price in sync with WooCommerce.
            template.list_price = import_price

        # Odoo is the stock source of truth. Never mutate stock.quant directly
        # from a remote catalog import; inventory adjustments require an
        # explicit, auditable stock workflow.
        if woo_product_data.get('stock_quantity') is not None:
            self.env['woo.sync.log'].log_sync(
                self.config, 'product.product', 'skip', 'woo_to_odoo',
                _('WooCommerce stock was not imported for "%s"; Odoo controls inventory.')
                % template.display_name,
                record_id=variant.id, woo_id=str(woo_id), state='skipped')
        
        self._apply_woo_catalog_data(template, woo_product_data)

        binding = binding or self.env['woo.product.binding'].search([
            ('config_id', '=', self.config.id), ('product_tmpl_id', '=', template.id),
        ], limit=1)
        binding_vals = {
            'config_id': self.config.id,
            'product_tmpl_id': template.id,
            'product_id': variant.id,
            'woo_product_id': woo_id,
            'last_sync': fields.Datetime.now(),
            'sync_status': 'synced',
        }
        if binding:
            binding.write(binding_vals)
        else:
            self.env['woo.product.binding'].create(binding_vals)
        
        # --- Log ---
        action = 'create' if is_new else 'update'
        self.env['woo.sync.log'].log_sync(
            self.config, 'product.template', action, 'woo_to_odoo',
            _('%s product "%s" from WooCommerce (#%s)') % (
                _('Created') if is_new else _('Updated'), template.name, woo_id),
            record_id=template.id, woo_id=str(woo_id))
        
        return template

    def _import_variable_product_from_woo(self, woo_product_data):
        """Import a Woo variable product using native Odoo attributes/variants."""
        woo_id = woo_product_data.get('id')
        if not woo_id:
            raise ValidationError(_('Variable WooCommerce product is missing its ID.'))

        variations = list(self._get_all_pages(
            f'products/{woo_id}/variations', per_page=100))

        template, binding = self._resolve_import_template(woo_product_data)
        if not template:
            matched_templates = self.env['product.template']
            for variation in variations:
                sku = (variation.get('sku') or '').strip()
                product = self._match_product_by_sku(sku) if sku else self.env['product.product']
                if product:
                    matched_templates |= product.product_tmpl_id
            if len(matched_templates) > 1:
                raise ValidationError(
                    _('WooCommerce variable product %s has variation barcodes '
                      'belonging to multiple Odoo product templates: %s.')
                    % (woo_id, matched_templates.mapped('display_name')))
            template = matched_templates[:1]
        if not template:
            if not self.config.auto_create_products:
                self.env['woo.sync.log'].log_sync(
                    self.config, 'product.template', 'manual_review_required', 'woo_to_odoo',
                    _('Variable product "%s" is not linked and auto-create is disabled.')
                    % (woo_product_data.get('name') or woo_id),
                    woo_id=str(woo_id), state='warning')
                return None
            template = self.env['product.template'].create({
                'name': woo_product_data.get('name') or _('WooCommerce Product #%s') % woo_id,
                'type': 'consu',
                'is_storable': True,
                'company_id': self.config.company_id.id,
                'categ_id': self.config.default_category_id.id or False,
                'list_price': float(woo_product_data.get('price') or 0.0),
                'description_sale': woo_product_data.get('short_description') or '',
                'description': woo_product_data.get('description') or '',
                'sync_to_woo': False,
                'woo_config_id': self.config.id,
                'woo_product_id': woo_id,
                'woo_sync_status': 'synced',
                'active': woo_product_data.get('status') == 'publish',
                'taxes_id': [Command.set(self.config.sale_tax_id.ids)],
            })
        else:
            template.write({
                'name': woo_product_data.get('name') or template.name,
                'list_price': float(woo_product_data.get('price') or 0.0),
                'description_sale': woo_product_data.get('short_description') or '',
                'description': woo_product_data.get('description') or '',
                'active': woo_product_data.get('status') == 'publish',
                'woo_config_id': self.config.id,
                'woo_product_id': woo_id,
                'woo_sync_status': 'synced',
            })

        # Collect variation attribute options from BOTH the parent product
        # attributes AND the actual variations (WooCommerce frequently lists
        # extra values on variations that are missing from the parent).
        attribute_options = {}
        for remote_attribute in woo_product_data.get('attributes', []):
            if not remote_attribute.get('variation'):
                continue
            attribute_name = (remote_attribute.get('name') or '').strip()
            if not attribute_name:
                continue
            options = {str(option).strip() for option in remote_attribute.get('options', []) if str(option).strip()}
            entry = attribute_options.setdefault(
                attribute_name.casefold(),
                {'name': attribute_name, 'options': set()})
            entry['options'] |= options
        for variation in variations:
            for item in (variation.get('attributes') or []):
                attribute_name = (item.get('name') or '').strip()
                option = (item.get('option') or '').strip()
                if not attribute_name or not option:
                    continue
                entry = attribute_options.setdefault(
                    attribute_name.casefold(),
                    {'name': attribute_name, 'options': set()})
                entry['options'].add(option)

        usable_attribute_count = 0
        new_line_commands = []
        for _key, info in attribute_options.items():
            attribute_name = info['name']
            options = sorted(info['options'])
            if not options:
                continue
            attribute = self.env['product.attribute'].search([
                ('name', '=ilike', attribute_name),
            ], limit=1)
            if not attribute:
                attribute = self.env['product.attribute'].create({
                    'name': attribute_name,
                    'create_variant': 'always',
                })
            elif attribute.create_variant != 'always':
                # The attribute must actually generate variants, otherwise the
                # local combinations can never match the remote variations.
                attribute.write({'create_variant': 'always'})
            values = self.env['product.attribute.value']
            for option in options:
                value = self.env['product.attribute.value'].search([
                    ('attribute_id', '=', attribute.id),
                    ('name', '=ilike', option),
                ], limit=1)
                if not value:
                    value = self.env['product.attribute.value'].create({
                        'attribute_id': attribute.id,
                        'name': option,
                    })
                values |= value
            usable_attribute_count += 1
            existing_line = template.attribute_line_ids.filtered(
                lambda line: line.attribute_id == attribute)[:1]
            if existing_line:
                # WooCommerce may expose only a subset of the variants kept in
                # Odoo (for example, variants migrated from another Odoo
                # database).  Preserve those local values and add any new Woo
                # values instead of replacing the complete attribute line.
                merged_values = existing_line.value_ids | values
                if merged_values != existing_line.value_ids:
                    existing_line.write({
                        'value_ids': [Command.set(merged_values.ids)],
                    })
            else:
                new_line_commands.append(Command.create({
                    'attribute_id': attribute.id,
                    'value_ids': [Command.set(values.ids)],
                }))
        if not usable_attribute_count:
            raise ValidationError(
                _('Variable product "%s" has no usable variation attributes.')
                % template.display_name)
        if new_line_commands:
            template.with_context(active_test=False).write({
                'attribute_line_ids': new_line_commands,
            })
        variants = template.with_context(active_test=False).product_variant_ids

        if binding:
            binding.write({
                'product_id': variants[:1].id,
                'last_sync': fields.Datetime.now(),
                'sync_status': 'synced',
            })
        else:
            binding = self.env['woo.product.binding'].create({
                'config_id': self.config.id,
                'product_tmpl_id': template.id,
                'product_id': variants[:1].id,
                'woo_product_id': woo_id,
                'last_sync': fields.Datetime.now(),
                'sync_status': 'synced',
            })

        local_by_combination = {}
        for variant in variants:
            combination = frozenset(
                (ptav.attribute_id.name.casefold(), ptav.name.casefold())
                for ptav in variant.product_template_attribute_value_ids
            )
            local_by_combination[combination] = variant

        mapped = 0
        unmapped = []
        for variation in variations:
            combination = frozenset(
                ((item.get('name') or '').strip().casefold(),
                 (item.get('option') or '').strip().casefold())
                for item in variation.get('attributes', [])
                if item.get('name') and item.get('option')
            )
            variant = local_by_combination.get(combination)
            if not variant:
                unmapped.append(variation.get('id'))
                continue
            sku = (variation.get('sku') or '').strip()
            if sku:
                if variant.barcode != sku:
                    duplicate = self.env['product.product'].with_context(active_test=False).search([
                        ('barcode', '=', sku), ('id', '!=', variant.id),
                    ], limit=1)
                    if duplicate:
                        raise ValidationError(
                            _(
                                'Woo variation SKU "%(sku)s" is already stored as the '
                                'barcode of "%(duplicate)s". It cannot be assigned to '
                                '"%(variant)s" or moved into Internal Reference. Resolve '
                                'the duplicate barcode before retrying the import.'
                            ) % {
                                'sku': sku,
                                'duplicate': duplicate.display_name,
                                'variant': variant.display_name,
                            })
                    else:
                        variant.barcode = sku
            elif variant.barcode:
                # The website variation has no SKU: mirror the linked Odoo
                # variant's barcode as its SKU. Existing SKUs are untouched.
                self._push_sku_to_woo(
                    f'products/{woo_id}/variations/{variation.get("id")}',
                    variant.barcode, variation.get('id'),
                    context_label='variation', odoo_variant=variant)

            # Website variation price → dedicated website pricelist. Filled
            # prices are never overwritten; only empty ones are filled.
            if self.config.website_pricelist_id:
                variation_price = float(
                    variation.get('price') or variation.get('regular_price') or 0)
                if variation_price:
                    self._set_website_price(variant, variation_price)

            variant_binding = self.env['woo.variant.binding'].search([
                ('config_id', '=', self.config.id),
                ('woo_variant_id', '=', variation.get('id')),
            ], limit=1)
            vals = {
                'config_id': self.config.id,
                'template_binding_id': binding.id,
                'product_id': variant.id,
                'woo_product_id': woo_id,
                'woo_variant_id': variation.get('id'),
                'last_sync': fields.Datetime.now(),
            }
            if variant_binding:
                variant_binding.write(vals)
            else:
                self.env['woo.variant.binding'].create(vals)
            mapped += 1

        if unmapped:
            binding.sync_status = 'error'
            raise ValidationError(
                _('%(count)s variations of "%(product)s" could not be mapped: %(ids)s')
                % {'count': len(unmapped), 'product': template.display_name,
                   'ids': ', '.join(map(str, unmapped[:10]))})

        template.write({
            'woo_config_id': self.config.id,
            'woo_product_id': woo_id,
            'woo_last_sync': fields.Datetime.now(),
            'woo_sync_status': 'synced',
        })
        self._apply_woo_catalog_data(template, woo_product_data)
        self.env['woo.sync.log'].log_sync(
            self.config, 'product.template', 'update', 'woo_to_odoo',
            _('Mapped variable product "%(product)s" with %(count)s variations.')
            % {'product': template.display_name, 'count': mapped},
            record_id=template.id, woo_id=str(woo_id))
        return template

    def _apply_woo_catalog_data(self, template, woo_product_data):
        """Apply non-accounting catalog metadata without touching stock."""
        dimensions = woo_product_data.get('dimensions') or {}
        template.write({
            'weight': float(woo_product_data.get('weight') or 0.0),
            'woo_length': float(dimensions.get('length') or 0.0),
            'woo_width': float(dimensions.get('width') or 0.0),
            'woo_height': float(dimensions.get('height') or 0.0),
        })
        self._import_product_categories(
            template, woo_product_data.get('categories') or [])
        self._import_product_images(
            template, woo_product_data.get('images') or [])

    def _import_product_categories(self, template, woo_categories):
        """Map Woo categories to tags while preserving Odoo's account category."""
        Binding = self.env['woo.category.binding']
        current_config_bindings = Binding.search([
            ('config_id', '=', self.config.id),
        ])
        imported_bindings = Binding
        for remote in woo_categories:
            woo_category_id = remote.get('id')
            name = (remote.get('name') or '').strip()
            if not woo_category_id or not name:
                continue
            binding = Binding.search([
                ('config_id', '=', self.config.id),
                ('woo_category_id', '=', woo_category_id),
            ], limit=1)
            if not binding:
                tag = self.env['product.tag'].search([
                    ('name', '=ilike', name),
                ], limit=1)
                if not tag:
                    tag = self.env['product.tag'].create({'name': name})
                binding = Binding.create({
                    'config_id': self.config.id,
                    'woo_category_id': woo_category_id,
                    'name': name,
                    'slug': remote.get('slug') or '',
                    'parent_woo_category_id': remote.get('parent') or 0,
                    'tag_id': tag.id,
                })
            else:
                binding.write({
                    'name': name,
                    'slug': remote.get('slug') or binding.slug,
                    'parent_woo_category_id': remote.get('parent') or 0,
                })
            imported_bindings |= binding

        other_bindings = template.woo_category_binding_ids.filtered(
            lambda item: item.config_id != self.config)
        template.woo_category_binding_ids = other_bindings | imported_bindings
        old_woo_tags = current_config_bindings.mapped('tag_id')
        non_woo_tags = template.product_tag_ids - old_woo_tags
        template.product_tag_ids = non_woo_tags | imported_bindings.mapped('tag_id')

    def _import_product_images(self, template, woo_images):
        """Import the primary image and an idempotent Woo gallery."""
        import base64 as b64
        Image = self.env['woo.product.image']
        imported_images = Image
        for i, img_data in enumerate(woo_images):
            img_url = img_data.get('src', '')
            if not img_url:
                continue
            try:
                response = self.session.get(img_url, timeout=20)
                response.raise_for_status()
                content_type = (response.headers.get('Content-Type') or '').split(';')[0]
                if not content_type.startswith('image/'):
                    raise ValidationError(
                        _('WooCommerce image URL did not return an image: %s') % img_url)
                if len(response.content) > 15 * 1024 * 1024:
                    raise ValidationError(
                        _('WooCommerce image exceeds the 15 MB safety limit: %s') % img_url)
                encoded = BinaryBytes(response.content)
                if i == 0:
                    template.image_1920 = encoded
                woo_image_id = img_data.get('id') or 0
                image = Image.search([
                    ('config_id', '=', self.config.id),
                    ('product_tmpl_id', '=', template.id),
                    '|', ('woo_image_id', '=', woo_image_id),
                         ('source_url', '=', img_url),
                ], limit=1)
                vals = {
                    'config_id': self.config.id,
                    'product_tmpl_id': template.id,
                    'woo_image_id': woo_image_id,
                    'name': img_data.get('name') or f'{template.name} - {i + 1}',
                    'source_url': img_url,
                    'sequence': i,
                    'image_1920': encoded,
                }
                if image:
                    image.write(vals)
                else:
                    image = Image.create(vals)
                imported_images |= image
            except Exception as exc:
                _logger.warning('Could not import image %s for product %s: %s',
                                img_url, template.display_name, exc)
                self.env['woo.sync.log'].log_sync(
                    self.config, 'product.template', 'image_error', 'woo_to_odoo',
                    _('Could not import image %(url)s for %(product)s: %(error)s') % {
                        'url': img_url, 'product': template.display_name, 'error': str(exc),
                    }, record_id=template.id, state='warning')
        stale = Image.search([
            ('config_id', '=', self.config.id),
            ('product_tmpl_id', '=', template.id),
            ('id', 'not in', imported_images.ids),
        ])
        stale.unlink()

    # ===================================================================
    # PRODUCT SYNC (Odoo → WooCommerce) — Section 5
    # ===================================================================

    def _sync_product_to_woo(self, product_id):
        """Export a single product template to WooCommerce.
        
        Simple products (one variant) are exported as WooCommerce 'simple'
        products. Configurable products (multiple variants) are exported as
        'variable' products with one variation per Odoo variant, each carrying
        its own barcode as the SKU, so every size/colour on the website maps
        1:1 to an Odoo variant.
        
        Args:
            product_id: product.template ID
        
        Returns:
            WooCommerce product ID (int)
        """
        template = self.env['product.template'].browse(product_id)
        if not template.exists():
            raise UserError(_('Product template #%s not found.') % product_id)
        if not template.sync_to_woo:
            raise UserError(
                _('Enable "Sync to WooCommerce" on this product before exporting it.'))
        if self.config.sync_direction_products not in ('odoo_to_woo', 'both'):
            raise UserError(_('Product export is disabled for this WooCommerce store.'))

        if len(template.product_variant_ids) > 1:
            return self._sync_variable_product_to_woo(template)
        return self._sync_simple_product_to_woo(template)

    def _sync_simple_product_to_woo(self, template):
        """Export a single-variant product as a WooCommerce simple product."""
        start_time = time.time()

        try:
            binding = self.env['woo.product.binding'].search([
                ('config_id', '=', self.config.id),
                ('product_tmpl_id', '=', template.id),
            ], limit=1)
            woo_product_id = binding.woo_product_id or (
                template.woo_product_id if template.woo_config_id == self.config else False)
            is_new = not woo_product_id

            # Website price comes from the dedicated website pricelist. Never
            # overwrite an existing website price: when there is no website
            # price and the product already exists on the site, no price is
            # sent at all. Brand-new pushes fall back to the card price so the
            # product is not price-less on the website.
            variant = template.product_variant_ids[:1]
            price = self._get_website_price(variant)
            if price is None and is_new:
                price = template.list_price

            woo_product_data = self._build_woo_product_data(template, price=price)

            if woo_product_id:
                # Update existing product
                endpoint = f"products/{woo_product_id}"
                response = self._request('PUT', endpoint, data=woo_product_data)
                operation = 'update'
                msg = _('Updated product "%s" in WooCommerce (ID: %s)') % (
                    template.name, woo_product_id)
            else:
                # Create new product
                response = self._request('POST', 'products', data=woo_product_data)
                woo_id = response.get('id')
                template.write({
                    'woo_product_id': woo_id,
                    'woo_config_id': self.config.id,
                    'woo_sync_status': 'synced',
                    'woo_last_sync': fields.Datetime.now(),
                })
                operation = 'create'
                msg = _('Created product "%s" in WooCommerce (ID: %s)') % (
                    template.name, woo_id)

            woo_product_id = response.get('id') or woo_product_id
            binding_vals = {
                'config_id': self.config.id,
                'product_tmpl_id': template.id,
                'product_id': template.product_variant_ids[:1].id,
                'woo_product_id': woo_product_id,
                'last_sync': fields.Datetime.now(),
                'sync_status': 'synced',
            }
            if binding:
                binding.write(binding_vals)
            else:
                self.env['woo.product.binding'].create(binding_vals)

            template.woo_last_sync = fields.Datetime.now()
            template.woo_sync_status = 'synced'

            duration = time.time() - start_time
            self.env['woo.sync.log'].log_sync(
                self.config, 'product.template', operation, 'odoo_to_woo',
                msg, record_id=template.id,
                woo_id=str(woo_product_id),
                raw_request=json.dumps(woo_product_data, indent=2),
                raw_response=json.dumps(response, indent=2),
                duration=duration)

            return woo_product_id

        except Exception as e:
            template.woo_sync_status = 'error'
            duration = time.time() - start_time
            self.env['woo.sync.log'].log_sync(
                self.config, 'product.template', 'error', 'odoo_to_woo',
                _('Error syncing product "%s": %s') % (template.name, str(e)),
                record_id=template.id, state='error', duration=duration)
            raise

    def _sync_variable_product_to_woo(self, template):
        """Export a configurable product as a WooCommerce variable product.

        The parent product carries the attribute definitions and every Odoo
        variant is pushed as a variation with its own SKU (barcode), price and
        stock. `woo.variant.binding` records are kept in sync so later updates
        hit the correct variation endpoint.
        """
        start_time = time.time()
        try:
            binding = self.env['woo.product.binding'].search([
                ('config_id', '=', self.config.id),
                ('product_tmpl_id', '=', template.id),
            ], limit=1)
            woo_product_id = binding.woo_product_id or (
                template.woo_product_id if template.woo_config_id == self.config else False)

            parent_data = self._build_woo_variable_parent_data(template)
            if woo_product_id:
                response = self._request(
                    'PUT', f'products/{woo_product_id}', data=parent_data)
                operation = 'update'
                msg = _('Updated variable product "%s" in WooCommerce (ID: %s)') % (
                    template.name, woo_product_id)
            else:
                response = self._request('POST', 'products', data=parent_data)
                woo_product_id = response.get('id')
                template.write({
                    'woo_product_id': woo_product_id,
                    'woo_config_id': self.config.id,
                    'woo_sync_status': 'synced',
                    'woo_last_sync': fields.Datetime.now(),
                })
                operation = 'create'
                msg = _('Created variable product "%s" in WooCommerce (ID: %s)') % (
                    template.name, woo_product_id)

            woo_product_id = response.get('id') or woo_product_id
            binding_vals = {
                'config_id': self.config.id,
                'product_tmpl_id': template.id,
                'product_id': template.product_variant_ids[:1].id,
                'woo_product_id': woo_product_id,
                'last_sync': fields.Datetime.now(),
                'sync_status': 'synced',
            }
            if binding:
                binding.write(binding_vals)
            else:
                binding = self.env['woo.product.binding'].create(binding_vals)

            # Push every variant as a variation (SKU = barcode)
            for variant in template.product_variant_ids:
                self._sync_variant_to_woo(template, binding, woo_product_id, variant)

            template.woo_last_sync = fields.Datetime.now()
            template.woo_sync_status = 'synced'

            duration = time.time() - start_time
            self.env['woo.sync.log'].log_sync(
                self.config, 'product.template', operation, 'odoo_to_woo',
                msg, record_id=template.id,
                woo_id=str(woo_product_id),
                raw_request=json.dumps(parent_data, indent=2),
                duration=duration)

            return woo_product_id

        except Exception as e:
            template.woo_sync_status = 'error'
            duration = time.time() - start_time
            self.env['woo.sync.log'].log_sync(
                self.config, 'product.template', 'error', 'odoo_to_woo',
                _('Error syncing variable product "%s": %s') % (template.name, str(e)),
                record_id=template.id, state='error', duration=duration)
            raise

    def _build_woo_variable_parent_data(self, template):
        """Build the parent 'variable' product payload with attribute definitions."""
        data = {
            'name': template.name,
            'type': 'variable',
            'description': template.description_sale or '',
            'short_description': template.description or '',
            'status': 'publish' if template.active else 'draft',
            'catalog_visibility': 'visible',
            'attributes': [],
        }

        for line in template.attribute_line_ids:
            values = line.value_ids
            if not values:
                continue
            data['attributes'].append({
                'name': line.attribute_id.name,
                'variation': True,
                'options': [value.name for value in values],
            })

        category_bindings = template.woo_category_binding_ids.filtered(
            lambda item: item.config_id == self.config)
        if category_bindings:
            data['categories'] = [
                {'id': binding.woo_category_id}
                for binding in category_bindings
            ]

        base_url = self.env['ir.config_parameter'].sudo().get_str('web.base.url')
        images = []
        if template.image_1920:
            images.append({
                'src': f'{base_url}/web/image/product.template/{template.id}/image_1920',
            })
        for gallery_image in template.woo_image_ids.filtered(
                lambda image: image.config_id == self.config)[1:]:
            images.append({
                'src': f'{base_url}/web/image/woo.product.image/{gallery_image.id}/image_1920',
            })
        if images:
            data['images'] = images

        return data

    def _build_woo_variation_data(self, variant, price=None):
        """Build a single variation payload (SKU = barcode, price, stock, images, attributes).

        Args:
            variant: product.product record
            price: price to send as regular_price. When None, no price is sent
                so the website keeps its existing price (no overwrite).
        """
        template = variant.product_tmpl_id
        stock_variant = variant.with_context(
            warehouse_id=self.config.warehouse_id.id,
            location=self.config.warehouse_id.lot_stock_id.id,
            allowed_company_ids=[self.config.company_id.id],
        ).with_company(self.config.company_id)

        data = {
            'sku': variant.barcode or '',
            'manage_stock': True,
            'stock_quantity': max(0, int(stock_variant.free_qty)),
            'attributes': [
                {'name': ptav.attribute_id.name, 'option': ptav.name}
                for ptav in variant.product_template_attribute_value_ids
            ],
        }
        if price is not None:
            data['regular_price'] = str(price)

        # Push the variant's own image (if any) so each variation on the
        # website shows its specific picture. When the variant has no image,
        # WooCommerce inherits the parent product image by default.
        if variant.image_variant_1920:
            base_url = self.env['ir.config_parameter'].sudo().get_str('web.base.url')
            data['image'] = {
                'src': f'{base_url}/web/image/product.product/{variant.id}/image_1920',
            }

        return data

    def _sync_variant_to_woo(self, template, binding, woo_product_id, variant):
        """Create or update a single WooCommerce variation for an Odoo variant."""
        variant_binding = self.env['woo.variant.binding'].search([
            ('config_id', '=', self.config.id),
            ('product_id', '=', variant.id),
        ], limit=1)
        is_new = not (variant_binding and variant_binding.woo_variant_id)

        # Website price comes from the dedicated website pricelist. Never
        # overwrite: if the variation already exists on the site and has no
        # website price, no price is sent. New variations fall back to the
        # card price.
        price = self._get_website_price(variant)
        if price is None and is_new:
            price = template.list_price

        variation_data = self._build_woo_variation_data(variant, price=price)

        if variant_binding and variant_binding.woo_variant_id:
            self._request(
                'PUT',
                f'products/{woo_product_id}/variations/{variant_binding.woo_variant_id}',
                data=variation_data)
            variant_binding.write({'last_sync': fields.Datetime.now()})
            return

        response = self._request(
            'POST', f'products/{woo_product_id}/variations', data=variation_data)
        woo_variant_id = response.get('id')
        vals = {
            'config_id': self.config.id,
            'template_binding_id': binding.id,
            'product_id': variant.id,
            'woo_product_id': woo_product_id,
            'woo_variant_id': woo_variant_id,
            'last_sync': fields.Datetime.now(),
        }
        if variant_binding:
            variant_binding.write(vals)
        else:
            self.env['woo.variant.binding'].create(vals)

    def _build_woo_product_data(self, template, price=None):
        """Build the WooCommerce API product data dict from an Odoo template.
        
        Includes: name, type, description, short_description, SKU, price,
        stock_quantity, weight, categories, images, status.
        
        Args:
            template: product.template record
            price: price to send as regular_price. When None, no price is sent
                so the website keeps its existing price (no overwrite).
        """
        variant = template.product_variant_ids[:1]
        stock_variant = variant.with_context(
            warehouse_id=self.config.warehouse_id.id,
            location=self.config.warehouse_id.lot_stock_id.id,
            allowed_company_ids=[self.config.company_id.id],
        ).with_company(self.config.company_id)
        
        data = {
            'name': template.name,
            'type': self._map_odoo_type_to_woo(getattr(template, 'detailed_type', 'product') or 'product'),
            'description': template.description_sale or '',
            'short_description': template.description or '',
            'sku': variant.barcode or '',
            'manage_stock': True,
            'stock_quantity': max(0, int(stock_variant.free_qty)),
            'status': 'publish' if template.active else 'draft',
            'catalog_visibility': 'visible',
        }
        if price is not None:
            data['regular_price'] = str(price)
        
        # Weight
        if template.weight:
            data['weight'] = str(template.weight)
        
        category_bindings = template.woo_category_binding_ids.filtered(
            lambda binding: binding.config_id == self.config)
        if category_bindings:
            data['categories'] = [
                {'id': binding.woo_category_id}
                for binding in category_bindings
            ]
        
        # Images — primary Odoo image first, followed by the imported gallery.
        base_url = self.env['ir.config_parameter'].sudo().get_str('web.base.url')
        images = []
        if template.image_1920:
            images.append({
                'src': f'{base_url}/web/image/product.template/{template.id}/image_1920',
            })
        for gallery_image in template.woo_image_ids.filtered(
                lambda image: image.config_id == self.config)[1:]:
            images.append({
                'src': f'{base_url}/web/image/woo.product.image/{gallery_image.id}/image_1920',
            })
        if images:
            data['images'] = images
        
        return data

    def _map_odoo_type_to_woo(self, odoo_type):
        """Map Odoo product type to WooCommerce type."""
        mapping = {
            'product': 'simple',
            'consu': 'simple',
            'service': 'simple',
        }
        return mapping.get(str(odoo_type), 'simple')

    # ===================================================================
    # STOCK SYNC (Odoo → WooCommerce) — Section 6
    # ===================================================================

    def _sync_stock_to_woo(self, product):
        """Update stock quantity for a single product in WooCommerce.
        
        Args:
            product: product.product record
        
        Returns:
            WooCommerce API response
        """
        template = product.product_tmpl_id
        
        binding = self.env['woo.product.binding'].search([
            ('config_id', '=', self.config.id),
            ('product_tmpl_id', '=', template.id),
        ], limit=1)
        variant_binding = self.env['woo.variant.binding'].search([
            ('config_id', '=', self.config.id),
            ('product_id', '=', product.id),
        ], limit=1)
        woo_product_id = binding.woo_product_id
        if not template.sync_to_woo or not woo_product_id:
            return None
        # If the product is archived in Odoo, skip
        if not template.active:
            _logger.debug('Product "%s" is archived in Odoo, skipping stock sync', template.name)
            return None
        
        try:
            stock_product = product.with_context(
                warehouse_id=self.config.warehouse_id.id,
                location=self.config.warehouse_id.lot_stock_id.id,
                allowed_company_ids=[self.config.company_id.id],
            ).with_company(self.config.company_id)
            qty = max(0, int(stock_product.free_qty))
            
            data = {
                'stock_quantity': qty,
                'manage_stock': True,
            }
            
            endpoint = (
                f"products/{woo_product_id}/variations/{variant_binding.woo_variant_id}"
                if variant_binding else f"products/{woo_product_id}"
            )
            response = self._request('PUT', endpoint, data=data)
            
            template.woo_last_sync = fields.Datetime.now()
            
            self.env['woo.sync.log'].log_sync(
                self.config, 'product.product', 'stock_update', 'odoo_to_woo',
                _('Stock updated for "%s": %s → WooCommerce') % (product.display_name, qty),
                record_id=product.id,
                woo_id=str(variant_binding.woo_variant_id or woo_product_id),
                raw_response=json.dumps(response, indent=2))
            
            return response
            
        except Exception as e:
            _logger.exception('Error syncing stock for product "%s"', product.display_name)
            self.env['woo.sync.log'].log_sync(
                self.config, 'product.product', 'error', 'odoo_to_woo',
                _('Stock sync error for "%s": %s') % (product.display_name, str(e)),
                record_id=product.id,
                woo_id=str(variant_binding.woo_variant_id or woo_product_id),
                state='error')
            # Re-queue for retry
            self._queue_stock_update(product)
            raise

    def _sync_stock_batch(self, product_ids):
        """Sync stock for multiple products in a batch.
        
        Uses WooCommerce batch endpoint for efficiency.
        
        Args:
            product_ids: List of product.product IDs
        """
        if not product_ids:
            return
        
        products = self.env['product.product'].browse(product_ids)
        
        # Rate limiting: process in chunks
        chunk_size = 10
        for i in range(0, len(products), chunk_size):
            chunk = products[i:i + chunk_size]
            for product in chunk:
                try:
                    self._sync_stock_to_woo(product)
                except Exception as e:
                    _logger.error('Failed to sync stock for product %s: %s', product.id, e)
                    continue

    def _queue_stock_update(self, product):
        """Queue a stock update for async processing."""
        if not self.config:
            return
        
        binding = self.env['woo.product.binding'].search([
            ('config_id', '=', self.config.id),
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
        ], limit=1)
        if not binding:
            return
        self.env['woo.sync.queue'].create({
            'config_id': self.config.id,
            'model_name': 'product.product',
            'record_id': product.id,
            'woo_id': str(binding.woo_product_id),
            'operation': 'stock_update',
            'direction': 'odoo_to_woo',
            'payload': json.dumps({'product_ids': [product.id]}),
            'priority': 5,  # Stock updates are high priority
        })

    def _queue_stock_batch(self, products):
        """Queue a batch stock update for async processing."""
        if not self.config or not products:
            return
        
        product_ids = products.ids if hasattr(products, 'ids') else list(products)
        self.env['woo.sync.queue'].create({
            'config_id': self.config.id,
            'model_name': 'product.product',
            'woo_id': str(len(product_ids)),
            'operation': 'stock_update',
            'direction': 'odoo_to_woo',
            'payload': json.dumps({'product_ids': product_ids}),
            'priority': 5,
        })

    # ===================================================================
    # FALLBACK POLLING (Section 7)
    # ===================================================================

    def _poll_recent_orders(self, return_stats=False):
        """Poll WooCommerce for orders modified since last successful poll.
        
        This is the fallback safety net for when webhooks fail or Odoo is down.
        """
        poll_started_at = fields.Datetime.now()
        after_date = (
            self.config.last_order_poll
            or fields.Datetime.now() - datetime.timedelta(days=1)
        )
        
        params = {
            'after': after_date.isoformat() if hasattr(after_date, 'isoformat') else str(after_date),
            'orderby': 'modified',
            'order': 'desc',
            'per_page': 50,
        }
        
        stats = {'created': 0, 'updated': 0, 'skipped': 0, 'failed': 0}
        for order_data in self._get_all_pages('orders', params=params, per_page=50):
            try:
                existing = self.env['sale.order'].search([
                    ('woo_order_id', '=', order_data.get('id')),
                    ('woo_config_id', '=', self.config.id),
                ], limit=1)
                self._import_order_from_webhook(order_data)
                if not existing:
                    stats['created'] += 1
                elif existing.state == 'draft':
                    stats['updated'] += 1
                else:
                    stats['skipped'] += 1
            except Exception as e:
                stats['failed'] += 1
                _logger.exception('Error importing order #%s during poll', order_data.get('id'))
                continue

        # Store a dedicated high-water mark only after the API cycle completed.
        # A small overlap prevents missing orders modified at the exact boundary;
        # order idempotency makes the overlap safe.
        self.config.last_order_poll = poll_started_at - datetime.timedelta(seconds=5)
        if return_stats:
            return stats
        return stats['created'] + stats['updated'] + stats['skipped']
