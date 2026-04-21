import logging
import json
from datetime import timedelta
from typing import Any, Dict, Iterable, Optional, Set, Tuple

from odoo import fields, models

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    wc_id = fields.Integer(string='ID WooCommerce', index=True)
    wc_sale_price = fields.Float(string='Precio oferta WooCommerce')
    wc_sale_date_from = fields.Datetime(string='Oferta desde')
    wc_sale_date_to = fields.Datetime(string='Oferta hasta')
    wc_sync_date = fields.Datetime(string='Última sync WooCommerce')
    wc_product_type = fields.Selection([
        ('simple', 'Simple'),
        ('variable', 'Variable'),
    ], string='Tipo WooCommerce', default='simple')
    wc_synced = fields.Boolean(string='Sincronizado con WooCommerce', compute='_compute_wc_synced')

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

    def _compute_wc_synced(self):
        for product in self:
            product.wc_synced = bool(product.wc_id)

    def _prepare_wc_data(self) -> Dict[str, Any]:
        """Prepara payload de producto para WooCommerce."""
        self.ensure_one()
        categories = []
        if self.categ_id:
            categories = [{'name': self.categ_id.name}]

        data = {
            'name': self.name,
            'sku': self.default_code or '',
            'regular_price': str(self.list_price or 0.0),
            'sale_price': str(self.wc_sale_price) if self.wc_sale_price else '',
            'date_on_sale_from': self.wc_sale_date_from.isoformat() if self.wc_sale_date_from else None,
            'date_on_sale_to': self.wc_sale_date_to.isoformat() if self.wc_sale_date_to else None,
            'description': self.description_sale or self.description or '',
            'stock_quantity': int(self.qty_available),
            'manage_stock': True,
            'categories': categories,
            'type': self.wc_product_type or ('variable' if self.product_variant_count > 1 else 'simple'),
        }
        if data['type'] == 'variable':
            attributes = []
            for line in self.attribute_line_ids:
                attributes.append({'name': line.attribute_id.name, 'visible': True, 'variation': True})
            data['attributes'] = attributes
        return data

    def _process_wc_data(self, wc_data: Dict[str, Any]):
        """Procesa datos de WooCommerce y actualiza producto en Odoo."""
        vals = {
            'name': wc_data.get('name'),
            'default_code': wc_data.get('sku') or False,
            'list_price': float(wc_data.get('regular_price') or 0.0),
            'wc_sale_price': float(wc_data.get('sale_price') or 0.0),
            'wc_id': wc_data.get('id') or self.wc_id,
            'wc_product_type': wc_data.get('type') if wc_data.get('type') in ('simple', 'variable') else 'simple',
            'wc_sync_date': fields.Datetime.now(),
        }
        self.with_context(wc_no_sync=True).write({k: v for k, v in vals.items() if v is not None})

    def _sync_variable_product_to_wc(self):
        """Sincroniza producto variable y luego sus variantes."""
        self.ensure_one()
        self.action_sync_to_wc()
        for variant in self.product_variant_ids:
            variant.action_sync_to_wc()

    def _sync_variable_product_from_wc(self, wc_product: Dict[str, Any]):
        """Importa producto variable y sus variaciones."""
        self.ensure_one()
        self._process_wc_data(wc_product)
        backend = self._get_wc_backend()
        if not backend or not self.wc_id:
            return {'imported': 0, 'mapped': 0}

        try:
            backend_batch_size = int(backend.batch_size or 100)
        except (TypeError, ValueError):
            backend_batch_size = 100
        page_size = min(max(backend_batch_size, 1), 100)
        page = 1
        variations = []
        while True:
            page_variations = backend._wc_get(
                f'products/{self.wc_id}/variations',
                params={'per_page': page_size, 'page': page},
            ) or []
            if not page_variations:
                break
            variations.extend(page_variations)
            if len(page_variations) < page_size:
                break
            page += 1

        if not variations:
            return {'imported': 0, 'mapped': 0}

        attr_name_by_id = {
            attr.get('id'): (attr.get('name') or '').strip()
            for attr in (wc_product.get('attributes') or [])
            if attr.get('id')
        }
        used_values: Dict[int, Set[int]] = {}
        value_ref_by_pair: Dict[Tuple[str, str], Any] = {}
        for variation in variations:
            for attr_data in (variation.get('attributes') or []):
                option = (attr_data.get('option') or '').strip()
                attr_name = (attr_data.get('name') or '').strip()
                if not attr_name and attr_data.get('id'):
                    attr_name = attr_name_by_id.get(attr_data.get('id'), '')
                if not attr_name or not option:
                    continue
                attribute = self.env['product.attribute'].search([('name', '=', attr_name)], limit=1)
                if not attribute:
                    create_vals = {'name': attr_name}
                    if 'create_variant' in self.env['product.attribute']._fields:
                        create_vals['create_variant'] = 'always'
                    attribute = self.env['product.attribute'].create(create_vals)
                value = self.env['product.attribute.value'].search([
                    ('attribute_id', '=', attribute.id),
                    ('name', '=', option),
                ], limit=1)
                if not value:
                    value = self.env['product.attribute.value'].create({
                        'attribute_id': attribute.id,
                        'name': option,
                    })
                used_values.setdefault(attribute.id, set()).add(value.id)
                value_ref_by_pair[(attribute.name.strip().lower(), option.lower())] = value

        if used_values:
            existing_lines = {line.attribute_id.id: line for line in self.attribute_line_ids}
            create_commands = []
            for attribute_id, value_ids in used_values.items():
                line = existing_lines.get(attribute_id)
                sorted_values = sorted(value_ids)
                if line:
                    if set(line.value_ids.ids) != set(sorted_values):
                        line.write({'value_ids': [(6, 0, sorted_values)]})
                else:
                    create_commands.append((0, 0, {'attribute_id': attribute_id, 'value_ids': [(6, 0, sorted_values)]}))
            if create_commands:
                self.with_context(wc_no_sync=True).write({'attribute_line_ids': create_commands})
            self._create_variant_ids()

        variants = self.product_variant_ids
        variants_by_wc_id = {variant.wc_variation_id: variant for variant in variants.filtered('wc_variation_id')}
        variants_by_sku = {variant.default_code: variant for variant in variants.filtered('default_code')}
        variants_by_attrs = {}
        for variant in variants:
            signature = tuple(sorted(
                (
                    ptav.attribute_id.name.strip().lower(),
                    ptav.product_attribute_value_id.name.strip().lower(),
                )
                for ptav in variant.product_template_attribute_value_ids
                if ptav.attribute_id and ptav.product_attribute_value_id
            ))
            if signature:
                variants_by_attrs[signature] = variant

        mapped_count = 0
        variation_ids = {variation.get('id') for variation in variations if variation.get('id')}
        valid_signatures = set()
        for variation in variations:
            variant = variants_by_wc_id.get(variation.get('id'))
            if not variant:
                attrs_signature = []
                combination_values = self.env['product.attribute.value']
                for attr_data in (variation.get('attributes') or []):
                    option = (attr_data.get('option') or '').strip()
                    attr_name = (attr_data.get('name') or '').strip()
                    if not attr_name and attr_data.get('id'):
                        attr_name = attr_name_by_id.get(attr_data.get('id'), '')
                    if not attr_name or not option:
                        continue
                    attrs_signature.append((attr_name.strip().lower(), option.lower()))
                    value = value_ref_by_pair.get((attr_name.strip().lower(), option.lower()))
                    if value:
                        combination_values |= value
                normalized_signature = tuple(sorted(attrs_signature))
                if normalized_signature:
                    valid_signatures.add(normalized_signature)
                variant = variants_by_attrs.get(normalized_signature)
                if not variant and variation.get('sku'):
                    variant = variants_by_sku.get(variation.get('sku'))
                if not variant and combination_values and hasattr(self, '_get_variant_for_combination'):
                    variant = self._get_variant_for_combination(combination_values)
            else:
                attrs_signature = tuple(sorted(
                    (
                        (attr_data.get('name') or attr_name_by_id.get(attr_data.get('id'), '')).strip().lower(),
                        (attr_data.get('option') or '').strip().lower(),
                    )
                    for attr_data in (variation.get('attributes') or [])
                    if (attr_data.get('option') or '').strip()
                    and (attr_data.get('name') or attr_name_by_id.get(attr_data.get('id'), '')).strip()
                ))
                if attrs_signature:
                    valid_signatures.add(attrs_signature)
            if not variant:
                continue
            variant._process_wc_variation_data(variation)
            mapped_count += 1

        if valid_signatures:
            extra_variants = self.product_variant_ids.filtered(
                lambda variant: variant.wc_variation_id not in variation_ids
                and tuple(sorted(
                    (
                        ptav.attribute_id.name.strip().lower(),
                        ptav.product_attribute_value_id.name.strip().lower(),
                    )
                    for ptav in variant.product_template_attribute_value_ids
                    if ptav.attribute_id and ptav.product_attribute_value_id
                )) not in valid_signatures
            )
            if extra_variants:
                extra_variants.with_context(wc_no_sync=True).write({'active': False})

        _logger.info(
            'Producto variable Woo %s (%s): %s variaciones importadas, %s mapeadas',
            self.wc_id,
            self.display_name,
            len(variations),
            mapped_count,
        )
        return {'imported': len(variations), 'mapped': mapped_count}

    def action_sync_to_wc(self):
        """Sincroniza manualmente producto hacia WooCommerce."""
        backend = self._get_wc_backend()
        if not backend:
            return
        for product in self:
            data = product._prepare_wc_data()
            if product.wc_id:
                response = backend._wc_put(f'products/{product.wc_id}', data)
            else:
                response = backend._wc_post('products', data)
            product.with_context(wc_no_sync=True).write({
                'wc_id': response.get('id') or product.wc_id,
                'wc_sync_date': fields.Datetime.now(),
            })

    def action_sync_from_wc(self):
        """Trae datos del producto desde WooCommerce."""
        backend = self._get_wc_backend()
        if not backend:
            return
        for product in self:
            if not product.wc_id:
                continue
            wc_product = backend._wc_get(f'products/{product.wc_id}')
            if wc_product.get('type') == 'variable':
                product._sync_variable_product_from_wc(wc_product)
            else:
                product._process_wc_data(wc_product)

    def write(self, vals: Dict[str, Any]):
        watched_fields = ['name', 'default_code', 'list_price', 'wc_sale_price', 'wc_sale_date_from', 'wc_sale_date_to', 'categ_id']
        should_enqueue = self._wc_field_changed(vals, watched_fields) and not self._is_wc_sync_disabled()
        result = super().write(vals)
        if should_enqueue and not self._wc_recent_sync('wc_sync_date'):
            for product in self.filtered('wc_id'):
                product._enqueue_wc_job(action='export', priority=3 if any(f in vals for f in ['list_price', 'wc_sale_price']) else 5)
        return result
