import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class WcStockImportWizard(models.TransientModel):
    _name = 'wc.stock.import.wizard'
    _description = 'Wizard para importar stock desde WooCommerce a Odoo'

    backend_id = fields.Many2one(
        'wc.backend',
        string='Backend WooCommerce',
        required=True,
        default=lambda self: self.env['wc.backend'].search([], limit=1),
    )
    stock_location_id = fields.Many2one(
        'stock.location',
        string='Ubicación de stock',
        domain=[('usage', '=', 'internal')],
        required=True,
        default=lambda self: self._default_stock_location(),
    )
    overwrite_stock = fields.Boolean(
        string='Pisar stock existente (Woo es fuente de verdad)',
        default=True,
        help='Si está activo, ajusta el stock de Odoo exactamente al valor de Woo. '
             'Si no, solo aplica donde no haya stock en Odoo.',
    )
    start_page = fields.Integer(string='Desde página', default=1)
    product_limit = fields.Integer(string='Límite de productos (0 = Todos)', default=0)
    state = fields.Selection([
        ('config', 'Configuración'),
        ('running', 'Ejecutando'),
        ('done', 'Completado'),
    ], default='config')
    log = fields.Text(string='Log')
    total_adjusted = fields.Integer(string='Variantes/productos ajustados', default=0)
    total_skipped = fields.Integer(string='Omitidos (sin manage_stock o sin producto en Odoo)', default=0)

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

    def _import_stock_for_product(self, backend, wc_product):
        """Importa stock desde WooCommerce para un producto y sus variantes.

        Retorna (adjusted, skipped, log_lines).
        Solo actúa cuando manage_stock=True en Woo para cada producto/variación.
        Usa contexto wc_no_sync=True para evitar encolar jobs de exportación.
        Captura y registra errores sin abortar la importación completa.
        """
        location = self.stock_location_id
        location_name = location.complete_name or location.name
        product_type = wc_product.get('type')
        adjusted = 0
        skipped = 0
        log_lines = []

        if product_type == 'variable':
            wc_id = wc_product.get('id')
            if not wc_id:
                return 0, 0, []
            try:
                backend_batch_size = int(backend.batch_size) if backend.batch_size else 100
            except (TypeError, ValueError):
                backend_batch_size = 100
            page_size = min(max(backend_batch_size, 1), 100)
            page = 1
            variations = []
            while True:
                try:
                    page_vars = backend._wc_get(
                        'products/%s/variations' % wc_id,
                        params={'per_page': page_size, 'page': page},
                    ) or []
                except Exception as exc:
                    _logger.warning('Error obteniendo variaciones para Woo ID %s: %s', wc_id, exc)
                    break
                if not page_vars:
                    break
                variations.extend(page_vars)
                if len(page_vars) < page_size:
                    break
                page += 1

            for variation in variations:
                try:
                    if not variation.get('manage_stock'):
                        skipped += 1
                        continue
                    wc_qty = variation.get('stock_quantity')
                    if wc_qty is None:
                        wc_qty = 0
                    variant = self.env['product.product'].search(
                        [('wc_variation_id', '=', variation.get('id'))], limit=1
                    )
                    if not variant:
                        skipped += 1
                        continue
                    if self._apply_stock_to_variant(variant, location, wc_qty):
                        sku = variant.default_code or str(variant.id)
                        log_lines.append(
                            'Stock importado: %s → %s unidades en %s' % (sku, int(wc_qty), location_name)
                        )
                        adjusted += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    _logger.warning(
                        'Error importando stock para variación Woo ID %s: %s',
                        variation.get('id'),
                        exc,
                    )
                    log_lines.append(
                        'Error en variación Woo ID %s: %s' % (variation.get('id'), exc)
                    )
        else:
            try:
                if not wc_product.get('manage_stock'):
                    return 0, 1, []
                wc_qty = wc_product.get('stock_quantity')
                if wc_qty is None:
                    wc_qty = 0
                template = self.env['product.template'].search(
                    [('wc_id', '=', wc_product.get('id'))], limit=1
                )
                if not template:
                    return 0, 1, []
                variant = template.product_variant_id
                if not variant:
                    return 0, 1, []
                if self._apply_stock_to_variant(variant, location, wc_qty):
                    sku = variant.default_code or template.default_code or str(variant.id)
                    log_lines.append(
                        'Stock importado: %s → %s unidades en %s' % (sku, int(wc_qty), location_name)
                    )
                    adjusted += 1
                else:
                    skipped += 1
            except Exception as exc:
                _logger.warning(
                    'Error importando stock para producto Woo ID %s: %s',
                    wc_product.get('id'),
                    exc,
                )
                log_lines.append(
                    'Error en producto Woo ID %s: %s' % (wc_product.get('id'), exc)
                )

        return adjusted, skipped, log_lines

    def action_start_import(self):
        """Inicia la importación de stock desde WooCommerce."""
        self.ensure_one()
        self.write({'state': 'running', 'log': 'Iniciando importación de stock...'})
        backend = self.backend_id
        if not backend:
            self.write({
                'state': 'done',
                'log': (self.log or '') + '\nBackend no configurado.',
            })
            return self._return_wizard()

        page = self.start_page if self.start_page > 0 else 1
        product_limit = self.product_limit if self.product_limit > 0 else None
        total_adjusted = 0
        total_skipped = 0
        processed = 0

        try:
            self.write({
                'log': (self.log or '') + '\nImportando stock desde página %s%s. Ubicación: %s.' % (
                    page,
                    ' con límite %s' % product_limit if product_limit else '',
                    self.stock_location_id.complete_name or self.stock_location_id.name,
                )
            })
            while True:
                try:
                    products = backend._wc_get(
                        'products', params={'per_page': 100, 'page': page}
                    )
                except Exception as exc:
                    _logger.exception('Error obteniendo productos de Woo en página %s: %s', page, exc)
                    self.write({
                        'log': (self.log or '') + '\nError obteniendo productos (página %s): %s' % (page, exc)
                    })
                    break

                if not products:
                    break

                self.write({
                    'log': (self.log or '') + '\nProcesando página %s (%s productos)...' % (page, len(products))
                })

                for wc_product in products:
                    try:
                        adj, skip, log_lines = self._import_stock_for_product(backend, wc_product)
                        total_adjusted += adj
                        total_skipped += skip
                        if log_lines:
                            self.write({'log': (self.log or '') + '\n' + '\n'.join(log_lines)})
                    except Exception as exc:
                        _logger.exception(
                            'Error procesando stock para producto Woo ID %s: %s',
                            wc_product.get('id'),
                            exc,
                        )
                        self.write({
                            'log': (self.log or '') + '\nError en producto Woo ID %s: %s' % (
                                wc_product.get('id'), exc
                            )
                        })
                    processed += 1
                    if product_limit and processed >= product_limit:
                        break

                if product_limit and processed >= product_limit:
                    break
                page += 1

            self.write({
                'state': 'done',
                'total_adjusted': total_adjusted,
                'total_skipped': total_skipped,
                'log': (self.log or '') + (
                    '\nImportación de stock finalizada. '
                    '%s variantes/productos ajustados. '
                    '%s omitidos (sin manage_stock o sin mapeo en Odoo).' % (
                        total_adjusted, total_skipped
                    )
                ),
            })
        except Exception as exc:
            _logger.exception('Error general en importación de stock WooCommerce')
            self.write({
                'state': 'done',
                'log': (self.log or '') + '\nError general: %s' % exc,
            })

        return self._return_wizard()

    def _return_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'wc.stock.import.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
