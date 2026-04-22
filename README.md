# WooCommerce Connector (Odoo 18/19)

Módulo nativo para sincronización bidireccional entre WooCommerce y Odoo, compatible con Odoo 18 y 19, diseñado para despliegue en Odoo.sh.

## Instalación en Odoo.sh

1. Subir este repositorio y agregarlo como submódulo/addons source en tu proyecto Odoo.sh.
2. En `odoo.conf`, incluir la ruta del addon `woocommerce_connector` en `addons_path`.
3. Actualizar lista de Apps.
4. Instalar **WooCommerce Connector** con un click desde Apps.

## Upgrade (si el módulo ya estaba instalado)

1. Actualizar el módulo (`-u woocommerce_connector`) para crear los nuevos campos y modelos de cola/webhook.
2. Regenerar el **Token Webhook** en el backend si necesitás rotarlo.
3. Actualizar la URL de webhook en WooCommerce al formato tokenizado.

## Configuración inicial

1. Ir a **WooCommerce → Configuración**.
2. Completar URL de tienda, Consumer Key y Consumer Secret.
3. Guardar y usar **Probar Conexión**.
4. Configurar opciones de sync (stock, precios, pedidos, clientes, imágenes, batch size y rate limit).
5. Ejecutar **Sync Inicial** desde el botón o menú.

## Webhooks en WooCommerce

Configurar webhooks apuntando a:

- `https://TU-ODOO/wc/webhook/<WEBHOOK_TOKEN>`

`WEBHOOK_TOKEN` se genera automáticamente en **WooCommerce → Configuración** y puede regenerarse desde el backend.

Eventos recomendados:

- `product.created`, `product.updated`
- `order.created`, `order.updated`
- `customer.created`, `customer.updated`

Si se configura `webhook_secret`, se valida firma HMAC-SHA256 (`X-WC-Webhook-Signature`).
El endpoint legacy `/wc/webhook` se mantiene solo por compatibilidad temporal y está deprecado.

## Campos agregados

### Productos (`product.template` / `product.product`)
- IDs de WooCommerce (`wc_id`, `wc_variation_id`)
- Precios de oferta y fechas de oferta
- Fecha de última sincronización

### Pedidos (`sale.order`)
- ID de pedido WooCommerce
- Estado original WooCommerce
- Método de pago, nota y fecha de sync

### Clientes (`res.partner`)
- ID de cliente WooCommerce
- Fecha de sincronización

## Cola de trabajos

Modelo `wc.queue.job` con prioridades:

1. Pedidos
2. Stock
3. Precios
5. Productos/variantes
10. Sync inicial masivo

Procesamiento por cron cada 2 minutos, con reintentos automáticos y limpieza diaria.

## Rendimiento y robustez

- Sincronización directa por ORM (sin middleware externo)
- Prevención de loops con contexto `wc_no_sync`
- Cooldown de 30 segundos para evitar resincronización inmediata
- Rate limiting configurable por minuto para API WooCommerce
- Manejo de errores con logging y cola asíncrona

## Troubleshooting

- Verificar credenciales y URL WooCommerce.
- Revisar **WooCommerce → Cola de trabajos** para errores y reintentos.
- Confirmar que los webhooks estén activos y con URL correcta.
- Ajustar `batch_size` y `rate_limit` según carga del worker en Odoo.sh.
