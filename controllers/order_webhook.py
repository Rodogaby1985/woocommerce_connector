import base64
import hashlib
import hmac
import json
import logging

from odoo import fields, http
from odoo.http import request
from werkzeug.wrappers import Response

_logger = logging.getLogger(__name__)


class WooCommerceOrderWebhook(http.Controller):
    def _extract_source_url(self, payload):
        if not isinstance(payload, dict):
            return False
        if payload.get('store_url'):
            return payload.get('store_url')
        links = payload.get('_links') or {}
        self_links = links.get('self') or []
        if isinstance(self_links, list):
            for item in self_links:
                href = isinstance(item, dict) and item.get('href')
                if href:
                    return href
        return False

    def _verify_backend_signature(self, ordered_backends, signature, raw_body):
        verified_backend = request.env['wc.backend']
        for backend in ordered_backends:
            if not backend.webhook_secret:
                continue
            try:
                secret_bytes = backend.webhook_secret.encode('utf-8')
            except UnicodeEncodeError:
                _logger.warning('Webhook secret inválido en backend %s', backend.display_name)
                continue
            digest = hmac.new(secret_bytes, raw_body, hashlib.sha256).digest()
            expected_signature = base64.b64encode(digest).decode('utf-8')
            if signature and hmac.compare_digest(signature, expected_signature):
                verified_backend = backend
                break
        return verified_backend

    def _receive_order_event(self, event_label, backend_fetch_method):
        raw_body = request.httprequest.get_data() or b''
        signature = request.httprequest.headers.get('X-WC-Webhook-Signature')
        try:
            payload = json.loads(raw_body.decode('utf-8')) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            _logger.warning('Webhook %s descartado: payload JSON inválido', event_label)
            return Response('Invalid JSON payload', status=400)

        source_url = request.httprequest.headers.get('X-WC-Webhook-Source') or self._extract_source_url(payload)
        backend_model = request.env['wc.backend'].sudo()
        backends = getattr(backend_model, backend_fetch_method)()
        if not backends:
            _logger.info('Webhook %s ignorado: no hay backend conectado', event_label)
            return Response('Ignored', status=202)

        source_backend = backend_model._match_backend_by_store_url(backends, source_url)
        ordered_backends = source_backend + (backends - source_backend) if source_backend else backends
        verified_backend = self._verify_backend_signature(
            ordered_backends=ordered_backends,
            signature=signature,
            raw_body=raw_body,
        )
        if not verified_backend:
            backend_for_error = source_backend or ordered_backends[:1]
            status = 401 if not signature else 403
            message = f'Firma webhook inválida para {event_label}'
            _logger.warning(message)
            if backend_for_error:
                backend_for_error.write({'webhook_last_error': message})
            return Response('Forbidden', status=status)

        verified_backend.write({
            'webhook_last_received_at': fields.Datetime.now(),
            'webhook_last_error': False,
        })
        order_id = payload.get('id')
        if not order_id:
            message = f'Webhook {event_label} sin id de pedido'
            _logger.warning(message)
            verified_backend.write({'webhook_last_error': message})
            return Response('Missing order id', status=202)
        try:
            verified_backend._enqueue_order_sync_webhook_job(
                order_id=order_id,
                push_stock_after_import=verified_backend.webhook_push_stock_after_import,
            )
        except Exception as exc:
            _logger.exception('Error encolando sync de pedido via webhook %s', event_label)
            error_message = str(exc)
            verified_backend.write({'webhook_last_error': error_message})
            verified_backend._send_alert_email(
                subject=f'[WooCommerce][{request.env.cr.dbname}] Error webhook {event_label}',
                body=(
                    f'Backend: {verified_backend.display_name}\n'
                    f'Order ID: {order_id}\n'
                    f'Error: {error_message}'
                ),
            )
            return Response('Accepted', status=202)
        return Response('Accepted', status=202)

    @http.route('/woocommerce/webhook/order-created', type='http', auth='public', csrf=False, methods=['POST'])
    def receive_order_created(self):
        return self._receive_order_event('order.created', '_get_order_webhook_backends')

    @http.route('/woocommerce/webhook/order-updated', type='http', auth='public', csrf=False, methods=['POST'])
    def receive_order_updated(self):
        return self._receive_order_event('order.updated', '_get_order_updated_webhook_backends')
