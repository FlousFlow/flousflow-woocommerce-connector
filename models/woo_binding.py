from odoo import fields, models, _


class WooProductBinding(models.Model):
    _name = 'woo.product.binding'
    _description = 'WooCommerce Product Binding'
    _check_company_auto = True
    _order = 'config_id, product_tmpl_id, product_id'

    config_id = fields.Many2one(
        'woo.config', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='config_id.company_id', store=True, index=True)
    product_tmpl_id = fields.Many2one(
        'product.template', required=True, ondelete='cascade', index=True,
        check_company=True)
    product_id = fields.Many2one(
        'product.product', ondelete='set null', index=True, check_company=True,
        help=('Primary variant for simple products; variable products use variant bindings.'))
    woo_product_id = fields.Integer(required=True, index=True)
    woo_variant_id = fields.Integer(index=True)
    last_sync = fields.Datetime(readonly=True)
    sync_status = fields.Selection(
        [('synced', 'Synced'), ('error', 'Error'), ('unlinked', 'Unlinked')],
        default='synced', required=True)

    _config_template_uniq = models.Constraint(
        'unique(config_id, product_tmpl_id)',
        ('This product is already linked to this WooCommerce store.'),
    )
    _config_woo_product_uniq = models.Constraint(
        'unique(config_id, woo_product_id)',
        ('This WooCommerce product is already linked in this store.'),
    )


class WooVariantBinding(models.Model):
    _name = 'woo.variant.binding'
    _description = 'WooCommerce Product Variant Binding'
    _check_company_auto = True
    _order = 'config_id, woo_product_id, woo_variant_id'

    config_id = fields.Many2one(
        'woo.config', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='config_id.company_id', store=True, index=True)
    template_binding_id = fields.Many2one(
        'woo.product.binding', required=True, ondelete='cascade', index=True,
        check_company=True)
    product_id = fields.Many2one(
        'product.product', required=True, ondelete='cascade', index=True,
        check_company=True)
    woo_product_id = fields.Integer(required=True, index=True)
    woo_variant_id = fields.Integer(required=True, index=True)
    last_sync = fields.Datetime(readonly=True)

    _config_product_uniq = models.Constraint(
        'unique(config_id, product_id)',
        ('This Odoo variant is already linked to this WooCommerce store.'),
    )
    _config_woo_variant_uniq = models.Constraint(
        'unique(config_id, woo_variant_id)',
        ('This WooCommerce variation is already linked in this store.'),
    )


class WooPartnerBinding(models.Model):
    _name = 'woo.partner.binding'
    _description = 'WooCommerce Customer Binding'
    _check_company_auto = True
    _order = 'config_id, partner_id'

    config_id = fields.Many2one(
        'woo.config', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='config_id.company_id', store=True, index=True)
    partner_id = fields.Many2one(
        'res.partner', required=True, ondelete='cascade', index=True,
        check_company=True)
    woo_customer_id = fields.Integer(required=True, index=True)
    last_sync = fields.Datetime(readonly=True)

    _config_partner_uniq = models.Constraint(
        'unique(config_id, partner_id)',
        ('This customer is already linked to this WooCommerce store.'),
    )
    _config_woo_customer_uniq = models.Constraint(
        'unique(config_id, woo_customer_id)',
        ('This WooCommerce customer is already linked in this store.'),
    )


class WooCategoryBinding(models.Model):
    _name = 'woo.category.binding'
    _description = 'WooCommerce Product Category Binding'
    _check_company_auto = True
    _order = 'config_id, name'

    config_id = fields.Many2one(
        'woo.config', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='config_id.company_id', store=True, index=True)
    woo_category_id = fields.Integer(required=True, index=True)
    name = fields.Char(required=True, index=True)
    slug = fields.Char()
    parent_woo_category_id = fields.Integer(index=True)
    tag_id = fields.Many2one(
        'product.tag', required=True, ondelete='restrict', index=True)

    _config_woo_category_uniq = models.Constraint(
        'unique(config_id, woo_category_id)',
        ('This WooCommerce category is already linked in this store.'),
    )


class WooProductImage(models.Model):
    _name = 'woo.product.image'
    _description = 'WooCommerce Product Image'
    _check_company_auto = True
    _order = 'sequence, id'

    config_id = fields.Many2one(
        'woo.config', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='config_id.company_id', store=True, index=True)
    product_tmpl_id = fields.Many2one(
        'product.template', required=True, ondelete='cascade', index=True,
        check_company=True)
    woo_image_id = fields.Integer(index=True)
    name = fields.Char(required=True)
    source_url = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    image_1920 = fields.Image(required=True, max_width=1920, max_height=1920)

    _config_woo_image_uniq = models.Constraint(
        'unique(config_id, product_tmpl_id, woo_image_id)',
        ('This WooCommerce image is already linked to this product.'),
    )


class WooPaymentMethodMap(models.Model):
    _name = 'woo.payment.method.map'
    _description = 'WooCommerce Payment Method Mapping'
    _check_company_auto = True
    _order = 'config_id, woo_payment_method'

    config_id = fields.Many2one(
        'woo.config', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='config_id.company_id', store=True, index=True)
    woo_payment_method = fields.Char(
        string=('WooCommerce Payment Method'), required=True,
        help=('WooCommerce payment method slug, e.g. "cod", "stripe", '
               '"paypal", "bacs", "cheque".'))
    payment_term_id = fields.Many2one(
        'account.payment.term', string=('Odoo Payment Term'),
        check_company=True,
        help=('Payment term applied to imported quotations that use this '
               'WooCommerce payment method.'))

    _config_method_uniq = models.Constraint(
        'unique(config_id, woo_payment_method)',
        ('A mapping for this WooCommerce payment method already exists for '
          'this store.'),
    )
