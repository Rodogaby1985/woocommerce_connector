import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class WcResetSync(models.TransientModel):
    _name = 'wc.reset.sync'
    _description = 'Wizard para reiniciar la sincronización WooCommerce'

    scope = fields.Selection([
        ('all', 'Todos los productos vinculados a Woo'),
        ('sku', 'Solo productos con SKU (referencia interna)'),
    ], string='Alcance', default='all', required=True)
    confirm = fields.Boolean(
        string='Confirmo que quiero limpiar los vínculos con WooCommerce',
        default=False,
    )
    log = fields.Text(string='Resultado', readonly=True)
    state = fields.Selection([
        ('confirm', 'Confirmar'),
        ('done', 'Completado'),
    ], default='confirm')

    def action_reset(self):
        """Limpia los campos de vínculo WooCommerce de los productos.

        Requiere confirmación explícita.
        Limpia: wc_id, wc_sync_date en product.template y
                wc_variation_id, wc_variant_sync_date en product.product.
        """
        self.ensure_one()
        if not self.confirm:
            self.write({
                'log': 'Debe marcar la casilla de confirmación para continuar.',
            })
            return self._return_wizard()

        log_lines = []

        domain = [('wc_id', '!=', False)]
        if self.scope == 'sku':
            domain += [('default_code', '!=', False), ('default_code', '!=', '')]

        templates = self.env['product.template'].search(domain)
        template_count = len(templates)
        if templates:
            try:
                templates.with_context(wc_no_sync=True).write({
                    'wc_id': 0,
                    'wc_sync_date': False,
                })
                log_lines.append(
                    '%s plantillas de producto desvinculadas (wc_id y wc_sync_date limpiados).' % template_count
                )
            except Exception as exc:
                _logger.exception('Error limpiando wc_id en plantillas: %s', exc)
                log_lines.append('Error limpiando plantillas: %s' % exc)

        variant_domain = [('wc_variation_id', '!=', False)]
        if self.scope == 'sku':
            variant_domain += [('default_code', '!=', False), ('default_code', '!=', '')]

        variants = self.env['product.product'].search(variant_domain)
        variant_count = len(variants)
        if variants:
            try:
                variants.with_context(wc_no_sync=True).write({
                    'wc_variation_id': 0,
                    'wc_variant_sync_date': False,
                })
                log_lines.append(
                    '%s variantes desvinculadas (wc_variation_id y wc_variant_sync_date limpiados).' % variant_count
                )
            except Exception as exc:
                _logger.exception('Error limpiando wc_variation_id en variantes: %s', exc)
                log_lines.append('Error limpiando variantes: %s' % exc)

        if not templates and not variants:
            log_lines.append('No se encontraron productos vinculados con WooCommerce.')

        log_lines.append(
            'Ahora puede ejecutar el Sync Inicial nuevamente. '
            'Active "Relinkear por SKU" para vincular por referencia interna sin duplicar.'
        )

        self.write({
            'state': 'done',
            'log': '\n'.join(log_lines),
        })
        return self._return_wizard()

    def _return_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'wc.reset.sync',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
