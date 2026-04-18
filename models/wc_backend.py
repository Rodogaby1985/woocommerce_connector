import logging
import threading
import time
from typing import Any, Dict, List, Optional, Union

import requests

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)
_last_call_times: Dict[int, float] = {}
_rate_lock = threading.Lock()


class WcBackend(models.Model):
    _name = 'wc.backend'
    _description = 'Backend WooCommerce'

    name = fields.Char(string='Nombre', required=True, default='WooCommerce Principal')
    wc_url = fields.Char(string='URL WooCommerce', required=True)
    wc_consumer_key = fields.Char(string='Consumer Key', required=True)
    wc_consumer_secret = fields.Char(string='Consumer Secret', required=True)
    webhook_secret = fields.Char(string='Secreto Webhook')
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
    batch_size = fields.Integer(string='Tamaño de lote', default=50)
    rate_limit = fields.Integer(string='Llamadas por minuto', default=60)
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
        """Realiza llamada a la API v3 de WooCommerce respetando el rate limit."""
        self.ensure_one()
        if not self.wc_url:
            raise UserError('Debe configurar la URL de WooCommerce.')

        with _rate_lock:
            min_interval = 60.0 / float(self.rate_limit or 60)
            backend_key = self.id or 0
            last_call = _last_call_times.get(backend_key, 0.0)
            elapsed = time.time() - last_call
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            _last_call_times[backend_key] = time.time()

        url = f"{self.wc_url.rstrip('/')}/wp-json/wc/v3/{endpoint.lstrip('/')}"
        response = requests.request(
            method=method,
            url=url,
            auth=(self.wc_consumer_key, self.wc_consumer_secret),
            json=data,
            params=params,
            timeout=30,
        )
        if not response.ok:
            raise UserError(f'Error WooCommerce API [{response.status_code}]: {response.text[:400]}')
        if not response.text:
            return {}
        return response.json()

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
