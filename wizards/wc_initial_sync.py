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
    product_limit = fields.Integer(string='Límite de productos (0 = Todos)', default=0)
    start_page = fields.Integer(string='Desde página', default=1)
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

    import_stock = fields.Boolean(
        string='Importar stock inicial desde Woo',
        default=False,
    )
    stock_location_id = fields.Many2one(
        'stock.location',
        string='Ubicación destino stock',
        domain=[('usage', '=', 'internal')],
        default=lambda self: self._default_stock_location(),
    )
    overwrite_stock = fields.Boolean(
        string='Pisar stock existente (Woo es fuente de verdad)',
        default=True,
    )

    @api.model
    def _default_stock_location(self):
        """Retorna WH/Stock del primer almacén de la compañía activa."""
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1
        )
        if warehouse and warehouse.lot_stock_id:
            return warehouse.lot_stock_id
        return self.env['stock.location'].search([('usage', '=', 'internal')], limit=1)

    def _apply_stock_to_variant(self, variant, location, target_qty):
        """Aplica cantidad de stock a una variante en la ubicación dada.

        Si overwrite_stock es True, ajusta exactamente al valor de Woo.
        Si overwrite_stock es False, solo actúa si no hay stock existente.
        Retorna True si se realizó algún ajuste.
        """
        quants = self.env['stock.quant'].search([
            ('product_id', '=', variant.id),
            ('location_id', '=', location.id),
        ])
        current_qty = sum(q.quantity for q in quants)
        if not self.overwrite_stock and current_qty > 0:
            return False
        delta = float(target_qty) - current_qty
        if delta == 0.0:
            return False
        self.env['stock.quant'].with_context(wc_no_sync=True)._update_available_quantity(
            variant, location, delta
        )
        return True

    def _import_stock_for_product(self, template, wc_product, variations=None):
        """Importa stock desde WooCommerce para un producto y sus variantes.

        Retorna (count, log_lines) donde count es el número de variantes ajustadas.
        Solo actúa cuando manage_stock=True en Woo para cada producto/variación.
        Usa contexto wc_no_sync=True para evitar encolar jobs de exportación.
        """
        location = self.stock_location_id
        if not location:
            return 0, []

        stock_count = 0
        log_lines = []
        location_name = location.complete_name or location.name
        product_type = wc_product.get('type')

        if product_type == 'variable' and variations:
            for variation in variations:
                if not variation.get('manage_stock'):
                    continue
                wc_qty = variation.get('stock_quantity') if variation.get('stock_quantity') is not None else 0
                variant = self.env['product.product'].search(
                    [('wc_variation_id', '=', variation.get('id'))], limit=1
                )
                if not variant:
                    continue
                if self._apply_stock_to_variant(variant, location, wc_qty):
                    sku = variant.default_code or str(variant.id)
                    log_lines.append(
                        'Stock importado: %s: %s unidades en %s' % (sku, int(wc_qty), location_name)
                    )
                    stock_count += 1
        elif product_type != 'variable':
            if wc_product.get('manage_stock'):
                wc_qty = wc_product.get('stock_quantity') if wc_product.get('stock_quantity') is not None else 0
                variant = template.product_variant_id
                if variant and self._apply_stock_to_variant(variant, location, wc_qty):
                    sku = (
                        variant.default_code
                        or template.default_code
                        or str(variant.id)
                    )
                    log_lines.append(
                        'Stock importado: %s: %s unidades en %s' % (sku, int(wc_qty), location_name)
                    )
                    stock_count += 1

        return stock_count, log_lines

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
                page = self.start_page if self.start_page > 0 else 1
                processed = 0
                total_variants = 0
                synced_variants = 0
                variable_products = 0
                total_stock_imported = 0
                product_limit = self.product_limit if self.product_limit > 0 else None
                self.write({'log': (self.log or '') + '\nIniciando sync de productos desde página %s%s.' % (
                    page,
                    ' con límite %s' % product_limit if product_limit else '',
                )})
                while True:
                    products = backend._wc_get('products', params={'per_page': 100, 'page': page})
                    if not products:
                        break
                    self.write({'log': (self.log or '') + '\nProcesando página %s (%s productos)...' % (page, len(products))})
                    for wc_product in products:
                        template = self.env['product.template'].search([('wc_id', '=', wc_product.get('id'))], limit=1)
                        if not template:
                            template = self.env['product.template'].create({'name': wc_product.get('name') or 'Producto WooCommerce'})
                        template._process_wc_data(wc_product)
                        processed += 1
                        if wc_product.get('type') == 'variable':
                            variable_products += 1
                            variation_stats = template._sync_variable_product_from_wc(wc_product)
                            total_variants += variation_stats.get('imported', 0)
                            synced_variants += variation_stats.get('mapped', 0)
                            self.write({
                                'log': (self.log or '') + '\nProducto variable %s (Woo ID %s): %s variaciones importadas, %s mapeadas.' % (
                                    template.display_name,
                                    template.wc_id,
                                    variation_stats.get('imported', 0),
                                    variation_stats.get('mapped', 0),
                                )
                            })
                            if self.import_stock:
                                stock_count, stock_lines = self._import_stock_for_product(
                                    template, wc_product,
                                    variations=variation_stats.get('variations'),
                                )
                                total_stock_imported += stock_count
                                if stock_lines:
                                    self.write({'log': (self.log or '') + '\n' + '\n'.join(stock_lines)})
                        elif self.import_stock:
                            stock_count, stock_lines = self._import_stock_for_product(
                                template, wc_product,
                            )
                            total_stock_imported += stock_count
                            if stock_lines:
                                self.write({'log': (self.log or '') + '\n' + '\n'.join(stock_lines)})
                        if product_limit and processed >= product_limit:
                            break
                    if product_limit and processed >= product_limit:
                        break
                    page += 1

                stock_summary = ''
                if self.import_stock:
                    stock_summary = 'Stock inicial importado: %s variantes/productos ajustados.' % total_stock_imported
                    if self.stock_location_id:
                        stock_summary += ' Ubicación: %s.' % (
                            self.stock_location_id.complete_name or self.stock_location_id.name
                        )
                    stock_summary += ' '
                self.write({
                    'total_products': processed,
                    'synced_products': processed,
                    'total_variants': total_variants,
                    'synced_variants': synced_variants,
                    'log': (self.log or '') + '\nSincronización de productos finalizada. '
                                             '%s productos importados (%s variables). '
                                             '%s variaciones importadas, %s mapeadas. '
                                             '%s'
                                             'Próxima ejecución desde página: %s.' % (
                                                 processed,
                                                 variable_products,
                                                 total_variants,
                                                 synced_variants,
                                                 stock_summary,
                                                 page,
                                             ),
                })

            if self.sync_customers:
                page = 1
                while True:
                    customers = backend._wc_get('customers', params={'per_page': 100, 'page': page})
                    if not customers:
                        break
                    for customer in customers:
                        self.env['res.partner']._get_or_create_from_wc(customer)
                    page += 1

            if self.sync_orders:
                page = 1
                while True:
                    orders = backend._wc_get('orders', params={'per_page': 100, 'page': page})
                    if not orders:
                        break
                    for order in orders:
                        self.env['sale.order']._process_wc_order(order)
                    page += 1

            self.write({'state': 'done', 'progress': 100.0, 'log': (self.log or '') + '\nSincronización finalizada.'})
        except Exception as exc:
            _logger.exception('Error en sync inicial WooCommerce')
            self.write({'log': (self.log or '') + '\nError: %s' % exc})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'wc.initial.sync',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
