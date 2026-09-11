# Caso de estudio: de un procedimiento manual a un flujo verificable

## Contexto

El proceso de cobranza requería revisar recibos y facturas en dos módulos de un ERP. El método asociado al catálogo de clientes no era suficiente para decidir sobre cada factura. Había nombres compartidos por distintas cuentas y recibos consecutivos del mismo cliente.

El objetivo fue automatizar la consulta y la generación condicionada del CFDI de pago, conservando evidencia de cada decisión.

## Preguntas que guiaron el análisis

- ¿Cuál es la fuente documental adecuada para determinar el método de pago?
- ¿Qué identificador permite unir un recibo con su factura?
- ¿Cómo distinguir cuentas diferentes que comparten el mismo nombre?
- ¿Qué debe ocurrir cuando no hay datos suficientes para decidir?
- ¿Cómo saber si el ERP completó una acción antes de intentar repetirla?

Estas preguntas llevaron a reglas de comparación, control de excepciones y persistencia. El nombre se utiliza para encontrar cuentas candidatas; la serie y el folio permiten localizar la factura; el PDF aporta la evidencia de método y receptor.

## Decisiones y competencias demostradas

| Situación | Decisión adoptada | Competencia observable |
|---|---|---|
| Clasificación del catálogo insuficiente | Consultar el método de cada factura PDF | Pensamiento crítico y selección de fuentes |
| Varias cuentas con el mismo nombre | Buscar la factura exacta en todas las cuentas candidatas | Calidad de datos y resolución de entidades |
| Cliente no encontrado | Omitir y registrar el motivo | Definición de reglas de excepción |
| Bloqueo después de una descarga | Localizar la etapa mediante logs y ajustar el orden de acciones | Diagnóstico estructurado y comunicación precisa |
| Dos recibos consecutivos del mismo cliente | Conservar la selección y volver a consultar facturas | Observación del proceso y diseño por eventos |
| Respuesta de emisión incierta | Registrar el intento antes de emitir y bloquear reenvíos automáticos | Responsabilidad sobre efectos operativos |
| Necesidad de reutilizar el programa | Entregar fechas y modo por argumentos de terminal | Documentación y transferencia de conocimiento |

## Participación y aprendizaje

El responsable aportó el conocimiento operativo, definió criterios de aceptación y supervisó ejecuciones para contrastar el comportamiento del programa con el trabajo real. La IA apoyó la implementación, documentación y pruebas. La depuración se basó en observaciones concretas y registros de ejecución, no en aceptar el código generado sin verificarlo.

El aprendizaje incluyó distinguir código y configuración, interpretar errores, entender funciones y argumentos, reconocer que la interfaz funciona por eventos y comprender por qué una bitácora persistente es necesaria para recuperar el contexto entre ejecuciones.

## Validación

La validación avanzó desde una consulta de tres recibos hasta un recorrido de 72, incluyendo casos de cuentas duplicadas y registros ausentes. Después se probó una emisión individual y el responsable informó que verificó satisfactoriamente los resultados de la ejecución operativa del rango.

El repositorio permite reproducir las reglas con un portal simulado y documentos ficticios. Los archivos del caso real se mantienen privados. No se afirma una mejora porcentual de productividad ni una tasa de error cuantificada porque no se levantó una línea base formal.

## Qué medir en una siguiente etapa

Para cuantificar el impacto, sería útil medir tiempo por recibo antes y después, proporción de casos que requieren revisión, fallos por etapa y frecuencia de datos faltantes o duplicados. Son indicadores propuestos, no resultados ya obtenidos.
