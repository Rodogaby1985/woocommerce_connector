import logging
from typing import Any

from odoo import fields, models

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = ['product.template', 'wc.sync.mixin']

    wc_id = fields.Integer(string='ID WooCommerce', index=True)
    wc_sale_price = fields.Float(string='Precio oferta WooCommerce')
    wc_sale_date_from = fields.Datetime(string='Oferta desde')
    wc_sale_date_to = fields.Datetime(string='Oferta hasta')
    wc_sync_date = fields.Datetime(string='Última sync WooCommerce')
    wc_product_type = fields.Selection([
        ('simple', 'Simple'),
        ('variable', 'Variable'),
    ], string='Tipo WooCommerce', default='simple')
    wc_synced = fields.Boolean(string='Sincronizado con WooCommerce', compute='_compute_wc_synced')

    def _compute_wc_synced(self):
        for product in self:
            product.wc_synced = bool(product.wc_id)

    def _prepare_wc_data(self) -> dict[str, Any]:
        """Prepara payload de producto para WooCommerce."""
        self.ensure_one()
        categories = []
        if self.categ_id:
            categories = [{'name': self.categ_id.name}]

        data = {
            'name': self.name,
            'sku': self.default_code or '',
            'regular_price': str(self.list_price or 0.0),
            'sale_price': str(self.wc_sale_price) if self.wc_sale_price else '',
            'date_on_sale_from': self.wc_sale_date_from.isoformat() if self.wc_sale_date_from else None,
            'date_on_sale_to': self.wc_sale_date_to.isoformat() if self.wc_sale_date_to else None,
            'description': self.description_sale or self.description or '',
            'stock_quantity': int(self.qty_available),
            'manage_stock': True,
            'categories': categories,
            'type': self.wc_product_type or ('variable' if self.product_variant_count > 1 else 'simple'),
        }
        if data['type'] == 'variable':
            attributes = []
            for line in self.attribute_line_ids:
                attributes.append({'name': line.attribute_id.name, 'visible': True, 'variation': True})
            data['attributes'] = attributes
        return data

    def _process_wc_data(self, wc_data: dict[str, Any]):
        """Procesa datos de WooCommerce y actualiza producto en Odoo."""
        vals = {
            'name': wc_data.get('name'),
            'default_code': wc_data.get('sku') or False,
            'list_price': float(wc_data.get('regular_price') or 0.0),
            'wc_sale_price': float(wc_data.get('sale_price') or 0.0),
            'wc_id': wc_data.get('id') or self.wc_id,
            'wc_product_type': wc_data.get('type') if wc_data.get('type') in ('simple', 'variable') else 'simple',
            'wc_sync_date': fields.Datetime.now(),
        }
        self.with_context(wc_no_sync=True).write({k: v for k, v in vals.items() if v is not None})

    def _sync_variable_product_to_wc(self):
        """Sincroniza producto variable y luego sus variantes."""
        self.ensure_one()
        self.action_sync_to_wc()
        for variant in self.product_variant_ids:
            variant.action_sync_to_wc()

    def _sync_variable_product_from_wc(self, wc_product: dict[str, Any]):
        """Importa producto variable y sus variaciones."""
        self.ensure_one()
        self._process_wc_data(wc_product)
        backend = self._get_wc_backend()
        if not backend or not self.wc_id:
            return
        variations = backend._wc_get(f'products/{self.wc_id}/variations', params={'per_page': 100})
        for variation in variations:
            variant = self.product_variant_ids.filtered(lambda v: v.wc_variation_id == variation.get('id'))[:1]
            if variant:
                variant._process_wc_variation_data(variation)

    def action_sync_to_wc(self):
        """Sincroniza manualmente producto hacia WooCommerce."""
        backend = self._get_wc_backend()
        if not backend:
            return
        for product in self:
            data = product._prepare_wc_data()
            if product.wc_id:
                response = backend._wc_put(f'products/{product.wc_id}', data)
            else:
                response = backend._wc_post('products', data)
            product.with_context(wc_no_sync=True).write({
                'wc_id': response.get('id') or product.wc_id,
                'wc_sync_date': fields.Datetime.now(),
            })

    def action_sync_from_wc(self):
        """Trae datos del producto desde WooCommerce."""
        backend = self._get_wc_backend()
        if not backend:
            return
        for product in self:
            if not product.wc_id:
                continue
            wc_product = backend._wc_get(f'products/{product.wc_id}')
            if wc_product.get('type') == 'variable':
                product._sync_variable_product_from_wc(wc_product)
            else:
                product._process_wc_data(wc_product)

    def write(self, vals: dict[str, Any]):
        watched_fields = ['name', 'default_code', 'list_price', 'wc_sale_price', 'wc_sale_date_from', 'wc_sale_date_to', 'categ_id']
        should_enqueue = self._wc_field_changed(vals, watched_fields) and not self._is_wc_sync_disabled()
        result = super().write(vals)
        if should_enqueue and not self._wc_recent_sync('wc_sync_date'):
            for product in self.filtered('wc_id'):
                product._enqueue_wc_job(action='export', priority=3 if any(f in vals for f in ['list_price', 'wc_sale_price']) else 5)
        return result
