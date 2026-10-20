"""
Unit tests for FlousFlow WooCommerce Connector.

Tests cover:
- Matching logic (product by SKU, partner by email/woo_id)
- Config validation
- Sync log creation
- Queue processing
- Order import logic
"""

import json
import datetime
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError, ValidationError
from odoo import fields


@tagged('woo_connector', 'standard', 'at_install')
class TestWooConfig(TransactionCase):
    """Test woo.config model."""

    def setUp(self):
        super().setUp()
        self.WooConfig = self.env['woo.config']
        self.warehouse = self.env.ref('stock.warehouse0')

    def test_create_config(self):
        """Test basic config creation."""
        config = self.WooConfig.create({
            'name': 'Test Store',
            'website_url': 'https://test.example.com',
            'consumer_key': 'ck_test',
            'consumer_secret': 'cs_test',
            'warehouse_id': self.warehouse.id,
        })
        self.assertTrue(config)
        self.assertEqual(config.api_base_url, 'https://test.example.com/wp-json/wc/v3')

    def test_unique_website_per_company(self):
        """Test that duplicate website URLs are rejected."""
        # First create is fine
        config1 = self.WooConfig.create({
            'name': 'Store 1',
            'website_url': 'https://unique-test.example.com',
            'consumer_key': 'ck_1',
            'consumer_secret': 'cs_1',
            'warehouse_id': self.warehouse.id,
        })
        self.assertTrue(config1)
        # Second with same URL should fail at DB level (constraint)
        # Note: Odoo 19 constraints are DB-level, tested via SQL
        with self.assertRaises(Exception):
            self.WooConfig.create({
                'name': 'Store 2',
                'website_url': 'https://unique-test.example.com',
                'consumer_key': 'ck_2',
                'consumer_secret': 'cs_2',
                'warehouse_id': self.warehouse.id,
            })
        # Rollback the first one to avoid persistent test data
        config1.unlink()

    def test_connection_result_uses_woocommerce_system_status_fields(self):
        """The live WooCommerce response stores its version in environment."""
        config = self.WooConfig.create({
            'name': 'Connection Test Store',
            'website_url': 'https://store.example.com',
            'consumer_key': 'ck_test',
            'consumer_secret': 'cs_test',
            'warehouse_id': self.warehouse.id,
        })
        connector = self.env['woo.connector']._get_connector(config)
        with patch.object(type(connector), '_request', return_value={
                'environment': {
                    'home_url': 'https://store.example.com',
                    'version': '11.2.1',
                },
                'settings': {},
        }):
            message = connector._test_connection()
        self.assertEqual(
            message,
            'Connected to "https://store.example.com" (WooCommerce v11.2.1)',
        )
    def test_root_menu_uses_module_icon(self):
        menu = self.env.ref('flousflow_woo_connector.menu_woo_root')
        self.assertEqual(
            menu.web_icon,
            'flousflow_woo_connector,static/description/icon.png',
        )
        self.assertTrue(menu.web_icon_data)

    def test_api_batch_size_is_bounded(self):
        config = self.WooConfig.create({
            'name': 'Batch Size Store',
            'website_url': 'https://batch.example.com',
            'consumer_key': 'ck_test',
            'consumer_secret': 'cs_test',
            'warehouse_id': self.warehouse.id,
        })
        with self.assertRaises(ValidationError):
            config.write({'auto_sync_batch_size': 101})

    def test_customer_auto_sync_uses_high_water_mark_and_batch_size(self):
        config = self.WooConfig.create({
            'name': 'Incremental Store',
            'website_url': 'https://incremental.example.com',
            'consumer_key': 'ck_test',
            'consumer_secret': 'cs_test',
            'warehouse_id': self.warehouse.id,
            'auto_sync_enabled': True,
            'auto_sync_orders': False,
            'auto_sync_customers': True,
            'auto_sync_batch_size': 25,
            'last_customer_sync': fields.Datetime.now() - datetime.timedelta(hours=1),
        })
        connector = MagicMock()
        connector._get_all_pages.return_value = iter([])
        with patch.object(
                type(self.env['woo.connector']), '_get_connector',
                return_value=connector):
            self.env['woo.connector']._cron_auto_sync()
        kwargs = connector._get_all_pages.call_args.kwargs
        self.assertEqual(kwargs['per_page'], 25)
        self.assertIn('modified_after', kwargs['params'])


@tagged('woo_connector', 'standard', 'at_install')
class TestWooSyncLog(TransactionCase):
    """Test woo.sync.log model."""

    def setUp(self):
        super().setUp()
        self.warehouse = self.env.ref('stock.warehouse0')
        self.config = self.env['woo.config'].create({
            'name': 'Test Store',
            'website_url': 'https://test.example.com',
            'consumer_key': 'ck_test',
            'consumer_secret': 'cs_test',
            'warehouse_id': self.warehouse.id,
        })

    def test_log_sync(self):
        """Test the log_sync convenience method."""
        log = self.env['woo.sync.log'].log_sync(
            self.config, 'sale.order', 'create', 'woo_to_odoo',
            'Test message', record_id=1, woo_id='123',
            state='success')
        self.assertTrue(log)
        self.assertEqual(log.state, 'success')
        self.assertEqual(log.model_name, 'sale.order')
        self.assertEqual(log.operation, 'create')


@tagged('woo_connector', 'standard', 'at_install')
class TestWooSyncQueue(TransactionCase):
    """Test woo.sync.queue model."""

    def setUp(self):
        super().setUp()
        self.warehouse = self.env.ref('stock.warehouse0')
        self.config = self.env['woo.config'].create({
            'name': 'Test Store',
            'website_url': 'https://test.example.com',
            'consumer_key': 'ck_test',
            'consumer_secret': 'cs_test',
            'warehouse_id': self.warehouse.id,
        })

    def test_create_queue_item(self):
        """Test queue item creation."""
        item = self.env['woo.sync.queue'].create({
            'config_id': self.config.id,
            'model_name': 'sale.order',
            'woo_id': '123',
            'operation': 'order_import',
            'direction': 'woo_to_odoo',
            'payload': json.dumps({'id': 123}),
        })
        self.assertEqual(item.state, 'pending')
        self.assertEqual(item.priority, 10)

    def test_cancel_queue_item(self):
        """Test cancelling a pending queue item."""
        item = self.env['woo.sync.queue'].create({
            'config_id': self.config.id,
            'model_name': 'sale.order',
            'woo_id': '123',
            'operation': 'order_import',
            'direction': 'woo_to_odoo',
            'payload': json.dumps({'id': 123}),
        })
        item.action_cancel()
        self.assertEqual(item.state, 'cancelled')

    def test_cannot_delete_processing(self):
        """Test that processing items cannot be deleted."""
        item = self.env['woo.sync.queue'].create({
            'config_id': self.config.id,
            'model_name': 'sale.order',
            'woo_id': '123',
            'operation': 'order_import',
            'direction': 'woo_to_odoo',
            'payload': json.dumps({'id': 123}),
        })
        item.state = 'processing'
        with self.assertRaises(UserError):
            item.unlink()


@tagged('woo_connector', 'standard', 'at_install')
class TestProductExtension(TransactionCase):
    """Test product.template WooCommerce extensions."""

    def setUp(self):
        super().setUp()
        self.ProductTemplate = self.env['product.template']

    def test_product_woo_fields(self):
        """Test that WooCommerce fields exist on product.template."""
        product = self.ProductTemplate.create({
            'name': 'Test Product',
        })
        self.assertFalse(product.sync_to_woo)
        self.assertEqual(product.woo_sync_status, 'pending')
        product.sync_to_woo = True
        product.woo_product_id = 123
        product.woo_sync_status = 'synced'
        self.assertTrue(product.sync_to_woo)
        self.assertEqual(product.woo_product_id, 123)
        self.assertEqual(product.woo_sync_status, 'synced')


@tagged('woo_connector', 'standard', 'at_install')
class TestPartnerExtension(TransactionCase):
    """Test res.partner WooCommerce extensions."""

    def test_partner_woo_fields(self):
        """Test that WooCommerce fields exist on res.partner."""
        partner = self.env['res.partner'].create({
            'name': 'Test Customer',
            'email': 'test@example.com',
        })
        partner.woo_customer_id = 456
        self.assertEqual(partner.woo_customer_id, 456)
        
        # Test search by woo_customer_id
        found = self.env['res.partner'].search([('woo_customer_id', '=', 456)])
        self.assertTrue(found)
        self.assertEqual(found, partner)


@tagged('woo_connector', 'standard', 'at_install')
class TestSaleOrderExtension(TransactionCase):
    """Test sale.order WooCommerce extensions."""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Test Customer',
            'email': 'test@example.com',
        })

    def test_sale_order_woo_fields(self):
        """Test that WooCommerce fields exist on sale.order."""
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'woo_order_id': 789,
            'woo_payment_status': 'pending',
            'woo_payment_method': 'cod',
            'woo_payment_method_title': 'Cash on Delivery',
        })
        self.assertEqual(order.woo_order_id, 789)
        self.assertEqual(order.woo_payment_status, 'pending')
        self.assertEqual(order.state, 'draft')

    def test_unique_woo_order_id(self):
        """Test that duplicate woo_order_id + woo_config_id combinations are rejected."""
        warehouse = self.env.ref('stock.warehouse0')
        config = self.env['woo.config'].create({
            'name': 'Test Unique',
            'website_url': 'https://unique-order-test.example.com',
            'consumer_key': 'ck_test',
            'consumer_secret': 'cs_test',
            'warehouse_id': warehouse.id,
        })
        self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'woo_order_id': 999,
            'woo_config_id': config.id,
        })
        # Same woo_order_id + woo_config_id should fail
        with self.assertRaises(Exception):
            self.env['sale.order'].create({
                'partner_id': self.partner.id,
                'woo_order_id': 999,
                'woo_config_id': config.id,
            })


@tagged('woo_connector', 'standard', 'at_install')
class TestMatchingLogic(TransactionCase):
    """Test the matching logic (Section 4 of the spec)."""

    def setUp(self):
        super().setUp()
        self.warehouse = self.env.ref('stock.warehouse0')
        self.config = self.env['woo.config'].create({
            'name': 'Test Store',
            'website_url': 'https://test.example.com',
            'consumer_key': 'ck_test',
            'consumer_secret': 'cs_test',
            'warehouse_id': self.warehouse.id,
        })
        self.connector = self.env['woo.connector']._get_connector(self.config)

    def test_match_product_by_sku_found(self):
        """Test matching a product by barcode/SKU — found."""
        product = self.env['product.product'].create({
            'name': 'Test Product',
            'barcode': 'SKU-001',
        })
        result = self.connector._match_product_by_sku('SKU-001')
        self.assertTrue(result)
        self.assertEqual(result, product)

    def test_match_product_by_sku_not_found(self):
        """Test matching a product by barcode/SKU — not found."""
        result = self.connector._match_product_by_sku('NONEXISTENT')
        self.assertFalse(result)

    def test_match_product_by_sku_empty(self):
        """Test matching with empty SKU."""
        result = self.connector._match_product_by_sku('')
        self.assertFalse(result)

    def test_match_partner_by_email_found(self):
        """Test matching a partner by email — found."""
        partner = self.env['res.partner'].create({
            'name': 'Test Customer',
            'email': 'match@example.com',
        })
        result = self.connector._match_partner_by_email('match@example.com')
        self.assertTrue(result)
        self.assertEqual(result, partner)

    def test_match_partner_by_email_case_insensitive(self):
        """Test email matching is case-insensitive."""
        partner = self.env['res.partner'].create({
            'name': 'Test Customer',
            'email': 'Match@Example.com',
        })
        result = self.connector._match_partner_by_email('match@example.com')
        self.assertTrue(result)

    def test_match_partner_by_email_not_found(self):
        """Test matching a partner by email — not found."""
        result = self.connector._match_partner_by_email('nonexistent@example.com')
        self.assertFalse(result)

    def test_match_partner_by_woo_id(self):
        """Test matching a partner by woo_customer_id."""
        partner = self.env['res.partner'].create({
            'name': 'Test Customer',
            'email': 'test@example.com',
            'woo_customer_id': 42,
        })
        result = self.connector._match_partner_by_woo_id(42)
        self.assertTrue(result)
        self.assertEqual(result, partner)

    def test_match_or_create_partner_new(self):
        """Test creating a new partner from WooCommerce data."""
        woo_customer = {
            'id': 100,
            'email': 'new@example.com',
            'first_name': 'John',
            'last_name': 'Doe',
            'billing': {
                'phone': '123456789',
                'address_1': '123 Main St',
                'city': 'Cairo',
                'country': 'EG',
            },
            'shipping': {},
        }
        partner = self.connector._match_or_create_partner(woo_customer)
        self.assertTrue(partner)
        self.assertEqual(partner.woo_customer_id, 100)
        self.assertEqual(partner.email, 'new@example.com')
        self.assertEqual(partner.name, 'John Doe')

    def test_match_or_create_partner_existing(self):
        """Test updating an existing partner from WooCommerce data."""
        existing = self.env['res.partner'].create({
            'name': 'Existing Customer',
            'email': 'existing@example.com',
        })
        woo_customer = {
            'id': 200,
            'email': 'existing@example.com',
            'first_name': 'Jane',
            'last_name': 'Smith',
            'billing': {'phone': '987654321'},
            'shipping': {},
        }
        partner = self.connector._match_or_create_partner(woo_customer)
        self.assertEqual(partner, existing)
        self.assertEqual(partner.woo_customer_id, 200)
