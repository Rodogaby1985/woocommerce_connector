import json
import logging
import traceback
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)
_MAX_TRACEBACK_SUMMARY_LINES = 12
_MAX_JOB_RETRIES = 3
_MAX_ERROR_MESSAGE_LENGTH = 1000


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
    next_attempt_at = fields.Datetime(string='Próximo intento', index=True)
    date_started = fields.Datetime(string='Fecha inicio')
    date_done = fields.Datetime(string='Fecha fin')
    date_processed = fields.Datetime(string='Fecha procesado')

    @api.model
    def _process_pending_jobs(self, limit: int = 50):
        """Procesa trabajos pendientes con locking SQL para multi-worker."""
        self.env.cr.execute(
            """
                WITH candidate AS (
                    SELECT id
                    FROM wc_queue_job
                    WHERE state IN ('pending', 'error')
                      AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
                    ORDER BY priority ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE wc_queue_job AS job
                SET state = 'processing',
                    date_started = %s
                FROM candidate
                WHERE job.id = candidate.id
                RETURNING job.id
            """,
            [fields.Datetime.now(), int(limit), fields.Datetime.now()],
        )
        job_ids = [row[0] for row in self.env.cr.fetchall()]
        jobs = self.browse(job_ids)
        for job in jobs:
            job._process_job()

    def _process_job(self):
        """Ejecuta un trabajo individual de sincronización."""
        self.ensure_one()
        if self.state not in ('pending', 'error', 'processing'):
            return
        if self.state != 'processing':
            self.write({
                'state': 'processing',
                'date_started': fields.Datetime.now(),
            })
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

            now = fields.Datetime.now()
            self.write({
                'state': 'done',
                'error_message': False,
                'next_attempt_at': False,
                'date_done': now,
                'date_processed': now,
            })
        except Exception as exc:
            _logger.exception('Error procesando trabajo WooCommerce %s', self.id)
            traceback_text = traceback.format_exc()
            retries = self.retry_count + 1
            state = 'pending' if retries < _MAX_JOB_RETRIES else 'error'
            delay_seconds = min(3600, 60 * (2 ** max(retries - 1, 0)))
            now = fields.Datetime.now()
            self.write({
                'state': state,
                'retry_count': retries,
                'error_message': str(exc)[:_MAX_ERROR_MESSAGE_LENGTH],
                'next_attempt_at': now + timedelta(seconds=delay_seconds) if state == 'pending' else False,
                'date_done': now if state == 'error' else False,
                'date_processed': now if state == 'error' else False,
            })
            if state == 'error':
                record_for_alert = self.env[self.model_name].browse(self.record_id)
                backend = self._get_backend_for_alert(record=record_for_alert)
                if backend:
                    operation = self._get_operation_label(record_for_alert=record_for_alert)
                    traceback_summary = self._summarize_traceback(traceback_text)
                    sku = getattr(record_for_alert, 'default_code', None)
                    if sku in (None, ''):
                        sku = self._extract_identifier_from_payload('sku') or '-'
                    wc_id = getattr(record_for_alert, 'wc_id', None)
                    if wc_id in (None, ''):
                        wc_id = self._extract_identifier_from_payload('wc_id', 'product_id') or '-'
                    wc_variation_id = getattr(record_for_alert, 'wc_variation_id', None)
                    if wc_variation_id in (None, ''):
                        wc_variation_id = self._extract_identifier_from_payload('wc_variation_id', 'variation_id') or '-'
                    backend._send_alert_email(
                        subject=(
                            f'[WooCommerce][{self.env.cr.dbname}]'
                            f'[{backend.display_name}] {operation} - job en error'
                        ),
                        body=(
                            f'Se detectó un error final en la cola de WooCommerce.\n\n'
                            f'Fecha/Hora: {fields.Datetime.now()}\n'
                            f'Backend: {backend.display_name}\n'
                            f'Operación: {operation}\n'
                            f'Job id: {self.id}\n'
                            f'Nombre: {self.name}\n'
                            f'Modelo: {self.model_name}\n'
                            f'Record ID: {self.record_id}\n'
                            f'Acción: {self.action}\n'
                            f'Prioridad: {self.priority}\n'
                            f'Reintentos: {retries}\n'
                            f'SKU/default_code: {sku}\n'
                            f'wc_id: {wc_id}\n'
                            f'wc_variation_id: {wc_variation_id}\n'
                            f'Error: {exc}\n\n'
                            f'Resumen traceback:\n{traceback_summary}'
                        ),
                    )

    def _extract_identifier_from_payload(self, *keys):
        self.ensure_one()
        try:
            payload = json.loads(self.data or '{}')
        except Exception:
            return False
        for key in keys:
            value = payload.get(key)
            if value:
                return value
        return False

    def _summarize_traceback(self, traceback_text):
        lines = [line for line in (traceback_text or '').strip().splitlines() if line.strip()]
        if not lines:
            return 'Sin traceback disponible'
        return '\n'.join(lines[-_MAX_TRACEBACK_SUMMARY_LINES:])

    def _get_operation_label(self, record_for_alert=None):
        self.ensure_one()
        payload_method = self._extract_identifier_from_payload('method')
        model = self.model_name or '-'
        if model == 'product.product':
            return 'Exportar stock' if self.action == 'export' else 'Importar variación'
        if model == 'product.template':
            return 'Exportar producto' if self.action == 'export' else 'Importar producto'
        if model == 'sale.order':
            return 'Exportar pedido' if self.action == 'export' else 'Importar pedido'
        if payload_method:
            return f'{self.action or "sync"}::{payload_method}'
        if record_for_alert and getattr(record_for_alert, '_description', None):
            return f'{self.action or "sync"} {record_for_alert._description}'
        return f'{self.action or "sync"} {model}'

    @api.model
    def _get_backend_for_alert(self, record=None):
        if record and record._name == 'wc.backend':
            return record
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
        old_jobs = self.search([('state', 'in', ['done', 'cancelled'])]).filtered(
            lambda job: (
                (job.date_done and job.date_done < threshold)
                or (not job.date_done and job.date_processed and job.date_processed < threshold)
            )
        )
        old_jobs.unlink()

    def action_retry(self):
        """Reintenta un trabajo fallido."""
        for job in self:
            job.write({
                'state': 'pending',
                'error_message': False,
                'retry_count': 0,
                'next_attempt_at': False,
                'date_started': False,
                'date_done': False,
                'date_processed': False,
            })

    def action_cancel(self):
        """Cancela un trabajo pendiente o en error."""
        for job in self:
            if job.state in ('pending', 'error'):
                now = fields.Datetime.now()
                job.write({
                    'state': 'cancelled',
                    'next_attempt_at': False,
                    'date_done': now,
                    'date_processed': now,
                })
