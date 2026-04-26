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

    prepare_stockable = fields.Boolean(
        string='Preparar productos como Bienes (sin importar stock)',
        default=True,
        help='Para cada producto/variación donde Woo tenga manage_stock=True, '
             'establece el producto en Odoo como Bienes (type=product) y activa '
             'el rastreo de inventario. No importa cantidades — eso se hace en el '
             'wizard "Importar Stock desde Woo".',
    )
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
    relink_by_sku = fields.Boolean(
        string='Relinkear por SKU si no hay ID Woo',
        default=True,
        help=(
            'Cuando está activo, si no se encuentra un producto por su ID de Woo, '
            'se busca uno existente con el mismo SKU (referencia interna) antes de crear uno nuevo. '
            'Útil para reiniciar la sincronización sin duplicar productos.'
        ),
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

    def _prepare_product_stockable(self, template, wc_product, variations=None):
        """Para productos con manage_stock=True en Woo, configura en Odoo como
        Bienes (type='product') para permitir rastreo de inventario.

        Nunca importa cantidades; solo prepara el producto para que luego
        el wizard de stock pueda crear quants sin errores.
        Errores individuales se capturan y loguean sin abortar la sync.
        """
        log_lines = []
        product_type = wc_product.get('type')

        def _set_storable(tmpl):
            try:
                if tmpl.type != 'product':
                    tmpl.with_context(wc_no_sync=True).write({'type': 'product'})
                    return True
            except Exception as exc:
                sku = tmpl.default_code or str(tmpl.id)
                _logger.warning('No se pudo preparar como Bienes el template %s: %s', sku, exc)
                log_lines.append(
                    'Advertencia: no se pudo preparar como Bienes el producto %s: %s' % (sku, exc)
                )
            return False

        if product_type == 'variable' and variations:
            for variation in variations:
                if not variation.get('manage_stock'):
                    continue
                if _set_storable(template):
                    sku = template.default_code or str(template.id)
                    log_lines.append(
                        'Preparado como Bienes (manage_stock=True en Woo): %s' % sku
                    )
                    break
        elif product_type != 'variable':
            if wc_product.get('manage_stock'):
                if _set_storable(template):
                    sku = template.default_code or str(template.id)
                    log_lines.append(
                        'Preparado como Bienes (manage_stock=True en Woo): %s' % sku
                    )

        return log_lines

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

        if not template:
            template = self.env['product.template'].create(
                {'name': wc_product.get('name') or 'Producto WooCommerce'}
            )
        return template

    def _prepare_stockable_for_product(self, template, wc_product, variations=None):
        """Prepara el producto como almacenable si manage_stock está activo en Woo.

        Solo cambia el tipo a 'product' (almacenable); no importa cantidades.
        Usa contexto wc_no_sync=True para evitar loops de exportación.
        Captura excepciones por producto para no abortar la sincronización completa.
        Retorna True si se realizó algún cambio.
        """
        needs_stockable = False
        product_type = wc_product.get('type')

        if product_type == 'variable' and variations:
            needs_stockable = any(v.get('manage_stock') for v in variations)
        elif product_type != 'variable':
            needs_stockable = bool(wc_product.get('manage_stock'))

        if not needs_stockable:
            return False

        try:
            if template.type != 'product':
                template.with_context(wc_no_sync=True).write({'type': 'product'})
                _logger.info(
                    'Producto %s (Woo ID %s) configurado como almacenable.',
                    template.display_name,
                    template.wc_id,
                )
            return True
        except Exception as exc:
            _logger.warning(
                'Error preparando stockable para producto %s (Woo ID %s): %s',
                template.display_name,
                wc_product.get('id'),
                exc,
            )
            return False

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
                stockable_prepared = 0
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
                        try:
                            template = self._find_or_create_template(wc_product)
                            template._process_wc_data(wc_product)
                            processed += 1
                            variations_data = None
                            if wc_product.get('type') == 'variable':
                                variable_products += 1
                                variation_stats = template._sync_variable_product_from_wc(wc_product)
                                total_variants += variation_stats.get('imported', 0)
                                synced_variants += variation_stats.get('mapped', 0)
                                variations_data = variation_stats.get('variations')
                                self.write({
                                    'log': (self.log or '') + '\nProducto variable %s (Woo ID %s): %s variaciones importadas, %s mapeadas.' % (
                                        template.display_name,
                                        template.wc_id,
                                        variation_stats.get('imported', 0),
                                        variation_stats.get('mapped', 0),
                                    )
                                })
                            if self.prepare_stockable:
                                if self._prepare_stockable_for_product(
                                    template, wc_product, variations=variations_data
                                ):
                                    stockable_prepared += 1
                        except Exception as exc:
                            _logger.exception(
                                'Error procesando producto Woo ID %s: %s',
                                wc_product.get('id'),
                                exc,
                            )
                            self.write({
                                'log': (self.log or '') + '\nError en producto Woo ID %s (%s): %s' % (
                                    wc_product.get('id'),
                                    wc_product.get('name', ''),
                                    exc,
                                )
                            })
                            if self.prepare_stockable:
                                prep_lines = self._prepare_product_stockable(
                                    template, wc_product,
                                    variations=variation_stats.get('variations'),
                                )
                                if prep_lines:
                                    self.write({'log': (self.log or '') + '\n' + '\n'.join(prep_lines)})
                            if self.import_stock:
                                try:
                                    stock_count, stock_lines = self._import_stock_for_product(
                                        template, wc_product,
                                        variations=variation_stats.get('variations'),
                                    )
                                    total_stock_imported += stock_count
                                    if stock_lines:
                                        self.write({'log': (self.log or '') + '\n' + '\n'.join(stock_lines)})
                                except Exception as stock_exc:
                                    _logger.warning(
                                        'Error importando stock para %s (Woo ID %s): %s',
                                        template.display_name, template.wc_id, stock_exc,
                                    )
                                    self.write({'log': (self.log or '') + '\nAdvertencia stock %s: %s' % (
                                        template.display_name, stock_exc,
                                    )})
                        else:
                            if self.prepare_stockable:
                                prep_lines = self._prepare_product_stockable(template, wc_product)
                                if prep_lines:
                                    self.write({'log': (self.log or '') + '\n' + '\n'.join(prep_lines)})
                            if self.import_stock:
                                try:
                                    stock_count, stock_lines = self._import_stock_for_product(
                                        template, wc_product,
                                    )
                                    total_stock_imported += stock_count
                                    if stock_lines:
                                        self.write({'log': (self.log or '') + '\n' + '\n'.join(stock_lines)})
                                except Exception as stock_exc:
                                    _logger.warning(
                                        'Error importando stock para %s: %s',
                                        template.display_name, stock_exc,
                                    )
                                    self.write({'log': (self.log or '') + '\nAdvertencia stock %s: %s' % (
                                        template.display_name, stock_exc,
                                    )})
                        if product_limit and processed >= product_limit:
                            break
                    if product_limit and processed >= product_limit:
                        break
                    page += 1

                stock_summary = ''
                if self.prepare_stockable:
                    stock_summary = 'Productos preparados como Bienes (manage_stock=True). '
                if self.import_stock:
                    stock_summary += 'Stock inicial importado: %s variantes/productos ajustados.' % total_stock_imported
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
                                             'Para importar stock use el wizard "Importar Stock desde Woo". '
                                             'Próxima ejecución desde página: %s.' % (
                                                 processed,
                                                 variable_products,
                                                 total_variants,
                                                 synced_variants,
                                                 stockable_summary,
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
