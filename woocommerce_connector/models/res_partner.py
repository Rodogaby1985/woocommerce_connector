from odoo import fields, models
from .wc_sync_mixin import WcSyncMixin


class ResPartner(WcSyncMixin, models.Model):
    _inherit = 'res.partner'

    wc_customer_id = fields.Integer(string='ID Cliente WooCommerce', index=True)
    wc_sync_date = fields.Datetime(string='Última sync WooCommerce')

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
