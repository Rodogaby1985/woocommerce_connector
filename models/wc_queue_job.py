import json
import logging
import traceback
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class WcQueueJob(models.Model):
    _name = 'wc.queue.job'
    _description = 'Cola de trabajos WooCommerce'
    _order = 'priority asc, id asc'

    name = fields.Char(string='Nombre', required=True)
    model_name = fields.Char(string='Modelo', required=True)
    record_id = fields.Integer(string='ID Registro', required=True)
    action = fields.Selection([
        ('export', 'Exportar a WC'),
        ('import', 'Importar de WC'),
    ], string='Acción', required=True)
    priority = fields.Integer(string='Prioridad', default=5)
    state = fields.Selection([
        ('pending', 'Pendiente'),
        ('processing', 'Procesando'),
        ('done', 'Completado'),
        ('error', 'Error'),
        ('cancelled', 'Cancelado'),
    ], string='Estado', default='pending', index=True)
    error_message = fields.Text(string='Mensaje de error')
    retry_count = fields.Integer(string='Reintentos', default=0)
    data = fields.Text(string='Datos JSON')
    date_created = fields.Datetime(string='Fecha creación', default=fields.Datetime.now)
    date_processed = fields.Datetime(string='Fecha procesado')

    @api.model
    def _process_pending_jobs(self, limit: int = 50):
        """Procesa trabajos pendientes según prioridad."""
        jobs = self.search([('state', '=', 'pending')], limit=limit, order='priority asc, id asc')
        for job in jobs:
            job._process_job()

    def _process_job(self):
        """Ejecuta un trabajo individual de sincronización."""
        self.ensure_one()
        if self.state not in ('pending', 'error'):
            return
        self.write({'state': 'processing'})
        try:
            record = self.env[self.model_name].browse(self.record_id)
            if not record.exists():
                raise ValueError('Registro no encontrado')

            if self.action == 'export' and hasattr(record, 'action_sync_to_wc'):
                record.with_context(wc_no_sync=True).action_sync_to_wc()
            elif self.action == 'import' and hasattr(record, 'action_sync_from_wc'):
                record.with_context(wc_no_sync=True).action_sync_from_wc()
            else:
                payload = json.loads(self.data or '{}')
                method_name = payload.get('method')
                method_args = payload.get('args', [])
                method_kwargs = payload.get('kwargs', {})
                if method_name and hasattr(record, method_name):
                    getattr(record.with_context(wc_no_sync=True), method_name)(*method_args, **method_kwargs)
                else:
                    raise ValueError('No se encontró método de sincronización')

            self.write({'state': 'done', 'error_message': False, 'date_processed': fields.Datetime.now()})
        except Exception as exc:
            _logger.exception('Error procesando trabajo WooCommerce %s', self.id)
            traceback_text = traceback.format_exc()
            retries = self.retry_count + 1
            state = 'pending' if retries < 3 else 'error'
            self.write({
                'state': state,
                'retry_count': retries,
                'error_message': str(exc),
                'date_processed': fields.Datetime.now() if state == 'error' else False,
            })
            if state == 'error':
                backend = self._get_backend_for_alert(record=self.env[self.model_name].browse(self.record_id))
                if backend:
                    backend._send_alert_email(
                        subject=f'[WooCommerce] Job en error #{self.id}: {self.name}',
                        body=(
                            f'Se detectó un error final en la cola de WooCommerce.\n\n'
                            f'Job id: {self.id}\n'
                            f'Nombre: {self.name}\n'
                            f'Modelo: {self.model_name}\n'
                            f'Record ID: {self.record_id}\n'
                            f'Acción: {self.action}\n'
                            f'Prioridad: {self.priority}\n'
                            f'Reintentos: {retries}\n'
                            f'Error: {exc}\n\n'
                            f'Traceback:\n{traceback_text}'
                        ),
                    )

    @api.model
    def _get_backend_for_alert(self, record=None):
        if record and hasattr(record, '_get_wc_backend'):
            backend = record._get_wc_backend()
            if backend:
                return backend
        if record and 'backend_id' in record._fields and record.backend_id:
            return record.backend_id
        return self.env['wc.backend'].search([], limit=1)

    @api.model
    def _auto_cleanup(self):
        """Elimina trabajos completados o cancelados con más de 7 días."""
        threshold = fields.Datetime.now() - timedelta(days=7)
        old_jobs = self.search([
            ('state', 'in', ['done', 'cancelled']),
            ('date_processed', '<', threshold),
        ])
        old_jobs.unlink()

    def action_retry(self):
        """Reintenta un trabajo fallido."""
        for job in self:
            job.write({'state': 'pending', 'error_message': False, 'retry_count': 0})

    def action_cancel(self):
        """Cancela un trabajo pendiente o en error."""
        for job in self:
            if job.state in ('pending', 'error'):
                job.write({'state': 'cancelled'})
