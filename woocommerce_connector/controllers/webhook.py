import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Dict, List

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)
_WEBHOOK_WINDOW_SECONDS = 60
_WEBHOOK_MAX_PER_WINDOW = 120
_webhook_hits: Dict[str, List[float]] = {}
_webhook_lock = threading.Lock()


class WooCommerceWebhook(http.Controller):
    @http.route('/wc/webhook', type='json', auth='none', csrf=False, methods=['POST'])
    def receive_webhook(self):
        """Recibe webhooks de WooCommerce y encola trabajos asíncronos."""
        backend = request.env['wc.backend'].sudo().search([], limit=1)
        if not backend:
            return {'status': 'ignored', 'message': 'Backend no configurado'}
        if not backend.webhook_secret:
            _logger.warning('Webhook WooCommerce rechazado: secreto no configurado')
            return {'status': 'error', 'message': 'Webhook secret no configurado'}

        client_ip = request.httprequest.remote_addr or 'unknown'
        now = time.time()
        with _webhook_lock:
            hits = [ts for ts in _webhook_hits.get(client_ip, []) if now - ts <= _WEBHOOK_WINDOW_SECONDS]
            if len(hits) >= _WEBHOOK_MAX_PER_WINDOW:
                _logger.warning('Webhook WooCommerce limitado por rate limit para IP %s', client_ip)
                return {'status': 'error', 'message': 'Rate limit excedido'}
            hits.append(now)
            _webhook_hits[client_ip] = hits

        raw_body = request.httprequest.get_data() or b''
        signature = request.httprequest.headers.get('X-WC-Webhook-Signature')
        topic = request.httprequest.headers.get('X-WC-Webhook-Topic', '')

        if backend.webhook_secret:
            digest = hmac.new(
                backend.webhook_secret.encode('utf-8'),
                raw_body,
                hashlib.sha256,
            ).digest()
            expected = base64.b64encode(digest).decode('utf-8')
            if not signature or not hmac.compare_digest(signature, expected):
                _logger.warning('Webhook WooCommerce rechazado por firma inválida: %s', topic)
                return {'status': 'error', 'message': 'Firma inválida'}

        data = request.jsonrequest
        if not data and raw_body:
            try:
                data = json.loads(raw_body.decode('utf-8'))
            except Exception:
                data = {}

        env = request.env
        try:
            if topic in ('product.created', 'product.updated'):
                product = env['product.template'].sudo().search([('wc_id', '=', data.get('id'))], limit=1)
                if not product:
                    product = env['product.template'].sudo().create({'name': data.get('name') or 'Producto WooCommerce'})
                product._enqueue_wc_job(action='import', priority=5)
            elif topic in ('order.created', 'order.updated'):
                order = env['sale.order'].sudo().search([('wc_order_id', '=', data.get('id'))], limit=1)
                if not order:
                    partner = env['res.partner'].sudo()._get_or_create_from_wc({'email': data.get('billing', {}).get('email')})
                    order = env['sale.order'].sudo().create({'partner_id': partner.id, 'wc_order_id': data.get('id')})
                order._enqueue_wc_job(action='import', priority=1)
            elif topic in ('customer.created', 'customer.updated'):
                partner = env['res.partner'].sudo().search([('wc_customer_id', '=', data.get('id'))], limit=1)
                if not partner:
                    partner = env['res.partner'].sudo().create({'name': data.get('email') or 'Cliente WooCommerce'})
                partner._enqueue_wc_job(action='import', priority=5)
        except Exception:
            _logger.exception('Error encolando webhook WooCommerce topic=%s', topic)

        return {'status': 'ok'}
