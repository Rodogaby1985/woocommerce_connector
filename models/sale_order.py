import json
import logging
from datetime import timedelta
from html import escape

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)
_QUOTATION_STATES = ('draft', 'sent')
_RESERVATION_FINAL_STATES = ('done', 'cancel')
_RESERVATION_NON_PENDING_STATES = ('assigned',) + _RESERVATION_FINAL_STATES


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    wc_order_id = fields.Integer(string='ID Pedido WooCommerce', index=True)
    wc_order_status = fields.Char(string='Estado WooCommerce')
    wc_payment_method = fields.Char(string='Método de pago WooCommerce')
    wc_order_note = fields.Text(string='Nota WooCommerce')
    wc_sync_date = fields.Datetime(string='Última sync WooCommerce')
    wc_stock_mismatch = fields.Boolean(string='Revisar stock', tracking=True)
    wc_stock_mismatch_note = fields.Text(string='Detalle inconsistencia de stock')
    wc_reservation_picking_ids = fields.One2many(
        'stock.picking',
        'wc_quotation_order_id',
        string='Reservas Woo en cotización',
    )

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
            can_rebuild_lines = order.state in _QUOTATION_STATES
            if can_rebuild_lines:
                order.order_line.unlink()
        else:
            order = self.with_context(wc_no_sync=True).create(order_vals)
            can_rebuild_lines = True

        mismatch_lines = []
        if can_rebuild_lines:
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
        if can_rebuild_lines:
            order.with_context(wc_no_sync=True).write({
                'wc_stock_mismatch': bool(mismatch_lines),
                'wc_stock_mismatch_note': mismatch_note or False,
            })

        if can_rebuild_lines and mismatch_lines and (not previous_flag or previous_note != mismatch_note):
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
        should_auto_confirm = order.state in _QUOTATION_STATES and order._wc_should_auto_confirm(backend)
        if order.wc_stock_mismatch:
            order._release_wc_quotation_stock_reservation()
            return order
        if status in ('pending', 'on-hold') and order.state in _QUOTATION_STATES:
            order._ensure_wc_quotation_stock_reservation(backend=backend)
        elif status == 'processing' and should_auto_confirm:
            order._release_wc_quotation_stock_reservation()
            order.action_confirm()
        elif status == 'completed':
            order._release_wc_quotation_stock_reservation()
            if should_auto_confirm:
                order.action_confirm()
            if order.state == 'sale' and (not backend or backend.auto_complete_completed_orders):
                order._action_done()
        elif status == 'cancelled':
            order._release_wc_quotation_stock_reservation()
            if order.state in _QUOTATION_STATES and backend and backend.cancelled_order_action == 'cancel_quote':
                order.action_cancel()
        return order

    def _get_wc_reservation_picking_type(self):
        self.ensure_one()
        warehouse = self.warehouse_id or self.env['stock.warehouse'].search([], limit=1)
        return warehouse.out_type_id if warehouse else self.env['stock.picking.type']

    def _build_wc_reservation_move_vals(self, picking, line):
        self.ensure_one()
        return {
            'name': line.name or line.product_id.display_name,
            'product_id': line.product_id.id,
            'product_uom': line.product_uom.id,
            'product_uom_qty': line.product_uom_qty,
            'location_id': picking.location_id.id,
            'location_dest_id': picking.location_dest_id.id,
            'company_id': self.company_id.id,
            'picking_id': picking.id,
            'state': 'draft',
        }

    def _wc_should_auto_confirm(self, backend):
        return not backend or backend.auto_confirm_processing_orders

    def _ensure_wc_quotation_stock_reservation(self, backend=None):
        """Crear y reservar picking de stock para una cotización Woo pending/on-hold.

        Reemplaza reservas previas activas de la misma cotización para mantener idempotencia.
        Si no logra reservar todas las líneas, envía alerta por email (si está habilitado).
        """
        self.ensure_one()
        backend = backend or self._get_wc_backend()
        if backend and not backend.reserve_stock_on_pending_quote:
            return
        if self.state not in _QUOTATION_STATES:
            return
        reservable_lines = self.order_line.filtered(
            lambda line: line.product_id and line.product_id.type == 'product' and line.product_uom_qty > 0
        )
        if not reservable_lines:
            return
        warehouse = self.warehouse_id or self.env['stock.warehouse'].search([], limit=1)
        picking_type = self._get_wc_reservation_picking_type()
        if not picking_type:
            warehouse_name = warehouse.display_name if warehouse else 'Ninguno'
            raise UserError(
                f'No se encontró tipo de operación de salida para el almacén {warehouse_name}. '
                'Revise la configuración del almacén.'
            )
        source_location = picking_type.default_location_src_id
        dest_location = self.partner_shipping_id.property_stock_customer or picking_type.default_location_dest_id
        if not source_location or not dest_location:
            raise UserError(
                f'Falta ubicación de origen o destino en el tipo de operación {picking_type.display_name}. '
                'Revise la configuración de ubicaciones.'
            )
        self._release_wc_quotation_stock_reservation()
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
            'partner_id': self.partner_shipping_id.id or self.partner_id.id,
            'origin': self.name,
            'company_id': self.company_id.id,
            'scheduled_date': fields.Datetime.now(),
            'wc_quotation_order_id': self.id,
            'wc_is_quotation_reservation': True,
        })
        for line in reservable_lines:
            self.env['stock.move'].create(self._build_wc_reservation_move_vals(picking=picking, line=line))
        if picking.state == 'draft':
            picking.action_confirm()
        picking.action_assign()
        pending_reservation_moves = picking.move_ids_without_package.filtered(
            lambda move: move.state not in _RESERVATION_NON_PENDING_STATES
        )
        if pending_reservation_moves and backend:
            backend._send_alert_email(
                subject=f'[WooCommerce][{self.env.cr.dbname}] Reserva parcial en cotización {self.name}',
                body=(
                    f'Backend: {backend.display_name}\n'
                    f'Pedido Odoo: {self.name}\n'
                    f'Woo order id: {self.wc_order_id}\n'
                    f'No se pudo reservar completamente el stock para todas las líneas.'
                ),
            )

    def _release_wc_quotation_stock_reservation(self):
        for order in self:
            pickings = order.wc_reservation_picking_ids.filtered(lambda p: p.state not in _RESERVATION_FINAL_STATES)
            if not pickings:
                continue
            for picking in pickings:
                moves = picking.move_ids_without_package.filtered(lambda move: move.state not in _RESERVATION_FINAL_STATES)
                if moves:
                    moves._do_unreserve()
                    moves._action_cancel()
                picking.action_cancel()

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
