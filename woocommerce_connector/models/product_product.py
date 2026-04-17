from typing import Any

from odoo import fields, models


class ProductProduct(models.Model):
    _inherit = ['product.product', 'wc.sync.mixin']

    wc_variation_id = fields.Integer(string='ID Variación WooCommerce', index=True)
    wc_sale_price = fields.Float(string='Precio oferta variante')
    wc_sale_date_from = fields.Datetime(string='Oferta variante desde')
    wc_sale_date_to = fields.Datetime(string='Oferta variante hasta')
    wc_variant_sync_date = fields.Datetime(string='Última sync variante')

    def _prepare_wc_variation_data(self) -> dict[str, Any]:
        """Prepara payload de variación para WooCommerce."""
        self.ensure_one()
        attrs = []
        for value in self.product_template_attribute_value_ids:
            attrs.append({'name': value.attribute_id.name, 'option': value.name})
        return {
            'sku': self.default_code or '',
            'regular_price': str(self.lst_price or 0.0),
            'sale_price': str(self.wc_sale_price) if self.wc_sale_price else '',
            'date_on_sale_from': self.wc_sale_date_from.isoformat() if self.wc_sale_date_from else None,
            'date_on_sale_to': self.wc_sale_date_to.isoformat() if self.wc_sale_date_to else None,
            'stock_quantity': int(self.qty_available),
            'manage_stock': True,
            'attributes': attrs,
        }

    def _process_wc_variation_data(self, wc_variation: dict[str, Any]):
        """Procesa variación de WooCommerce."""
        vals = {
            'default_code': wc_variation.get('sku') or False,
            'lst_price': float(wc_variation.get('regular_price') or 0.0),
            'wc_sale_price': float(wc_variation.get('sale_price') or 0.0),
            'wc_variation_id': wc_variation.get('id') or self.wc_variation_id,
            'wc_variant_sync_date': fields.Datetime.now(),
        }
        self.with_context(wc_no_sync=True).write({k: v for k, v in vals.items() if v is not None})

    def action_sync_to_wc(self):
        """Sincroniza variante a WooCommerce."""
        backend = self._get_wc_backend()
        if not backend:
            return
        for variant in self:
            template = variant.product_tmpl_id
            if not template.wc_id:
                template.action_sync_to_wc()
            payload = variant._prepare_wc_variation_data()
            if variant.wc_variation_id:
                response = backend._wc_put(f'products/{template.wc_id}/variations/{variant.wc_variation_id}', payload)
            else:
                response = backend._wc_post(f'products/{template.wc_id}/variations', payload)
            variant.with_context(wc_no_sync=True).write({
                'wc_variation_id': response.get('id') or variant.wc_variation_id,
                'wc_variant_sync_date': fields.Datetime.now(),
            })

    def action_sync_from_wc(self):
        """Trae variación desde WooCommerce."""
        backend = self._get_wc_backend()
        if not backend:
            return
        for variant in self.filtered(lambda v: v.wc_variation_id and v.product_tmpl_id.wc_id):
            response = backend._wc_get(f'products/{variant.product_tmpl_id.wc_id}/variations/{variant.wc_variation_id}')
            variant._process_wc_variation_data(response)

    def write(self, vals: dict[str, Any]):
        watched_fields = ['lst_price', 'wc_sale_price', 'qty_available', 'default_code']
        should_enqueue = self._wc_field_changed(vals, watched_fields) and not self._is_wc_sync_disabled()
        result = super().write(vals)
        if should_enqueue and not self._wc_recent_sync('wc_variant_sync_date'):
            for variant in self.filtered('wc_variation_id'):
                variant._enqueue_wc_job(action='export', priority=3 if any(f in vals for f in ['lst_price', 'wc_sale_price']) else 2)
        return result
