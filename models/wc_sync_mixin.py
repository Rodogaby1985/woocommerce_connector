import json
from datetime import timedelta
from typing import Any, Dict, Iterable, Optional

from odoo import fields, models


class WcSyncMixin(models.AbstractModel):
    _name = 'wc.sync.mixin'
    _description = 'WooCommerce Sync Mixin'

    """Mixin Python puro para lógica de sync WooCommerce en modelos Odoo.

    Este mixin asume que se usa junto a modelos Odoo (`models.Model`) que
    proveen `self.env` y comportamiento de recordsets.
    """

    def _get_wc_backend(self):
        """Obtiene el backend activo de WooCommerce."""
        return self.env['wc.backend'].search([], limit=1)

    def _enqueue_wc_job(self, action: str, priority: int = 5, data: Optional[Dict[str, Any]] = None):
        """Encola un trabajo para procesamiento asíncrono."""
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

    def _is_wc_sync_disabled(self) -> bool:
        """Retorna True si el contexto actual desactiva el encolado de sync."""
        return bool(self.env.context.get('wc_no_sync'))

    def _wc_field_changed(self, vals: Dict[str, Any], watched_fields: Iterable[str]) -> bool:
        """Detecta si cambió alguno de los campos monitoreados."""
        return any(field in vals for field in watched_fields)

    def _wc_recent_sync(self, sync_date_field: str) -> bool:
        """Evita loops por sincronizaciones recientes (<30 segundos)."""
        cooldown = fields.Datetime.now() - timedelta(seconds=30)
        return any(getattr(record, sync_date_field) and getattr(record, sync_date_field) >= cooldown for record in self)
