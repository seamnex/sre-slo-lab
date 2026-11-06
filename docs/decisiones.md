# Decisiones de diseño de los SLOs

Este documento explica **por qué** cada número de este lab es el que es. Un SLO
sin las razones detrás es un número que nadie se anima a cambiar, y que tampoco
nadie sabe defender cuando alguien pregunta por qué el equipo no salió a
apagar un incendio que "solo" afectó al 0,4 % de los usuarios.

---

## 1. Qué se mide: el checkout, no el servidor

El servicio expone `/healthz`, `/metrics` y `/api/checkout`. **Los SLOs miden
solo `/api/checkout`**, porque es lo único que le importa a un usuario.

Un `/healthz` en verde con el checkout devolviendo 500 es exactamente el
incidente que un SLO tiene que ver y que un monitoreo de "¿está vivo el
proceso?" no ve. Incluir los health checks en el SLI, además, lo inflaría con
tráfico que siempre sale bien.

## 2. Los dos SLOs

| SLO | SLI (evento bueno / evento válido) | Objetivo | Error budget (30 días) |
|---|---|---|---|
| **Disponibilidad** | checkouts sin 5xx / checkouts | **99,5 %** | 0,5 % ≈ 3 h 36 min de caída total |
| **Latencia** | checkouts en < 300 ms / checkouts | **99 %** | 1 % de checkouts lentos |

### Por qué 99,5 % y no 99,9 %

Con 99,9 %, el budget mensual es de 43 minutos. Un solo incidente P1 con un
MTTR razonable —detección, War Room, diagnóstico, rollback— se lo come entero.
Un SLO que se incumple todos los meses deja de ser una herramienta para decidir
y pasa a ser ruido que el equipo aprende a ignorar.

**El objetivo no se elige por aspiración, se elige por lo que el usuario
tolera y por lo que el sistema puede sostener.** Para un lab sin usuarios
reales, 99,5 % es una posición razonable: deja margen para un incidente serio
por mes sin que el SLO deje de significar algo.

### Por qué la latencia se mide contando, no con el p99

El SLI de latencia **no** es "el p99 es menor a 300 ms". Es "qué fracción de
los checkouts tardó menos de 300 ms", y se calcula contando pedidos en el
bucket `le="0.3"` del histograma.

- `histogram_quantile()` **estima** el p99 interpolando entre buckets. Con
  buckets de 0,2 y 0,3 s, un p99 "de 0,29 s" puede ser cualquier cosa entre
  esos dos valores.
- Un percentil no se puede promediar entre ventanas ni entre instancias. Una
  fracción de eventos sí: se suman los buenos, se suman los totales, se divide.
- El error budget necesita una fracción de eventos malos. Con un p99 no hay
  forma de saber cuánto budget se consumió.

Por eso **300 ms es un bucket exacto del histograma** (`servicio.py`). Si el
límite cayera entre dos buckets, el SLI volvería a ser una estimación.

El p99 sigue en el dashboard, como contexto para diagnosticar. No como SLI.

### Qué pasa con los errores rápidos

Un checkout que devuelve 500 en 5 ms cuenta como **rápido** en el SLO de
latencia. Es a propósito: ese pedido ya consume budget en el SLO de
disponibilidad, y contarlo dos veces haría que una sola falla queme dos budgets.
Cada SLO mide una cosa.

## 3. Las alertas: burn rate multiventana

### Por qué no alcanza con un umbral fijo

"Alertar si los errores superan el 1 %" tiene dos problemas, los dos clásicos
en gestión de incidentes:

- **Dispara por picos que no importan.** Treinta segundos al 2 % no ponen en
  riesgo el SLO del mes, pero despiertan a alguien igual.
- **No dispara por degradaciones que sí importan.** Un 0,8 % sostenido durante
  días se come el budget entero y nunca cruza el umbral.

El burn rate cambia la pregunta: no "¿hay errores?" sino **"¿a este ritmo, el
budget se agota antes de fin de mes?"**.

### Las cuatro condiciones

Tomadas del capítulo 5 del *SRE Workbook* de Google:

| Severidad | Burn rate | Ventana larga | Ventana corta | Budget consumido al disparar |
|---|---|---|---|---|
| page | 14,4× | 1 h | 5 min | 2 % |
| page | 6× | 6 h | 30 min | 5 % |
| ticket | 3× | 1 d | 2 h | 10 % |
| ticket | 1× | 3 d | 6 h | 10 % |

- **La ventana larga da precisión:** un pico de dos minutos no mueve el
  promedio de una hora lo suficiente como para disparar.
- **La ventana corta da reset rápido:** cuando el problema termina, la de 5 min
  baja enseguida y la alerta se apaga, en vez de quedar encendida una hora
  arrastrada por el promedio de la larga. La ventana corta es 1/12 de la larga.
- **Dos severidades:** lo que agota el budget en días despierta a alguien; lo
  que lo agota en semanas abre un ticket. Alertmanager las rutea a receptores
  distintos y la lenta queda inhibida si la rápida ya está disparando por el
  mismo SLO: es el mismo incidente, contado dos veces.

### El `for:`

`for: 1m` en la de page. La ventana corta ya filtra los picos, así que el `for`
solo cubre un scrape perdido o una evaluación con datos parciales. Cada minuto
de `for` es un minuto más de tiempo de detección: no es gratis.

`for: 15m` en la de ticket: a esa escala de tiempo, 15 minutos no cambian nada
y evitan tickets por una degradación que se resolvió sola.

## 4. El punto ciego: `SLOSinDatos`

Si el servicio se cae entero, Prometheus no puede scrapearlo, los contadores
dejan de existir y el ratio de errores da **"sin datos"**, no 100 %. Ninguna
alerta de burn rate dispara.

**La peor caída posible sería la única que no avisa.** `SLOSinDatos` cubre ese
caso con `up == 0` (el target existe pero no responde) y `absent(up)` (el
target desapareció de la configuración). Los dos casos tienen test unitario.

## 5. El `or vector(0)` del SLI de disponibilidad

Un servicio que nunca devolvió un 5xx no tiene la serie `code="500"`. Sin el
`or vector(0)`, el numerador del SLI da vacío y el SLI de disponibilidad da
"sin datos" en vez de 0: el error budget desaparece del dashboard **justo
cuando todo está sano**. Lo encontré con el stack levantado, no leyendo la
documentación, y ahora tiene un test que falla si alguien lo saca.

Si no hay tráfico, el denominador sigue vacío y el SLI también. Ahí "sin datos"
es la respuesta correcta: sin pedidos no hay nada que medir.

## 6. Reglas de grabación

Todas las alertas y el dashboard leen series precalculadas
(`slo:sli_errores:ratio_rate1h`, etc.), no consultas crudas:

- **Una sola definición del SLI.** Si cambia, cambia en un lugar, y la alerta
  y el dashboard no pueden contradecirse.
- **Rendimiento.** `rate()` sobre 30 días evaluado cada 10 segundos por varias
  reglas es caro. Precalculado, se evalúa una sola vez.

**Lo que hay que saber:** los grupos de reglas se evalúan en paralelo. Una regla
de un grupo que lee una serie de otro grupo ve el valor del ciclo anterior. Con
`evaluation_interval: 10s` son 10 segundos de retraso en el burn rate: aceptable
para este lab, pero es la razón por la que en sistemas grandes las reglas
derivadas van en el mismo grupo que sus fuentes.

## 7. Lo que este lab no resuelve

- **Historia corta.** En un stack recién levantado, la ventana "de 1 hora"
  promedia sobre los minutos que hay. La alerta dispara **antes** que en
  producción: el tiempo de detección medido acá es optimista. Los tests
  unitarios cubren el caso con historia completa.
- **Un solo servicio, una sola réplica.** Con varias instancias, el SLI se
  agrega con `sum()` antes de dividir, que es lo que ya hacen las reglas. Lo que
  no está probado es qué pasa cuando una instancia deja de reportar y las demás sí.
- **El error budget no gobierna nada todavía.** En un equipo real, un budget
  agotado frena los deploys de features hasta recuperarlo. Acá solo se mide.
