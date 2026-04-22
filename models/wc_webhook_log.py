from datetime import timedelta

from odoo import api, fields, models


class WcWebhookLog(models.Model):
    _name = 'wc.webhook.log'
    _description = 'Registro de Webhooks WooCommerce'
    _order = 'id desc'

    backend_id = fields.Many2one('wc.backend', string='Backend', required=True, ondelete='cascade', index=True)
    client_ip = fields.Char(string='IP cliente', required=True, index=True)

    @api.model
    def _cleanup_old_logs(self, retention_days: int = 2):
        threshold = fields.Datetime.now() - timedelta(days=retention_days)
        old_logs = self.search([('create_date', '<', threshold)], limit=2000)
        if old_logs:
            old_logs.unlink()

    @api.model
    def _cron_cleanup_old_logs(self):
        self._cleanup_old_logs()
