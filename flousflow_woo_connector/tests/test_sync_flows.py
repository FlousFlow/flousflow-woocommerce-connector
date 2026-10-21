import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged


@tagged('post_install', '-at_install', 'woo_sync_flows')
class TestWooSyncFlows(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env.ref('stock.warehouse0')
        cls.pricelist = cls.env['product.pricelist'].search([
            ('currency_id', '=', cls.company.currency_id.id),
        ], limit=1)
        if not cls.pricelist:
            cls.pricelist = cls.env['product.pricelist'].create({
                'name': 'Woo Test Pricelist',
                'currency_id': cls.company.currency_id.id,
            })
        cls.shipping = cls.env['product.product'].create({
            'name': 'Woo Shipping', 'type': 'service', 'taxes_id': False,
        })
        cls.discount = cls.env['product.product'].create({
            'name': 'Woo Discount', 'type': 'service', 'taxes_id': False,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Woo Test Product', 'barcode': 'WOO-SKU-1',
            'list_price': 100.0, 'taxes_id': False,
        })
        cls.product.product_tmpl_id.sync_to_woo = True
        cls.config = cls.env['woo.config'].create({
            'name': 'Flow Test Store',
            'website_url': 'https://flow-test.example.com',
            'consumer_key': 'ck_test',
            'consumer_secret': 'cs_test',
            'webhook_secret': 'webhook-test',
            'warehouse_id': cls.warehouse.id,
            'pricelist_id': cls.pricelist.id,
            'shipping_product_id': cls.shipping.id,
            'discount_product_id': cls.discount.id,
            'sync_direction_products': 'both',
            'auto_create_products': True,
            'strict_total_validation': True,
        })
        cls.connector = cls.env['woo.connector']._get_connector(cls.config)

    def _order_payload(self, **overrides):
        payload = {
            'id': 99001,
            'number': '99001',
            'status': 'processing',
            'currency': self.pricelist.currency_id.name,
            'date_created': '2026-07-31T12:00:00+02:00',
            'customer_id': 7001,
            'billing': {
                'email': 'buyer@example.com',
                'first_name': 'Test',
                'last_name': 'Buyer',
                'country': 'EG',
            },
            'shipping': {},
            'line_items': [{
                'id': 1,
                'product_id': 501,
                'name': self.product.name,
                'sku': self.product.barcode,
                'quantity': 2,
                'subtotal': '200.00',
                'total': '190.00',
            }],
            'shipping_total': '20.00',
            'discount_total': '10.00',
            'total': '210.00',
            'payment_method': 'cod',
        }
        payload.update(overrides)
        return payload

    def test_customer_sync_is_idempotent_per_store(self):
        data = {
            'id': 7001,
            'email': 'sync.customer@example.com',
            'first_name': 'Sync',
            'last_name': 'Customer',
            'billing': {'country': 'EG'},
        }
        first = self.connector._match_or_create_partner(data)
        second = self.connector._match_or_create_partner(data)
        self.assertEqual(first, second)
        binding = self.env['woo.partner.binding'].search([
            ('config_id', '=', self.config.id), ('woo_customer_id', '=', 7001),
        ])
        self.assertEqual(len(binding), 1)
        self.assertEqual(binding.partner_id, first)

    def test_order_totals_shipping_discount_and_idempotency(self):
        order = self.connector._import_order_from_webhook(self._order_payload())
        self.assertEqual(order.state, 'draft')
        self.assertEqual(order.currency_id, self.pricelist.currency_id)
        self.assertEqual(str(order.date_order), '2026-07-31 10:00:00')
        self.assertAlmostEqual(order.amount_total, 210.0, places=2)
        self.assertIn(self.shipping, order.order_line.product_id)
        self.assertIn(self.discount, order.order_line.product_id)
        self.assertEqual(
            self.connector._import_order_from_webhook(self._order_payload()), order)
        self.assertEqual(self.env['sale.order'].search_count([
            ('woo_order_id', '=', 99001), ('woo_config_id', '=', self.config.id),
        ]), 1)

    def test_order_line_uses_variant_binding_before_conflicting_barcode_holder(self):
        canonical = self.product
        canonical.write({
            'barcode': 'FD-AUTO-CANONICAL',
            'default_code': 'STRICT-WOO-SKU',
        })
        conflicting_holder = self.env['product.product'].create({
            'name': 'Conflicting SaaS Variant',
            'barcode': 'STRICT-WOO-SKU',
            'taxes_id': False,
        })
        template_binding = self.env['woo.product.binding'].create({
            'config_id': self.config.id,
            'product_tmpl_id': canonical.product_tmpl_id.id,
            'product_id': canonical.id,
            'woo_product_id': 88001,
        })
        self.env['woo.variant.binding'].create({
            'config_id': self.config.id,
            'template_binding_id': template_binding.id,
            'product_id': canonical.id,
            'woo_product_id': 88001,
            'woo_variant_id': 88101,
        })

        payload = self._order_payload(
            id=99011,
            line_items=[{
                'id': 11,
                'product_id': 88001,
                'variation_id': 88101,
                'name': canonical.name,
                'sku': 'STRICT-WOO-SKU',
                'quantity': 2,
                'subtotal': '200.00',
                'total': '190.00',
            }],
        )
        order = self.connector._import_order_from_webhook(payload)

        product_line = order.order_line.filtered(
            lambda line: line.product_id not in (self.shipping, self.discount)
        )
        self.assertEqual(product_line.product_id, canonical)
        self.assertNotEqual(product_line.product_id, conflicting_holder)

    def test_order_review_payment_and_refund_fields_are_updated(self):
        order = self.connector._import_order_from_webhook(self._order_payload())
        self.assertEqual(order.woo_review_status, 'ready')
        self.assertFalse(order.woo_date_paid)

        updated = self._order_payload(
            status='refunded', shipping_total='25.00', discount_total='15.00',
            total='210.00', date_paid='2026-07-31T12:30:00+02:00',
            refunds=[{'id': 10, 'total': '-5.00'}],
            line_items=[{
                'id': 1, 'product_id': 501, 'name': self.product.name,
                'sku': self.product.barcode, 'quantity': 2,
                'subtotal': '200.00', 'total': '185.00',
            }],
        )
        order = self.connector._import_order_from_webhook(updated)
        self.assertEqual(order.woo_review_status, 'blocked')
        self.assertEqual(str(order.woo_date_paid), '2026-07-31 10:30:00')
        self.assertAlmostEqual(order.woo_refund_total, 5.0)
        self.assertAlmostEqual(order.woo_shipping_total, 25.0)
        self.assertAlmostEqual(order.woo_discount_total, 15.0)
        with self.assertRaises(UserError):
            order.action_confirm()

    def test_new_cancelled_order_is_cancelled_in_odoo(self):
        order = self.connector._import_order_from_webhook(
            self._order_payload(id=99005, status='cancelled'))
        self.assertEqual(order.state, 'cancel')
        self.assertEqual(order.woo_review_status, 'blocked')

    def test_full_import_queue_is_deduplicated(self):
        self.config.action_pull_products()
        self.config.action_pull_products()
        self.assertEqual(self.env['woo.sync.queue'].search_count([
            ('config_id', '=', self.config.id),
            ('operation', '=', 'import_products'),
            ('state', 'in', ['pending', 'processing']),
        ]), 1)

    def test_order_poll_uses_watermark_and_returns_truthful_stats(self):
        payload = self._order_payload(id=99006)
        with patch.object(type(self.connector), '_get_all_pages', return_value=iter([payload])):
            first = self.connector._poll_recent_orders(return_stats=True)
        self.assertEqual(first, {'created': 1, 'updated': 0, 'skipped': 0, 'failed': 0})
        self.assertTrue(self.config.last_order_poll)

        payload['payment_method_title'] = 'Cash on Delivery'
        with patch.object(type(self.connector), '_get_all_pages', return_value=iter([payload])):
            second = self.connector._poll_recent_orders(return_stats=True)
        self.assertEqual(second, {'created': 0, 'updated': 1, 'skipped': 0, 'failed': 0})

    def test_order_currency_mismatch_is_blocked(self):
        with self.assertRaises(ValidationError):
            self.connector._import_order_from_webhook(
                self._order_payload(id=99002, currency='ZZZ'))

    def test_order_total_mismatch_is_blocked(self):
        with self.assertRaises(ValidationError):
            self.connector._import_order_from_webhook(
                self._order_payload(id=99003, total='999.00'))

    def test_product_direction_is_enforced(self):
        self.config.sync_direction_products = 'woo_to_odoo'
        with self.assertRaises(UserError):
            self.connector._sync_product_to_woo(self.product.product_tmpl_id.id)
        self.config.sync_direction_products = 'odoo_to_woo'
        with self.assertRaises(UserError):
            self.connector._import_product_from_woo({'id': 501, 'name': 'Remote'})

    def test_product_export_creates_store_binding(self):
        self.config.sync_direction_products = 'odoo_to_woo'
        with patch.object(type(self.connector), '_request', return_value={'id': 501}):
            woo_id = self.connector._sync_product_to_woo(
                self.product.product_tmpl_id.id)
        self.assertEqual(woo_id, 501)
        binding = self.env['woo.product.binding'].search([
            ('config_id', '=', self.config.id),
            ('product_tmpl_id', '=', self.product.product_tmpl_id.id),
        ])
        self.assertEqual(binding.woo_product_id, 501)

    def test_product_export_requires_manual_selection(self):
        product = self.env['product.template'].create({
            'name': 'Private Odoo Product',
            'sync_to_woo': False,
        })
        with self.assertRaises(UserError):
            self.connector._sync_product_to_woo(product.id)

    def test_product_import_uses_account_category_tags_images_and_dimensions(self):
        self.config.sync_direction_products = 'woo_to_odoo'
        accounting_category = self.env['product.category'].create({
            'name': 'Woo Accounting Category',
        })
        self.config.default_category_id = accounting_category
        image_response = MagicMock()
        image_response.content = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC'
            'AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
        image_response.headers = {'Content-Type': 'image/png'}
        image_response.raise_for_status.return_value = None
        remote = {
            'id': 503,
            'name': 'Catalog Product',
            'sku': 'CATALOG-503',
            'type': 'simple',
            'status': 'publish',
            'price': '12.500',
            'description': '<p>Full description</p>',
            'short_description': 'Short description',
            'weight': '1.25',
            'dimensions': {'length': '10', 'width': '5', 'height': '2'},
            'categories': [{'id': 71, 'name': 'Dresses', 'slug': 'dresses'}],
            'images': [{'id': 901, 'name': 'Front', 'src': 'https://img.example/front.png'}],
        }
        with patch.object(self.connector.session, 'get', return_value=image_response):
            template = self.connector._import_product_from_woo(remote)
            repeated = self.connector._import_product_from_woo(remote)

        self.assertEqual(repeated, template)
        self.assertEqual(template.categ_id, accounting_category)
        self.assertFalse(template.sync_to_woo)
        self.assertEqual(template.product_variant_ids.barcode, 'CATALOG-503')
        self.assertEqual(template.product_tag_ids.mapped('name'), ['Dresses'])
        self.assertTrue(template.image_1920)
        self.assertEqual(len(template.woo_image_ids), 1)
        self.assertEqual(template.woo_length, 10.0)
        self.assertEqual(template.woo_width, 5.0)
        self.assertEqual(template.woo_height, 2.0)

        second_remote = dict(remote, id=506, name='Second Catalog Product', sku='CATALOG-506')
        with patch.object(self.connector.session, 'get', return_value=image_response):
            second_template = self.connector._import_product_from_woo(second_remote)
        self.assertNotEqual(second_template, template)
        self.assertEqual(len(second_template.woo_image_ids), 1)
        self.assertEqual(self.env['woo.product.image'].search_count([
            ('config_id', '=', self.config.id), ('woo_image_id', '=', 901),
        ]), 2)

    def test_barcode_match_prevents_duplicate_and_conflicting_woo_identity(self):
        self.config.sync_direction_products = 'woo_to_odoo'
        product = self.env['product.product'].create({
            'name': 'Existing Barcode Product',
            'barcode': 'UNIQUE-BARCODE-504',
        })
        first = self.connector._import_product_from_woo({
            'id': 504,
            'name': 'Remote Renamed Product',
            'sku': 'UNIQUE-BARCODE-504',
            'type': 'simple',
            'status': 'publish',
        })
        self.assertEqual(first, product.product_tmpl_id)
        with self.assertRaises(ValidationError):
            self.connector._import_product_from_woo({
                'id': 505,
                'name': 'Duplicate Remote Product',
                'sku': 'UNIQUE-BARCODE-504',
                'type': 'simple',
                'status': 'publish',
            })
        self.assertEqual(self.env['product.product'].search_count([
            ('barcode', '=', 'UNIQUE-BARCODE-504'),
        ]), 1)

    def test_product_import_does_not_modify_stock_quant(self):
        self.config.sync_direction_products = 'woo_to_odoo'
        before = self.product.with_context(
            location=self.warehouse.lot_stock_id.id).qty_available
        result = self.connector._import_product_from_woo({
            'id': 502,
            'name': self.product.name,
            'sku': self.product.barcode,
            'stock_quantity': 999,
            'status': 'publish',
        })
        after = self.product.with_context(
            location=self.warehouse.lot_stock_id.id).qty_available
        self.assertEqual(result, self.product.product_tmpl_id)
        self.assertEqual(after, before)

    def test_variable_product_import_maps_native_variants(self):
        self.config.sync_direction_products = 'woo_to_odoo'
        remote = {
            'id': 601,
            'name': 'Variable Uniform',
            'type': 'variable',
            'status': 'publish',
            'attributes': [
                {'name': 'Color', 'variation': True, 'options': ['Blue', 'Red']},
                {'name': 'Size', 'variation': True, 'options': ['S', 'M']},
            ],
        }
        variations = [
            {'id': 6101, 'sku': 'VU-B-S', 'attributes': [
                {'name': 'Color', 'option': 'Blue'}, {'name': 'Size', 'option': 'S'}]},
            {'id': 6102, 'sku': 'VU-B-M', 'attributes': [
                {'name': 'Color', 'option': 'Blue'}, {'name': 'Size', 'option': 'M'}]},
            {'id': 6103, 'sku': 'VU-R-S', 'attributes': [
                {'name': 'Color', 'option': 'Red'}, {'name': 'Size', 'option': 'S'}]},
            {'id': 6104, 'sku': 'VU-R-M', 'attributes': [
                {'name': 'Color', 'option': 'Red'}, {'name': 'Size', 'option': 'M'}]},
        ]
        with patch.object(type(self.connector), '_get_all_pages', return_value=iter(variations)):
            template = self.connector._import_product_from_woo(remote)
        template.taxes_id = False
        self.assertEqual(len(template.product_variant_ids), 4)
        bindings = self.env['woo.variant.binding'].search([
            ('config_id', '=', self.config.id), ('woo_product_id', '=', 601),
        ])
        self.assertEqual(len(bindings), 4)
        self.assertEqual(set(bindings.mapped('woo_variant_id')), {6101, 6102, 6103, 6104})

        payload = self._order_payload(
            id=99004,
            discount_total='0',
            shipping_total='0',
            total='25.00',
            line_items=[{
                'id': 2,
                'product_id': 601,
                'variation_id': 6104,
                'name': 'Variable Uniform - Red, M',
                'sku': '',
                'quantity': 1,
                'subtotal': '25.00',
                'total': '25.00',
            }],
        )
        order = self.connector._import_order_from_webhook(payload)
        self.assertEqual(order.order_line.product_id, bindings.filtered(
            lambda binding: binding.woo_variant_id == 6104).product_id)

    def test_variable_product_reimport_uses_existing_binding_with_local_extra_axis(self):
        """An established Woo variation binding remains authoritative after Odoo adds a local axis."""
        self.config.sync_direction_products = 'woo_to_odoo'
        remote = {
            'id': 603,
            'name': 'Variable Uniform With Legacy Axis',
            'type': 'variable',
            'status': 'publish',
            'attributes': [
                {'name': 'Color', 'variation': True, 'options': ['Blue', 'Red']},
                {'name': 'Size', 'variation': True, 'options': ['S', 'M']},
            ],
        }
        variations = [
            {'id': 6301, 'sku': 'VULA-B-S', 'attributes': [
                {'name': 'Color', 'option': 'Blue'}, {'name': 'Size', 'option': 'S'}]},
            {'id': 6302, 'sku': 'VULA-B-M', 'attributes': [
                {'name': 'Color', 'option': 'Blue'}, {'name': 'Size', 'option': 'M'}]},
            {'id': 6303, 'sku': 'VULA-R-S', 'attributes': [
                {'name': 'Color', 'option': 'Red'}, {'name': 'Size', 'option': 'S'}]},
            {'id': 6304, 'sku': 'VULA-R-M', 'attributes': [
                {'name': 'Color', 'option': 'Red'}, {'name': 'Size', 'option': 'M'}]},
        ]
        with patch.object(type(self.connector), '_get_all_pages', return_value=iter(variations)):
            template = self.connector._import_product_from_woo(remote)
        bindings = self.env['woo.variant.binding'].search([
            ('config_id', '=', self.config.id), ('woo_product_id', '=', 603),
        ])
        original_mapping = {binding.woo_variant_id: binding.product_id for binding in bindings}

        legacy_attribute = self.env['product.attribute'].create({
            'name': 'Legacy Fabric', 'create_variant': 'always',
        })
        legacy_values = self.env['product.attribute.value'].create([
            {'attribute_id': legacy_attribute.id, 'name': 'Legacy A'},
            {'attribute_id': legacy_attribute.id, 'name': 'Legacy B'},
        ])
        template.write({'attribute_line_ids': [Command.create({
            'attribute_id': legacy_attribute.id,
            'value_ids': [Command.set(legacy_values.ids)],
        })]})

        with patch.object(type(self.connector), '_get_all_pages', return_value=iter(variations)):
            result = self.connector._import_product_from_woo(remote)

        self.assertEqual(result, template)
        self.assertEqual(
            {binding.woo_variant_id: binding.product_id for binding in bindings},
            original_mapping,
        )

    def test_variable_product_import_uses_unique_barcode_with_local_extra_axis(self):
        """A unique variant SKU may map by barcode, but only inside its linked template."""
        self.config.sync_direction_products = 'woo_to_odoo'
        color = self.env['product.attribute'].create({
            'name': 'Woo Fallback Color', 'create_variant': 'always',
        })
        size = self.env['product.attribute'].create({
            'name': 'Woo Fallback Size', 'create_variant': 'always',
        })
        legacy = self.env['product.attribute'].create({
            'name': 'Woo Fallback Legacy', 'create_variant': 'always',
        })
        values = {}
        for attribute, options in [
                (color, ['Blue', 'Red']), (size, ['S', 'M']),
                (legacy, ['Legacy A', 'Legacy B'])]:
            values[attribute.id] = self.env['product.attribute.value'].create([
                {'attribute_id': attribute.id, 'name': option} for option in options
            ])
        template = self.env['product.template'].create({
            'name': 'Variable Uniform Barcode Fallback',
            'type': 'consu',
            'company_id': self.company.id,
            'taxes_id': False,
            'attribute_line_ids': [Command.create({
                'attribute_id': attribute.id,
                'value_ids': [Command.set(values[attribute.id].ids)],
            }) for attribute in (color, size, legacy)],
        })
        product_binding = self.env['woo.product.binding'].create({
            'config_id': self.config.id,
            'product_tmpl_id': template.id,
            'product_id': template.product_variant_ids[:1].id,
            'woo_product_id': 604,
        })
        remote = {
            'id': 604,
            'name': template.name,
            'type': 'variable',
            'status': 'publish',
            'attributes': [
                {'name': 'Woo Fallback Color', 'variation': True, 'options': ['Blue', 'Red']},
                {'name': 'Woo Fallback Size', 'variation': True, 'options': ['S', 'M']},
            ],
        }
        variations = []
        for color_name, size_name, sku, remote_id in [
                ('Blue', 'S', 'WFB-B-S', 6401), ('Blue', 'M', 'WFB-B-M', 6402),
                ('Red', 'S', 'WFB-R-S', 6403), ('Red', 'M', 'WFB-R-M', 6404)]:
            variant = template.product_variant_ids.filtered(
                lambda item: color_name in item.product_template_attribute_value_ids.mapped('name')
                and size_name in item.product_template_attribute_value_ids.mapped('name')
                and 'Legacy A' in item.product_template_attribute_value_ids.mapped('name'))
            self.assertEqual(len(variant), 1)
            variant.barcode = sku
            variations.append({
                'id': remote_id,
                'sku': sku,
                'attributes': [
                    {'name': 'Woo Fallback Color', 'option': color_name},
                    {'name': 'Woo Fallback Size', 'option': size_name},
                ],
            })

        with patch.object(type(self.connector), '_get_all_pages', return_value=iter(variations)):
            result = self.connector._import_product_from_woo(remote)

        self.assertEqual(result, template)
        self.assertEqual(product_binding.woo_product_id, 604)
        variant_bindings = self.env['woo.variant.binding'].search([
            ('config_id', '=', self.config.id), ('woo_product_id', '=', 604),
        ])
        self.assertEqual(len(variant_bindings), 4)
        self.assertEqual(
            set(variant_bindings.mapped('product_id.barcode')),
            {'WFB-B-S', 'WFB-B-M', 'WFB-R-S', 'WFB-R-M'},
        )

    def test_variable_import_blocks_sku_collision_instead_of_using_internal_reference(self):
        self.config.sync_direction_products = 'woo_to_odoo'
        canonical_template = self.env['product.template'].create({
            'name': 'Bound Variable Collision Product',
            'taxes_id': False,
        })
        self.env['woo.product.binding'].create({
            'config_id': self.config.id,
            'product_tmpl_id': canonical_template.id,
            'product_id': canonical_template.product_variant_id.id,
            'woo_product_id': 603,
        })
        self.env['product.product'].create({
            'name': 'Existing SKU Holder',
            'barcode': 'STRICT-COLLISION-SKU',
        })
        remote = {
            'id': 603,
            'name': 'Variable Collision Product',
            'type': 'variable',
            'status': 'publish',
            'attributes': [
                {'name': 'Size', 'variation': True, 'options': ['S']},
            ],
        }
        variations = [{
            'id': 6301,
            'sku': 'STRICT-COLLISION-SKU',
            'attributes': [{'name': 'Size', 'option': 'S'}],
        }]

        with patch.object(type(self.connector), '_get_all_pages', return_value=iter(variations)):
            with self.assertRaises(ValidationError):
                self.connector._import_product_from_woo(remote)

    def test_variable_reimport_pushes_local_barcode_when_woo_sku_is_empty(self):
        self.config.sync_direction_products = 'woo_to_odoo'
        remote = {
            'id': 604,
            'name': 'Variable Without Remote SKU',
            'type': 'variable',
            'status': 'publish',
            'attributes': [
                {'name': 'Size', 'variation': True, 'options': ['S']},
            ],
        }
        initial_variations = [{
            'id': 6401,
            'sku': 'INITIAL-REMOTE-SKU',
            'attributes': [{'name': 'Size', 'option': 'S'}],
        }]
        with patch.object(
                type(self.connector), '_get_all_pages', return_value=iter(initial_variations)):
            self.connector._import_product_from_woo(remote)

        binding = self.env['woo.variant.binding'].search([
            ('config_id', '=', self.config.id), ('woo_variant_id', '=', 6401),
        ])
        binding.product_id.barcode = 'FD-AUTO-LOCAL'
        empty_sku_variations = [{
            'id': 6401,
            'sku': '',
            'attributes': [{'name': 'Size', 'option': 'S'}],
        }]
        with patch.object(
                type(self.connector), '_get_all_pages', return_value=iter(empty_sku_variations)):
            with patch.object(type(self.connector), '_push_sku_to_woo') as push_sku:
                self.connector._import_product_from_woo(remote)

        # The website variation has no SKU → the linked Odoo barcode must be
        # mirrored back as its SKU (and nothing else is touched).
        push_sku.assert_called_once()
        args = push_sku.call_args.args
        self.assertEqual(args[0], 'products/604/variations/6401')
        self.assertEqual(args[1], 'FD-AUTO-LOCAL')

    def test_simple_product_pushes_local_barcode_when_woo_sku_is_empty(self):
        self.config.sync_direction_products = 'woo_to_odoo'
        remote = {
            'id': 605,
            'name': 'Simple Without Remote SKU',
            'type': 'simple',
            'status': 'publish',
        }
        initial = {
            'id': 605,
            'name': 'Simple Without Remote SKU',
            'type': 'simple',
            'sku': 'INITIAL-SIMPLE-SKU',
            'status': 'publish',
        }
        with patch.object(type(self.connector), '_get_all_pages', return_value=iter([])):
            template = self.connector._import_product_from_woo(initial)

        template.product_variant_ids[:1].barcode = 'FD-SIMPLE-LOCAL'
        empty_sku = {
            'id': 605,
            'name': 'Simple Without Remote SKU',
            'type': 'simple',
            'sku': '',
            'status': 'publish',
        }
        with patch.object(type(self.connector), '_get_all_pages', return_value=iter([])):
            with patch.object(type(self.connector), '_push_sku_to_woo') as push_sku:
                self.connector._import_product_from_woo(empty_sku)

        push_sku.assert_called_once()
        args = push_sku.call_args.args
        self.assertEqual(args[0], 'products/605')
        self.assertEqual(args[1], 'FD-SIMPLE-LOCAL')

    def test_sync_variable_product_exports_all_variations(self):
        self.config.sync_direction_products = 'both'
        attr = self.env['product.attribute'].create({
            'name': 'Test Var Size', 'create_variant': 'always'})
        v_s = self.env['product.attribute.value'].create(
            {'name': 'S', 'attribute_id': attr.id})
        v_m = self.env['product.attribute.value'].create(
            {'name': 'M', 'attribute_id': attr.id})
        template = self.env['product.template'].create({
            'name': 'Var Export Test',
            'type': 'consu',
            'list_price': 50.0,
            'sync_to_woo': True,
            'attribute_line_ids': [(0, 0, {
                'attribute_id': attr.id,
                'value_ids': [(6, 0, [v_s.id, v_m.id])],
            })],
        })
        variants = template.product_variant_ids
        variants[0].barcode = 'FD-VAR-1'
        variants[1].barcode = 'FD-VAR-2'
        # Give the first variant its own image → must be pushed on its variation
        variants[0].image_variant_1920 = (
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=')

        calls = []
        variation_counter = {'n': 7100}

        def fake_request(method, endpoint, data=None, params=None, timeout=None):
            calls.append((method, endpoint, data))
            if method == 'POST' and endpoint == 'products':
                return {'id': 701}
            if endpoint.startswith('products/701/variations'):
                # WooCommerce returns a unique id per created variation
                variation_counter['n'] += 1
                return {'id': variation_counter['n']}
            return {'id': 701}

        with patch.object(type(self.connector), '_request', side_effect=fake_request):
            woo_id = self.connector._sync_product_to_woo(template.id)

        self.assertEqual(woo_id, 701)
        posts = [c for c in calls if c[0] == 'POST']
        # One POST for the parent + one POST per variation
        self.assertEqual(len(posts), 3)
        variation_posts = [c for c in posts if 'variations' in c[1]]
        self.assertEqual(len(variation_posts), 2)
        skus = sorted(c[2]['sku'] for c in variation_posts)
        self.assertEqual(skus, ['FD-VAR-1', 'FD-VAR-2'])

        # Variant 1 has its own image → included; variant 2 has none → omitted
        by_sku = {c[2]['sku']: c[2] for c in variation_posts}
        self.assertIn('image', by_sku['FD-VAR-1'])
        self.assertIn('image_1920', by_sku['FD-VAR-1']['image']['src'])
        self.assertNotIn('image', by_sku['FD-VAR-2'])

        # Cleanup
        template.unlink()
        attr.unlink()

    def test_import_uses_website_pricelist_without_overwrite(self):
        website_pl = self.env['product.pricelist'].create({
            'name': 'Website Pricelist Test',
            'currency_id': self.company.currency_id.id,
        })
        self.config.website_pricelist_id = website_pl
        self.config.sync_direction_products = 'woo_to_odoo'

        remote = {
            'id': 606,
            'name': 'Pricelist Simple Product',
            'type': 'simple',
            'sku': 'PL-SKU-1',
            'regular_price': '25.0',
            'status': 'publish',
        }
        with patch.object(type(self.connector), '_get_all_pages', return_value=iter([])):
            template = self.connector._import_product_from_woo(remote)

        variant = template.product_variant_ids[:1]
        # Price went to the dedicated website pricelist, NOT to the card price
        # (the card keeps its default/own price — never the imported one)
        self.assertNotEqual(template.list_price, 25.0)
        item = self.env['product.pricelist.item'].search([
            ('pricelist_id', '=', website_pl.id),
            ('product_id', '=', variant.id),
        ], limit=1)
        self.assertTrue(item)
        self.assertEqual(float(item.fixed_price), 25.0)

        # Re-import with a different price → existing website price is NOT overwritten
        remote2 = dict(remote, regular_price='99.0')
        with patch.object(type(self.connector), '_get_all_pages', return_value=iter([])):
            self.connector._import_product_from_woo(remote2)
        item.invalidate_recordset()
        self.assertEqual(float(item.fixed_price), 25.0)

        template.unlink()
        website_pl.unlink()

    def test_export_uses_website_pricelist_and_no_overwrite(self):
        self.config.sync_direction_products = 'both'
        template = self.env['product.template'].create({
            'name': 'Export Price Test', 'type': 'consu', 'list_price': 10.0,
            'sync_to_woo': True,
        })
        variant = template.product_variant_ids[:1]
        self.env['woo.product.binding'].create({
            'config_id': self.config.id,
            'product_tmpl_id': template.id,
            'product_id': variant.id,
            'woo_product_id': 707,
        })

        # Existing Woo product with NO website price → no price sent (no overwrite)
        calls = []
        with patch.object(type(self.connector), '_request',
                          side_effect=lambda m, e, data=None, **kw:
                          calls.append((m, e, data)) or {'id': 707}):
            self.connector._sync_product_to_woo(template.id)
        put = [c for c in calls if c[0] == 'PUT'][0]
        self.assertNotIn('regular_price', put[2])

        # New product with a website price → price sent from the website pricelist
        website_pl = self.env['product.pricelist'].create({
            'name': 'Website Pricelist Test 2',
            'currency_id': self.company.currency_id.id,
        })
        self.config.website_pricelist_id = website_pl
        template2 = self.env['product.template'].create({
            'name': 'Export Price New', 'type': 'consu', 'list_price': 10.0,
            'sync_to_woo': True,
        })
        self.connector._set_website_price(template2.product_variant_ids[:1], 33.0)
        calls2 = []
        with patch.object(type(self.connector), '_request',
                          side_effect=lambda m, e, data=None, **kw:
                          calls2.append((m, e, data)) or {'id': 708}):
            self.connector._sync_product_to_woo(template2.id)
        post = [c for c in calls2 if c[0] == 'POST' and c[1] == 'products'][0]
        self.assertEqual(post[2]['regular_price'], '33.0')

        template.unlink()
        template2.unlink()
        website_pl.unlink()

    def test_variable_product_reimport_preserves_local_attribute_values(self):
        self.config.sync_direction_products = 'woo_to_odoo'
        remote = {
            'id': 602,
            'name': 'Variable Uniform With Local Size',
            'type': 'variable',
            'status': 'publish',
            'attributes': [
                {'name': 'Size', 'variation': True, 'options': ['S', 'M']},
            ],
        }
        variations = [
            {'id': 6201, 'sku': 'VUL-S', 'attributes': [
                {'name': 'Size', 'option': 'S'}]},
            {'id': 6202, 'sku': 'VUL-M', 'attributes': [
                {'name': 'Size', 'option': 'M'}]},
        ]
        with patch.object(type(self.connector), '_get_all_pages', return_value=iter(variations)):
            template = self.connector._import_product_from_woo(remote)

        size_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.name.casefold() == 'size')
        local_value = self.env['product.attribute.value'].create({
            'attribute_id': size_line.attribute_id.id,
            'name': 'XL - SaaS Only',
        })
        size_line.write({'value_ids': [Command.link(local_value.id)]})
        local_variant = template.with_context(active_test=False).product_variant_ids.filtered(
            lambda variant: local_value in variant.product_template_attribute_value_ids.product_attribute_value_id)
        self.assertTrue(local_variant)
        local_variant.active = True

        with patch.object(type(self.connector), '_get_all_pages', return_value=iter(variations)):
            self.connector._import_product_from_woo(remote)

        size_line = template.attribute_line_ids.filtered(
            lambda line: line.attribute_id.name.casefold() == 'size')
        self.assertIn(local_value, size_line.value_ids)
        self.assertTrue(local_variant.active)
        self.assertIn(local_variant, template.product_variant_ids)
        bindings = self.env['woo.variant.binding'].search([
            ('config_id', '=', self.config.id), ('woo_product_id', '=', 602),
        ])
        self.assertEqual(set(bindings.mapped('woo_variant_id')), {6201, 6202})


@tagged('post_install', '-at_install', 'woo_webhook_http')
class TestWooWebhookHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.secret = 'http-webhook-secret'
        cls.config = cls.env['woo.config'].create({
            'name': 'HTTP Webhook Store',
            'website_url': 'https://http-webhook.example.com',
            'consumer_key': 'ck_http',
            'consumer_secret': 'cs_http',
            'webhook_secret': cls.secret,
            'warehouse_id': cls.env.ref('stock.warehouse0').id,
        })

    def _headers(self, body, delivery_id, signature=None):
        signature = signature or base64.b64encode(hmac.new(
            self.secret.encode(), body, hashlib.sha256).digest()).decode()
        return {
            'Content-Type': 'application/json',
            'X-WC-Webhook-Topic': 'customer.updated',
            'X-WC-Webhook-ID': delivery_id,
            'X-WC-Webhook-Signature': signature,
        }

    def test_signed_webhook_returns_202_and_is_idempotent(self):
        body = json.dumps({'id': 88001, 'email': 'http@example.com'}).encode()
        url = f'/woo/webhook/{self.config.id}'
        response = self.url_open(
            url, data=body, headers=self._headers(body, 'delivery-88001'), method='POST')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'success')
        duplicate = self.url_open(
            url, data=body, headers=self._headers(body, 'delivery-88001'), method='POST')
        self.assertEqual(duplicate.status_code, 202)
        self.assertEqual(self.env['woo.sync.queue'].search_count([
            ('config_id', '=', self.config.id),
            ('webhook_delivery_id', '=', 'delivery-88001'),
        ]), 1)

    def test_invalid_signature_returns_401(self):
        body = json.dumps({'id': 88002}).encode()
        response = self.url_open(
            f'/woo/webhook/{self.config.id}', data=body,
            headers=self._headers(body, 'delivery-88002', signature='invalid'),
            method='POST')
        self.assertEqual(response.status_code, 401)
