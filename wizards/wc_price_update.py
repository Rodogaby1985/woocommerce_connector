from odoo import fields, models


class WcPriceUpdate(models.TransientModel):
    _name = 'wc.price.update'
    _description = 'Wizard de actualización masiva de precios WooCommerce'

    product_ids = fields.Many2many('product.template', string='Productos')
    update_regular_price = fields.Boolean(string='Actualizar precio regular', default=True)
    update_sale_price = fields.Boolean(string='Actualizar precio oferta', default=False)

    def action_update_prices(self):
        """Encola actualización de precios con prioridad alta."""
        self.ensure_one()
        if not self.update_regular_price and not self.update_sale_price:
            return False
        for product in self.product_ids:
            product._enqueue_wc_job(action='export', priority=3, data={
                'update_regular_price': self.update_regular_price,
                'update_sale_price': self.update_sale_price,
            })
        return {'type': 'ir.actions.act_window_close'}
