import logging

from odoo import fields, models
from .wc_sync_mixin import WcSyncMixin

_logger = logging.getLogger(__name__)


class SaleOrder(WcSyncMixin, models.Model):
    _inherit = 'sale.order'

    wc_order_id = fields.Integer(string='ID Pedido WooCommerce', index=True)
    wc_order_status = fields.Char(string='Estado WooCommerce')
    wc_payment_method = fields.Char(string='Método de pago WooCommerce')
    wc_order_note = fields.Text(string='Nota WooCommerce')
    wc_sync_date = fields.Datetime(string='Última sync WooCommerce')

    def _process_wc_order(self, wc_order: dict):
        """Crea o actualiza pedido de venta desde WooCommerce."""
        partner = self.env['res.partner']._get_or_create_from_wc({
            'id': wc_order.get('customer_id'),
            'email': wc_order.get('billing', {}).get('email'),
            'first_name': wc_order.get('billing', {}).get('first_name'),
            'last_name': wc_order.get('billing', {}).get('last_name'),
            'billing': wc_order.get('billing', {}),
        })

        order = self.search([('wc_order_id', '=', wc_order.get('id'))], limit=1)
        order_vals = {
            'partner_id': partner.id,
            'wc_order_id': wc_order.get('id'),
            'wc_order_status': wc_order.get('status'),
            'wc_payment_method': wc_order.get('payment_method_title') or wc_order.get('payment_method'),
            'wc_order_note': wc_order.get('customer_note'),
            'wc_sync_date': fields.Datetime.now(),
        }
        if order:
            order.with_context(wc_no_sync=True).write(order_vals)
            order.order_line.unlink()
        else:
            order = self.with_context(wc_no_sync=True).create(order_vals)

        for item in wc_order.get('line_items', []):
            product = False
            sku = item.get('sku')
            if sku:
                product = self.env['product.product'].search([('default_code', '=', sku)], limit=1)
            if not product and item.get('product_id'):
                product = self.env['product.template'].search([('wc_id', '=', item.get('product_id'))], limit=1).product_variant_id
            if not product:
                continue
            self.env['sale.order.line'].create({
                'order_id': order.id,
                'product_id': product.id,
                'name': item.get('name') or product.display_name,
                'product_uom_qty': float(item.get('quantity') or 1.0),
                'price_unit': float(item.get('price') or 0.0),
            })

        status = wc_order.get('status')
        if status == 'processing' and order.state == 'draft':
            order.action_confirm()
        elif status == 'completed':
            if order.state == 'draft':
                order.action_confirm()
            if order.state == 'sale':
                order._action_done()
        elif status == 'cancelled' and order.state not in ('cancel', 'done'):
            order.action_cancel()
        return order

    def action_sync_status_to_wc(self):
        """Envía estado de Odoo hacia WooCommerce."""
        backend = self._get_wc_backend()
        if not backend:
            return
        mapping = {
            'draft': 'pending',
            'sale': 'processing',
            'done': 'completed',
            'cancel': 'cancelled',
        }
        for order in self.filtered('wc_order_id'):
            status = mapping.get(order.state)
            if not status:
                continue
            try:
                backend._wc_put(f'orders/{order.wc_order_id}', {'status': status})
                order.with_context(wc_no_sync=True).write({'wc_order_status': status, 'wc_sync_date': fields.Datetime.now()})
            except Exception:
                _logger.exception('No se pudo sincronizar estado de pedido %s', order.name)

    def action_sync_to_wc(self):
        return self.action_sync_status_to_wc()

    def action_sync_from_wc(self):
        backend = self._get_wc_backend()
        if not backend:
            return
        for order in self.filtered('wc_order_id'):
            response = backend._wc_get(f'orders/{order.wc_order_id}')
            order._process_wc_order(response)
