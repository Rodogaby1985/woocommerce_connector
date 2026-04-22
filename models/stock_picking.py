from odoo import fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    wc_quotation_order_id = fields.Many2one(
        'sale.order',
        string='Cotización Woo reservada',
        index=True,
        ondelete='set null',
    )
    wc_is_quotation_reservation = fields.Boolean(
        string='Reserva de cotización Woo',
        default=False,
        index=True,
    )
