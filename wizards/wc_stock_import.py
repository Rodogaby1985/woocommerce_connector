import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class WcStockImport(models.TransientModel):
    _name = 'wc.stock.import'
    _description = 'Importar Stock desde WooCommerce (Woo → Odoo)'

    stock_location_id = fields.Many2one(
        'stock.location',
        string='Ubicación destino stock',
        domain=[('usage', '=', 'internal')],
        required=True,
        default=lambda self: self._default_stock_location(),
    )
    overwrite_stock = fields.Boolean(
        string='Pisar stock existente (Woo es fuente de verdad)',
        default=True,
        help='Si está activo, el stock en Odoo se ajusta exactamente al valor de Woo. '
             'Si está inactivo, solo se aplica cuando no hay stock en Odoo.',
    )
    batch_size = fields.Integer(string='Tamaño de lote', default=50)
    product_limit = fields.Integer(string='Límite de productos (0 = Todos)', default=0)
    start_page = fields.Integer(string='Desde página', default=1)
    state = fields.Selection([
        ('config', 'Configuración'),
        ('running', 'Ejecutando'),
        ('done', 'Completado'),
    ], default='config')
    log = fields.Text(string='Log')
    products_processed = fields.Integer(string='Productos procesados', default=0)
    variants_adjusted = fields.Integer(string='Variantes ajustadas', default=0)

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

        Retorna (count, log_lines). Solo actúa cuando manage_stock=True en Woo.
        Errores individuales se capturan y loguean sin abortar el proceso.
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
                wc_qty = float(variation.get('stock_quantity')) if variation.get('stock_quantity') is not None else 0.0
                try:
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
                except Exception as exc:
                    var_id = variation.get('id') or '?'
                    _logger.warning(
                        'Error aplicando stock para variación Woo ID %s: %s', var_id, exc
                    )
                    log_lines.append(
                        'Advertencia: error en variación Woo ID %s: %s' % (var_id, exc)
                    )
        elif product_type != 'variable':
            if wc_product.get('manage_stock'):
                wc_qty = float(wc_product.get('stock_quantity')) if wc_product.get('stock_quantity') is not None else 0.0
                try:
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
                except Exception as exc:
                    sku = template.default_code or str(template.id)
                    _logger.warning(
                        'Error aplicando stock para producto %s: %s', sku, exc
                    )
                    log_lines.append(
                        'Advertencia: error en producto %s: %s' % (sku, exc)
                    )

        return stock_count, log_lines

    def action_start_import(self):
        """Inicia la importación de stock desde WooCommerce."""
        self.ensure_one()
        self.write({'state': 'running', 'log': 'Iniciando importación de stock desde WooCommerce...'})
        return self.action_import_batch()

    def action_import_batch(self):
        """Importa stock de todos los productos de Woo por páginas."""
        self.ensure_one()
        backend = self.env['wc.backend'].search([], limit=1)
        if not backend:
            self.write({'log': (self.log or '') + '\nBackend no configurado.'})
            return self._return_action()

        try:
            page = self.start_page if self.start_page > 0 else 1
            processed = 0
            total_adjusted = 0
            product_limit = self.product_limit if self.product_limit > 0 else None
            location_name = (
                self.stock_location_id.complete_name or self.stock_location_id.name
                if self.stock_location_id else '(sin ubicación)'
            )
            self.write({'log': (self.log or '') + '\nUbicación destino: %s. Desde página %s%s.' % (
                location_name,
                page,
                ' con límite %s' % product_limit if product_limit else '',
            )})

            while True:
                products = backend._wc_get('products', params={'per_page': 100, 'page': page})
                if not products:
                    break
                self.write({'log': (self.log or '') + '\nProcesando página %s (%s productos)...' % (
                    page, len(products)
                )})
                for wc_product in products:
                    try:
                        template = self.env['product.template'].search(
                            [('wc_id', '=', wc_product.get('id'))], limit=1
                        )
                        if not template:
                            _logger.info(
                                'Producto Woo ID %s no encontrado en Odoo, saltando.',
                                wc_product.get('id'),
                            )
                            processed += 1
                            if product_limit and processed >= product_limit:
                                break
                            continue

                        variations = None
                        if wc_product.get('type') == 'variable':
                            woo_id = wc_product.get('id')
                            variations = backend._wc_get(
                                'products/%s/variations' % woo_id,
                                params={'per_page': 100, 'page': 1},
                            ) or []

                        stock_count, stock_lines = self._import_stock_for_product(
                            template, wc_product, variations=variations
                        )
                        total_adjusted += stock_count
                        if stock_lines:
                            self.write({'log': (self.log or '') + '\n' + '\n'.join(stock_lines)})
                    except Exception as exc:
                        prod_name = wc_product.get('name') or wc_product.get('id') or '?'
                        _logger.warning('Error procesando producto %s durante import de stock: %s', prod_name, exc)
                        self.write({'log': (self.log or '') + '\nAdvertencia: error en %s: %s' % (prod_name, exc)})
                    processed += 1
                    if product_limit and processed >= product_limit:
                        break
                if product_limit and processed >= product_limit:
                    break
                page += 1

            self.write({
                'state': 'done',
                'products_processed': processed,
                'variants_adjusted': total_adjusted,
                'log': (self.log or '') + '\nImportación de stock finalizada. '
                                         '%s productos procesados. '
                                         '%s variantes/productos ajustados en %s.' % (
                                             processed,
                                             total_adjusted,
                                             location_name,
                                         ),
            })
        except Exception as exc:
            _logger.exception('Error en importación de stock desde WooCommerce')
            self.write({'log': (self.log or '') + '\nError: %s' % exc})

        return self._return_action()

    def _return_action(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'wc.stock.import',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
