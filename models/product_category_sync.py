import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class ProductCategorySync(models.Model):
    _name = 'wc.product.category.sync'
    _description = 'Sincronización de categorías WooCommerce'

    name = fields.Char(string='Nombre', default='Sync categorías WooCommerce')

    def action_sync_categories_from_wc(self):
        """Importa categorías de WooCommerce y las crea en Odoo."""
        backend = self.env['wc.backend'].search([], limit=1)
        if not backend:
            return
        categories = backend._wc_get('products/categories', params={'per_page': 100})
        categ_model = self.env['product.category']
        for wc_cat in categories:
            if not categ_model.search([('name', '=', wc_cat.get('name'))], limit=1):
                categ_model.create({'name': wc_cat.get('name')})
