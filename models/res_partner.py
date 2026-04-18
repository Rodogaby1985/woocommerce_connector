import json
from datetime import timedelta
from typing import Any, Dict, Iterable, Optional

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    wc_customer_id = fields.Integer(string='ID Cliente WooCommerce', index=True)
    wc_sync_date = fields.Datetime(string='Última sync WooCommerce')

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

    def _get_or_create_from_wc(self, wc_customer_data: dict):
        """Busca cliente por email y crea/actualiza si corresponde."""
        email = wc_customer_data.get('email') or wc_customer_data.get('billing', {}).get('email')
        partner = self.search([('email', '=', email)], limit=1) if email else self.search([], limit=0)
        vals = {
            'name': f"{wc_customer_data.get('first_name', '')} {wc_customer_data.get('last_name', '')}".strip() or email or 'Cliente WooCommerce',
            'email': email,
            'phone': wc_customer_data.get('billing', {}).get('phone') or wc_customer_data.get('phone'),
            'street': wc_customer_data.get('billing', {}).get('address_1'),
            'city': wc_customer_data.get('billing', {}).get('city'),
            'zip': wc_customer_data.get('billing', {}).get('postcode'),
            'wc_customer_id': wc_customer_data.get('id'),
            'wc_sync_date': fields.Datetime.now(),
            'customer_rank': 1,
        }
        if partner:
            partner.with_context(wc_no_sync=True).write(vals)
            return partner
        return self.with_context(wc_no_sync=True).create(vals)

    def _prepare_wc_customer_data(self) -> dict:
        """Prepara payload de cliente para WooCommerce."""
        self.ensure_one()
        first_name, last_name = '', ''
        if self.name:
            parts = self.name.split(' ', 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ''
        return {
            'email': self.email,
            'first_name': first_name,
            'last_name': last_name,
            'billing': {
                'first_name': first_name,
                'last_name': last_name,
                'address_1': self.street or '',
                'city': self.city or '',
                'postcode': self.zip or '',
                'phone': self.phone or '',
                'email': self.email or '',
            },
        }

    def action_sync_to_wc(self):
        backend = self._get_wc_backend()
        if not backend:
            return
        for partner in self.filtered(lambda p: p.email):
            data = partner._prepare_wc_customer_data()
            if partner.wc_customer_id:
                response = backend._wc_put(f'customers/{partner.wc_customer_id}', data)
            else:
                response = backend._wc_post('customers', data)
            partner.with_context(wc_no_sync=True).write({
                'wc_customer_id': response.get('id') or partner.wc_customer_id,
                'wc_sync_date': fields.Datetime.now(),
            })

    def action_sync_from_wc(self):
        backend = self._get_wc_backend()
        if not backend:
            return
        for partner in self.filtered('wc_customer_id'):
            response = backend._wc_get(f'customers/{partner.wc_customer_id}')
            partner._get_or_create_from_wc(response)
