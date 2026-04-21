import json
import logging
from datetime import timedelta
from html import escape

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    wc_order_id = fields.Integer(string='ID Pedido WooCommerce', index=True)
    wc_order_status = fields.Char(string='Estado WooCommerce')
    wc_payment_method = fields.Char(string='Método de pago WooCommerce')
    wc_order_note = fields.Text(string='Nota WooCommerce')
    wc_sync_date = fields.Datetime(string='Última sync WooCommerce')
    wc_stock_mismatch = fields.Boolean(string='Revisar stock', tracking=True)
    wc_stock_mismatch_note = fields.Text(string='Detalle inconsistencia de stock')

    def _get_wc_backend(self):
        return self.env['wc.backend'].search([], limit=1)

    def _enqueue_wc_job(self, action, priority=5, data=None):
        queue = self.env['wc.queue.job']
        payload = json.dumps(data or {})
        for record in self:
            queue.create({
                'name': f'{record._name} #{record.id} - {action}',
                'model_name': record._name,
                'record_id': record.id,
                'action': action,
                'priority': priority,
                'data': payload,
            })

    def _is_wc_sync_disabled(self):
        return bool(self.env.context.get('wc_no_sync'))

    def _wc_field_changed(self, vals, watched_fields):
        return any(field in vals for field in watched_fields)

    def _wc_recent_sync(self, sync_date_field):
        cooldown = fields.Datetime.now() - timedelta(seconds=30)
        return any(getattr(record, sync_date_field) and getattr(record, sync_date_field) >= cooldown for record in self)

    def _process_wc_order(self, wc_order: dict):
        """Crea o actualiza pedido de venta desde WooCommerce."""
        backend = self._get_wc_backend()
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

        mismatch_lines = []
        for item in wc_order.get('line_items', []):
            product = False
            sku = item.get('sku')
            if sku:
                product = self.env['product.product'].search([('default_code', '=', sku)], limit=1)
            if not product and item.get('variation_id'):
                product = self.env['product.product'].search([('wc_variation_id', '=', item.get('variation_id'))], limit=1)
            if not product and item.get('product_id'):
                product = self.env['product.template'].search([('wc_id', '=', item.get('product_id'))], limit=1).product_variant_id
            if not product:
                continue
            qty_ordered = float(item.get('quantity') or 1.0)
            # Usamos qty_available porque el conector también lo utiliza para sincronizar stock real a WooCommerce.
            qty_available = float(product.qty_available)
            if qty_ordered > qty_available:
                mismatch_lines.append({
                    'product_name': item.get('name') or product.product_tmpl_id.name or product.display_name,
                    'variant': product.display_name,
                    'sku': product.default_code or sku or '-',
                    'qty_ordered': qty_ordered,
                    'qty_available': qty_available,
                })
            self.env['sale.order.line'].create({
                'order_id': order.id,
                'product_id': product.id,
                'name': item.get('name') or product.display_name,
                'product_uom_qty': qty_ordered,
                'price_unit': float(item.get('price') or 0.0),
            })

        mismatch_note = False
        if mismatch_lines:
            detail_lines = [
                (
                    f"- Producto: {line['product_name']} | Variante: {line['variant']} | "
                    f"SKU: {line['sku']} | Cant. pedida: {line['qty_ordered']} | "
                    f"Stock Odoo: {line['qty_available']}"
                )
                for line in mismatch_lines
            ]
            mismatch_note = '\n'.join(detail_lines)

        previous_note = order.wc_stock_mismatch_note or ''
        previous_flag = bool(order.wc_stock_mismatch)
        order.with_context(wc_no_sync=True).write({
            'wc_stock_mismatch': bool(mismatch_lines),
            'wc_stock_mismatch_note': mismatch_note or False,
        })

        if mismatch_lines and (not previous_flag or previous_note != mismatch_note):
            chatter_msg = (
                f"Pedido Woo #{wc_order.get('id')} marcado para revisión de stock.\n"
                f"{mismatch_note}"
            )
            order.message_post(body=escape(chatter_msg).replace('\n', '<br/>'))
            if backend:
                email_subject = f"[WooCommerce] Revisar stock en pedido {order.name or '/'} (Woo #{wc_order.get('id')})"
                email_body = (
                    f"Se detectó inconsistencia de stock al importar un pedido.\n\n"
                    f"Woo order id: {wc_order.get('id')}\n"
                    f"Odoo order reference: {order.name or '/'}\n\n"
                    f"Detalle:\n{mismatch_note}"
                )
                backend._send_alert_email(email_subject, email_body)

        status = wc_order.get('status')
        if order.wc_stock_mismatch:
            return order
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

    def action_confirm(self):
        blocked_orders = self.filtered('wc_stock_mismatch')
        if blocked_orders:
            names = ', '.join(blocked_orders.mapped('name'))
            raise UserError(
                f'No se puede confirmar el pedido porque está marcado como "Revisar stock": {names}'
            )
        return super().action_confirm()
