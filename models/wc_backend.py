import json
import logging
import re
import threading
import time
import secrets
from datetime import timedelta
from html import escape
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)
_MAX_WC_REQUEST_ATTEMPTS = 3
_DEFAULT_RETRY_AFTER_SECONDS = 30
_EMAIL_PATTERN = re.compile(r'^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$')
_DEFAULT_CATCHUP_BATCH_SIZE = 50
_MIN_CATCHUP_BATCH_SIZE = 1
_MAX_CATCHUP_BATCH_SIZE = 100
_RECONCILE_ORDER_STATUSES = 'pending,on-hold,processing,completed,cancelled'


class WcBackend(models.Model):
    _name = 'wc.backend'
    _description = 'Backend WooCommerce'

    name = fields.Char(string='Nombre', required=True, default='WooCommerce Principal')
    wc_url = fields.Char(string='URL WooCommerce', required=True)
    wc_consumer_key = fields.Char(string='Consumer Key', required=True, password=True)
    wc_consumer_secret = fields.Char(string='Consumer Secret', required=True, password=True)
    enable_order_webhook = fields.Boolean(string='Activar webhook de pedidos', default=False)
    enable_order_updated_webhook = fields.Boolean(string='Activar webhook order.updated', default=False)
    webhook_secret = fields.Char(string='Secreto Webhook', password=True)
    webhook_token = fields.Char(
        string='Token Webhook',
        copy=False,
        readonly=True,
        default=lambda self: secrets.token_urlsafe(32),
    )
    webhook_push_stock_after_import = fields.Boolean(
        string='Actualizar stock tras importar pedido (webhook)',
        default=True,
    )
    webhook_last_received_at = fields.Datetime(string='Último webhook recibido', readonly=True)
    webhook_last_error = fields.Text(string='Último error de webhook', readonly=True)
    reserve_stock_on_pending_quote = fields.Boolean(
        string='Reservar stock en cotizaciones pending/on-hold',
        default=True,
    )
    auto_confirm_processing_orders = fields.Boolean(
        string='Confirmar automáticamente cuando Woo pasa a processing',
        default=True,
    )
    auto_complete_completed_orders = fields.Boolean(
        string='Marcar como hecho cuando Woo pasa a completed',
        default=True,
    )
    cancelled_order_action = fields.Selection([
        ('keep_quote', 'Liberar reserva y mantener cotización'),
        ('cancel_quote', 'Liberar reserva y cancelar cotización segura'),
    ], string='Acción para cancelled', default='keep_quote', required=True)
    enable_order_catchup = fields.Boolean(
        string='Activar reconciliación periódica de pedidos',
        default=True,
    )
    order_catchup_interval_minutes = fields.Integer(
        string='Intervalo de reconciliación (minutos)',
        default=5,
    )
    order_catchup_last_run = fields.Datetime(string='Última reconciliación de pedidos', readonly=True)
    price_strategy = fields.Selection([
        ('custom_fields', 'Campos personalizados'),
        ('pricelist', 'Lista de precios'),
    ], string='Estrategia de precios', default='custom_fields', required=True)
    sale_pricelist_id = fields.Many2one('product.pricelist', string='Lista de precios')
    sync_stock = fields.Boolean(string='Sincronizar stock', default=True)
    sync_prices = fields.Boolean(string='Sincronizar precios', default=True)
    sync_orders = fields.Boolean(string='Sincronizar pedidos', default=True)
    sync_customers = fields.Boolean(string='Sincronizar clientes', default=True)
    sync_images = fields.Boolean(string='Sincronizar imágenes', default=False)
    enable_error_notifications = fields.Boolean(string='Activar alertas de error', default=False)
    error_notification_emails = fields.Text(string='Emails de notificación de error')
    batch_size = fields.Integer(string='Tamaño de lote', default=50)
    rate_limit = fields.Integer(string='Llamadas por minuto', default=60)
    wc_next_call_time = fields.Datetime(string='Próxima llamada API Woo')
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('connected', 'Conectado'),
        ('error', 'Error'),
    ], string='Estado', default='draft', tracking=True)
    last_sync_date = fields.Datetime(string='Última sincronización')
    last_error = fields.Text(string='Último error')

    product_synced_count = fields.Integer(string='Productos sincronizados', compute='_compute_dashboard_stats')
    product_total_count = fields.Integer(string='Total productos', compute='_compute_dashboard_stats')
    variant_synced_count = fields.Integer(string='Variantes sincronizadas', compute='_compute_dashboard_stats')
    variant_total_count = fields.Integer(string='Total variantes', compute='_compute_dashboard_stats')
    pending_job_count = fields.Integer(string='Trabajos pendientes', compute='_compute_dashboard_stats')
    error_job_count = fields.Integer(string='Trabajos en error', compute='_compute_dashboard_stats')
    month_order_count = fields.Integer(string='Pedidos del mes', compute='_compute_dashboard_stats')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('webhook_token'):
                vals['webhook_token'] = secrets.token_urlsafe(32)
        return super().create(vals_list)

    def action_regenerate_webhook_token(self):
        for backend in self:
            backend.write({'webhook_token': secrets.token_urlsafe(32)})

    def _ensure_webhook_token(self):
        for backend in self.filtered(lambda b: not b.webhook_token):
            backend.write({'webhook_token': secrets.token_urlsafe(32)})

    def _get_rate_limit_interval_seconds(self) -> float:
        self.ensure_one()
        return 60.0 / float(self.rate_limit or 60)

    def _reserve_wc_rate_limit_slot(self):
        self.ensure_one()
        now = fields.Datetime.now()
        min_interval = self._get_rate_limit_interval_seconds()
        self.env.cr.execute(
            'SELECT wc_next_call_time FROM wc_backend WHERE id = %s FOR UPDATE',
            [self.id],
        )
        row = self.env.cr.fetchone()
        next_call_time = row and row[0] or False
        if next_call_time and next_call_time > now:
            wait_seconds = max(1, int((next_call_time - now).total_seconds()))
            raise UserError(
                f'Rate limit de WooCommerce activo. Reintentar en {wait_seconds} segundo(s).'
            )
        self.write({'wc_next_call_time': now + timedelta(seconds=min_interval)})

    def _set_wc_next_call_time(self, delay_seconds: int):
        self.ensure_one()
        delay = max(int(delay_seconds or 0), 1)
        self.write({'wc_next_call_time': fields.Datetime.now() + timedelta(seconds=delay)})

    def _parse_retry_after(self, response):
        retry_header = response.headers.get('Retry-After')
        if not retry_header:
            return _DEFAULT_RETRY_AFTER_SECONDS
        try:
            retry_after = int(retry_header)
            return max(retry_after, 1)
        except (TypeError, ValueError):
            return _DEFAULT_RETRY_AFTER_SECONDS

    def _check_webhook_rate_limit(
        self,
        client_ip: str,
        window_seconds: int = 60,
        max_per_window: int = 120,
    ) -> bool:
        self.ensure_one()
        now = fields.Datetime.now()
        threshold = now - timedelta(seconds=window_seconds)
        self.env.cr.execute(
            'SELECT id FROM wc_backend WHERE id = %s FOR UPDATE',
            [self.id],
        )
        log_model = self.env['wc.webhook.log'].sudo()
        recent_count = log_model.search_count([
            ('backend_id', '=', self.id),
            ('client_ip', '=', client_ip),
            ('create_date', '>=', threshold),
        ])
        if recent_count >= max_per_window:
            return False
        log_model.create({
            'backend_id': self.id,
            'client_ip': client_ip,
        })
        return True

    def _compute_dashboard_stats(self):
        month_start = fields.Datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for backend in self:
            backend.product_total_count = self.env['product.template'].search_count([])
            backend.product_synced_count = self.env['product.template'].search_count([('wc_id', '!=', False)])
            backend.variant_total_count = self.env['product.product'].search_count([])
            backend.variant_synced_count = self.env['product.product'].search_count([('wc_variation_id', '!=', False)])
            backend.pending_job_count = self.env['wc.queue.job'].search_count([('state', 'in', ['pending', 'processing'])])
            backend.error_job_count = self.env['wc.queue.job'].search_count([('state', '=', 'error')])
            backend.month_order_count = self.env['sale.order'].search_count([('date_order', '>=', month_start)])

    @api.constrains('order_catchup_interval_minutes')
    def _check_order_catchup_interval_minutes(self):
        for backend in self:
            if backend.order_catchup_interval_minutes < 1:
                raise ValidationError('El intervalo de reconciliación debe ser de al menos 1 minuto.')

    def action_test_connection(self):
        """Prueba conexión a la API REST de WooCommerce."""
        for backend in self:
            try:
                backend._wc_get('system_status')
                backend.write({'state': 'connected', 'last_error': False})
            except Exception as exc:
                _logger.exception('Error probando conexión WooCommerce')
                backend.write({'state': 'error', 'last_error': str(exc)})
                raise UserError(f'No se pudo conectar a WooCommerce: {exc}')

    def _get_wc_api(self, endpoint: str, method: str = 'GET', data: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """Realiza llamada a la API v3 de WooCommerce con control de rate limit/reintentos."""
        self.ensure_one()
        if not self.wc_url:
            raise UserError('Debe configurar la URL de WooCommerce.')

        url = f"{self.wc_url.rstrip('/')}/wp-json/wc/v3/{endpoint.lstrip('/')}"
        last_exception = None
        for attempt in range(1, _MAX_WC_REQUEST_ATTEMPTS + 1):
            try:
                self._reserve_wc_rate_limit_slot()
            except UserError as exc:
                last_exception = exc
                if attempt < _MAX_WC_REQUEST_ATTEMPTS:
                    continue
                raise
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    auth=(self.wc_consumer_key, self.wc_consumer_secret),
                    json=data,
                    params=params,
                    timeout=30,
                )
            except requests.RequestException as exc:
                last_exception = exc
                if attempt < _MAX_WC_REQUEST_ATTEMPTS:
                    continue
                raise UserError(f'Error de red WooCommerce: {exc}') from exc

            if response.status_code == 429:
                retry_after = self._parse_retry_after(response)
                self._set_wc_next_call_time(retry_after)
                last_exception = UserError(
                    f'WooCommerce devolvió 429 (rate limit). Reintentar en {retry_after} segundo(s).'
                )
                if attempt < _MAX_WC_REQUEST_ATTEMPTS:
                    continue
                raise last_exception
            if response.status_code >= 500 and attempt < _MAX_WC_REQUEST_ATTEMPTS:
                continue
            if not response.ok:
                raise UserError(f'Error WooCommerce API [{response.status_code}]: {response.text[:400]}')
            if not response.text:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise UserError('Respuesta inválida de WooCommerce (JSON inválido).') from exc

        raise UserError(f'No se pudo completar la llamada a WooCommerce: {last_exception}')

    def _wc_get(self, endpoint: str, params: Optional[Dict[str, Any]] = None):
        return self._get_wc_api(endpoint=endpoint, method='GET', params=params)

    def _wc_post(self, endpoint: str, data: Dict[str, Any]):
        return self._get_wc_api(endpoint=endpoint, method='POST', data=data)

    def _wc_put(self, endpoint: str, data: Dict[str, Any]):
        return self._get_wc_api(endpoint=endpoint, method='PUT', data=data)

    def action_sync_all(self):
        """Abre el wizard de sincronización inicial."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Sincronización inicial WooCommerce',
            'res_model': 'wc.initial.sync',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_batch_size': self.batch_size},
        }

    def action_open_dashboard(self):
        """Abre el dashboard sobre un backend existente."""
        backend = self.env['wc.backend'].search([('state', '=', 'connected')], limit=1)
        if not backend:
            backend = self.env['wc.backend'].search([], limit=1)
        if not backend:
            return self.env['ir.actions.actions']._for_xml_id('woocommerce_connector.action_wc_backend_form')
        return {
            'type': 'ir.actions.act_window',
            'name': 'Dashboard WooCommerce',
            'res_model': 'wc.backend',
            'view_mode': 'form',
            'view_id': self.env.ref('woocommerce_connector.view_wc_dashboard_form').id,
            'res_id': backend.id,
            'target': 'current',
        }

    def _cron_process_queue(self):
        """Procesa cola de sincronizaciones desde cron."""
        try:
            self.env['wc.queue.job']._process_pending_jobs(limit=50)
            self.search([], limit=1).write({'last_sync_date': fields.Datetime.now(), 'last_error': False})
        except Exception as exc:
            _logger.exception('Falló el procesamiento de cola WooCommerce')
            backend = self.search([], limit=1)
            if backend:
                backend.write({'state': 'error', 'last_error': str(exc)})

    @api.model
    def _cron_reconcile_orders(self):
        """Reconciliar pedidos Woo modificados desde la última ejecución."""
        now = self._as_utc_naive(fields.Datetime.now())
        backends = self.search([
            ('state', '=', 'connected'),
            ('enable_order_catchup', '=', True),
            ('sync_orders', '=', True),
        ])
        for backend in backends:
            if backend.order_catchup_last_run:
                last_run = self._as_utc_naive(backend.order_catchup_last_run)
                delta = now - last_run
                if delta < timedelta(minutes=backend.order_catchup_interval_minutes or 1):
                    continue
            backend._reconcile_orders_since_last_run()

    def _send_alert_email(self, subject: str, body: str):
        """Envía alertas por email según configuración del backend."""
        self.ensure_one()
        if not self.enable_error_notifications:
            return False

        recipients = self._parse_notification_emails()
        if not recipients:
            _logger.warning(
                'Alerta WooCommerce no enviada: configure destinatarios en backend %s (%s)',
                self.display_name,
                self.id,
            )
            return False

        mail = self.env['mail.mail'].sudo().create({
            'subject': subject,
            'email_to': ','.join(recipients),
            'body_html': f'<pre style="white-space:pre-wrap">{escape(body or "")}</pre>',
        })
        mail.send()
        return True

    def _parse_notification_emails(self):
        self.ensure_one()
        recipients_raw = self.error_notification_emails or ''
        raw_candidates = [mail.strip() for mail in re.split(r'[,;\s]+', recipients_raw) if mail.strip()]
        valid_recipients = []
        invalid_recipients = []
        seen = set()
        for email in raw_candidates:
            normalized = email
            if not normalized:
                continue
            if not _EMAIL_PATTERN.match(normalized):
                invalid_recipients.append(normalized)
                continue
            if normalized.lower() in seen:
                continue
            seen.add(normalized.lower())
            valid_recipients.append(normalized)
        if invalid_recipients:
            _logger.warning(
                'Se ignoraron emails inválidos en backend %s (%s): %s',
                self.display_name,
                self.id,
                ', '.join(invalid_recipients),
            )
        return valid_recipients

    @api.model
    def _normalize_store_url(self, url):
        if not url:
            return False
        parsed = urlparse(url.strip())
        if not parsed.scheme or not parsed.netloc:
            return False
        return f'{parsed.scheme}://{parsed.netloc}'.rstrip('/').lower()

    @api.model
    def _get_order_webhook_backends(self):
        backends = self.search([
            ('state', '=', 'connected'),
            ('enable_order_webhook', '=', True),
        ])
        if backends:
            return backends
        return self.search([('state', '=', 'connected')])

    @api.model
    def _get_order_updated_webhook_backends(self):
        return self.search([
            ('state', '=', 'connected'),
            ('enable_order_updated_webhook', '=', True),
        ])

    @api.model
    def _match_backend_by_store_url(self, backends, source_url):
        normalized_source = self._normalize_store_url(source_url)
        if not normalized_source:
            return self.browse()
        for backend in backends:
            if self._normalize_store_url(backend.wc_url) == normalized_source:
                return backend
        return self.browse()

    def _enqueue_order_sync_webhook_job(self, order_id, push_stock_after_import=True):
        self.ensure_one()
        try:
            order_id = int(order_id)
        except (TypeError, ValueError) as exc:
            raise UserError(f'ID de pedido inválido para webhook: {order_id}') from exc
        self._enqueue_webhook_import_job(
            name=f'Webhook import order #{order_id}',
            method_name='_webhook_import_order_by_id',
            args=[order_id],
            kwargs={'push_stock_after_import': bool(push_stock_after_import)},
            priority=1,
        )

    def _enqueue_product_import_webhook_job(self, wc_product_id):
        self.ensure_one()
        try:
            wc_product_id = int(wc_product_id)
        except (TypeError, ValueError) as exc:
            raise UserError(f'ID de producto inválido para webhook: {wc_product_id}') from exc
        self._enqueue_webhook_import_job(
            name=f'Webhook import product #{wc_product_id}',
            method_name='_webhook_import_product_by_id',
            args=[wc_product_id],
            priority=5,
        )

    def _enqueue_customer_import_webhook_job(self, wc_customer_id):
        self.ensure_one()
        try:
            wc_customer_id = int(wc_customer_id)
        except (TypeError, ValueError) as exc:
            raise UserError(f'ID de cliente inválido para webhook: {wc_customer_id}') from exc
        self._enqueue_webhook_import_job(
            name=f'Webhook import customer #{wc_customer_id}',
            method_name='_webhook_import_customer_by_id',
            args=[wc_customer_id],
            priority=5,
        )

    def _enqueue_webhook_import_job(
        self,
        name: str,
        method_name: str,
        args: Optional[List[Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
        priority: int = 5,
    ):
        self.ensure_one()
        self.env['wc.queue.job'].create({
            'name': name,
            'model_name': self._name,
            'record_id': self.id,
            'action': 'import',
            'priority': priority,
            'data': json.dumps({
                'method': method_name,
                'args': args or [],
                'kwargs': kwargs or {},
            }),
        })

    def _enqueue_order_import_webhook_job(self, order_id, push_stock_after_import=True):
        self.ensure_one()
        return self._enqueue_order_sync_webhook_job(
            order_id=order_id,
            push_stock_after_import=push_stock_after_import,
        )

    def _webhook_import_order_by_id(self, order_id, push_stock_after_import=True):
        self.ensure_one()
        try:
            normalized_order_id = int(order_id)
        except (TypeError, ValueError) as exc:
            raise UserError(f'ID de pedido inválido para importación webhook: {order_id}') from exc
        wc_order = self._wc_get(f'orders/{normalized_order_id}')
        order = self.env['sale.order']._process_wc_order(wc_order)
        if not push_stock_after_import or not self.sync_stock:
            return order

        if not order.order_line:
            return order
        variants = order.order_line.mapped('product_id').filtered(lambda p: p.wc_variation_id)
        for variant in variants:
            variant.action_sync_to_wc()
        return order

    def _reconcile_orders_since_last_run(self):
        self.ensure_one()
        started_at = fields.Datetime.now()
        since = self.order_catchup_last_run or (started_at - timedelta(days=1))
        try:
            orders = self._fetch_orders_modified_since(since)
            for wc_order in orders:
                self.env['sale.order']._process_wc_order(wc_order)
            self.write({
                'order_catchup_last_run': started_at,
                'last_error': False,
            })
        except Exception as exc:
            _logger.exception('Error reconciliando pedidos WooCommerce para backend %s', self.display_name)
            message = str(exc)
            self.write({'last_error': message})
            self._send_alert_email(
                subject=f'[WooCommerce][{self.env.cr.dbname}] Error cron reconciliación pedidos',
                body=(
                    f'Backend: {self.display_name}\n'
                    f'Desde: {since}\n'
                    f'Error: {message}'
                ),
            )

    def _fetch_orders_modified_since(self, since_dt):
        self.ensure_one()
        if not since_dt:
            return []
        since_utc_dt = self._as_utc_naive(since_dt)
        since_utc = since_utc_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        page = 1
        # Clamp per_page to WooCommerce API bounds [1, 100].
        per_page = min(max(self.batch_size or _DEFAULT_CATCHUP_BATCH_SIZE, _MIN_CATCHUP_BATCH_SIZE), _MAX_CATCHUP_BATCH_SIZE)
        all_orders = []
        while True:
            params = {
                'per_page': per_page,
                'page': page,
                'status': _RECONCILE_ORDER_STATUSES,
                'orderby': 'modified',
                'order': 'asc',
                'modified_after': since_utc,
            }
            page_orders = self._wc_get('orders', params=params)
            if not page_orders:
                break
            all_orders.extend(page_orders)
            if len(page_orders) < per_page:
                break
            page += 1
        return all_orders

    @api.model
    def _as_utc_naive(self, dt):
        if not dt:
            return None
        if dt.tzinfo:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
