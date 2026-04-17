import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class ProductAttribute(models.Model):
    _inherit = 'product.attribute'

    wc_attribute_id = fields.Integer(string='ID Atributo WooCommerce', index=True)

    def _get_or_create_from_wc(self, name: str):
        """Busca o crea atributo de Odoo desde nombre de WooCommerce."""
        attribute = self.search([('name', '=', name)], limit=1)
        if not attribute:
            attribute = self.create({'name': name})
        return attribute

    def _sync_attribute_to_wc(self):
        """Sincroniza atributo global hacia WooCommerce."""
        backend = self.env['wc.backend'].search([], limit=1)
        if not backend:
            return
        for attribute in self:
            payload = {'name': attribute.name}
            try:
                if attribute.wc_attribute_id:
                    backend._wc_put(f'products/attributes/{attribute.wc_attribute_id}', payload)
                else:
                    response = backend._wc_post('products/attributes', payload)
                    attribute.write({'wc_attribute_id': response.get('id')})
            except Exception:
                _logger.exception('No se pudo sincronizar atributo %s', attribute.name)
