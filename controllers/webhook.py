import base64
import hashlib
import hmac
import json
import logging
import secrets

from odoo import fields, http
from odoo.http import request
from werkzeug.wrappers import Response

_logger = logging.getLogger(__name__)


class WooCommerceWebhook(http.Controller):
    def _json_response(self, payload, status=200):
        return Response(
            json.dumps(payload),
            status=status,
            content_type='application/json',
        )

    @http.route(
        ['/wc/webhook/<string:token>', '/wc/webhook'],
        type='http',
        auth='public',
        csrf=False,
        methods=['POST'],
    )
    def receive_webhook(self, token=None):
        backend_model = request.env['wc.backend'].sudo()
        backend = backend_model.search([], limit=1)
        if not backend:
            return self._json_response({'status': 'ignored', 'message': 'Backend no configurado'}, status=202)
        backend._ensure_webhook_token()

        if token:
            token_backend = backend_model.search([('webhook_token', '=', token)], limit=1)
            if not token_backend or not secrets.compare_digest(token_backend.webhook_token or '', token):
                _logger.warning('Webhook WooCommerce rechazado por token inválido')
                return self._json_response({'status': 'error', 'message': 'Token inválido'}, status=403)
            backend = token_backend
        else:
            _logger.warning('Webhook legado /wc/webhook utilizado sin token (deprecado)')

        raw_body = request.httprequest.get_data() or b''
        signature = request.httprequest.headers.get('X-WC-Webhook-Signature')
        topic = request.httprequest.headers.get('X-WC-Webhook-Topic', '')
        client_ip = request.httprequest.remote_addr or 'unknown'

        if not backend._check_webhook_rate_limit(client_ip=client_ip):
            _logger.warning('Webhook WooCommerce limitado por rate limit para backend %s IP %s', backend.id, client_ip)
            return self._json_response({'status': 'error', 'message': 'Rate limit excedido'}, status=429)

        if not backend.webhook_secret:
            _logger.warning('Webhook WooCommerce rechazado: secreto no configurado')
            backend.write({'webhook_last_error': 'Webhook secret no configurado'})
            return self._json_response({'status': 'error', 'message': 'Webhook secret no configurado'}, status=400)

        digest = hmac.new(
            backend.webhook_secret.encode('utf-8'),
            raw_body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode('utf-8')
        if not signature or not hmac.compare_digest(signature, expected):
            _logger.warning('Webhook WooCommerce rechazado por firma inválida: %s', topic)
            backend.write({'webhook_last_error': 'Firma inválida'})
            return self._json_response({'status': 'error', 'message': 'Firma inválida'}, status=403)

        data = {}
        if raw_body:
            try:
                data = json.loads(raw_body.decode('utf-8'))
            except Exception:
                data = {}

        entity_id = data.get('id')
        try:
            if topic in ('order.created', 'order.updated') and entity_id:
                backend._enqueue_order_import_webhook_job(
                    order_id=entity_id,
                    push_stock_after_import=backend.webhook_push_stock_after_import,
                )
            elif topic in ('product.created', 'product.updated') and entity_id:
                backend._enqueue_product_import_webhook_job(entity_id)
            elif topic in ('customer.created', 'customer.updated') and entity_id:
                backend._enqueue_customer_import_webhook_job(entity_id)
        except Exception as exc:
            _logger.exception('Error encolando webhook WooCommerce topic=%s id=%s', topic, entity_id)
            backend.write({'webhook_last_error': str(exc)})
            return self._json_response({'status': 'error', 'message': 'Error encolando webhook'}, status=500)

        backend.write({
            'webhook_last_received_at': fields.Datetime.now(),
            'webhook_last_error': False,
        })
        return self._json_response({'status': 'ok'}, status=200)
