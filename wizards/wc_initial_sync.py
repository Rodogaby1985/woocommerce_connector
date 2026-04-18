import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class WcInitialSync(models.TransientModel):
    _name = 'wc.initial.sync'
    _description = 'Wizard de sincronización inicial WooCommerce'

    sync_products = fields.Boolean(string='Sincronizar productos', default=True)
    sync_customers = fields.Boolean(string='Sincronizar clientes', default=True)
    sync_orders = fields.Boolean(string='Sincronizar pedidos', default=False)
    batch_size = fields.Integer(string='Tamaño de lote', default=50)
    state = fields.Selection([
        ('config', 'Configuración'),
        ('running', 'Ejecutando'),
        ('done', 'Completado'),
    ], default='config')
    progress = fields.Float(string='Progreso (%)', default=0.0)
    total_products = fields.Integer(string='Total productos', default=0)
    synced_products = fields.Integer(string='Productos sincronizados', default=0)
    total_variants = fields.Integer(string='Total variantes', default=0)
    synced_variants = fields.Integer(string='Variantes sincronizadas', default=0)
    log = fields.Text(string='Log')

    def action_start_sync(self):
        """Arranca el proceso inicial y procesa el primer lote."""
        self.ensure_one()
        self.write({'state': 'running', 'log': 'Iniciando sincronización...'})
        return self.action_sync_batch()

    def action_sync_batch(self):
        """Procesa un lote de productos y encola siguientes importaciones."""
        self.ensure_one()
        backend = self.env['wc.backend'].search([], limit=1)
        if not backend:
            self.write({'log': (self.log or '') + '\nBackend no configurado.'})
            return False

        try:
            if self.sync_products:
                page = 1
                processed = 0
                total_variants = self.synced_variants
                while True:
                    products = backend._wc_get('products', params={'per_page': 100, 'page': page})
                    if not products:
                        break
                    with self.env.cr.savepoint():
                        for wc_product in products:
                            template = self.env['product.template'].search([('wc_id', '=', wc_product.get('id'))], limit=1)
                            if not template:
                                template = self.env['product.template'].create({'name': wc_product.get('name') or 'Producto WooCommerce'})
                            template._process_wc_data(wc_product)
                            processed += 1
                            if wc_product.get('type') == 'variable':
                                template._sync_variable_product_from_wc(wc_product)
                                total_variants += len(template.product_variant_ids)
                            if processed % max(self.batch_size, 1) == 0:
                                self.env.cr.commit()
                    page += 1

                self.write({
                    'total_products': processed,
                    'synced_products': processed,
                    'total_variants': total_variants,
                    'synced_variants': total_variants,
                })

            if self.sync_customers:
                page = 1
                while True:
                    customers = backend._wc_get('customers', params={'per_page': 100, 'page': page})
                    if not customers:
                        break
                    with self.env.cr.savepoint():
                        for customer in customers:
                            self.env['res.partner']._get_or_create_from_wc(customer)
                    self.env.cr.commit()
                    page += 1

            if self.sync_orders:
                page = 1
                while True:
                    orders = backend._wc_get('orders', params={'per_page': 100, 'page': page})
                    if not orders:
                        break
                    with self.env.cr.savepoint():
                        for order in orders:
                            self.env['sale.order']._process_wc_order(order)
                    self.env.cr.commit()
                    page += 1

            self.write({'state': 'done', 'progress': 100.0, 'log': (self.log or '') + '\nSincronización finalizada.'})
        except Exception as exc:
            _logger.exception('Error en sync inicial WooCommerce')
            self.write({'log': (self.log or '') + f'\nError: {exc}'})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'wc.initial.sync',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
