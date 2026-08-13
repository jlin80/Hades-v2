Quiero construir Hades V2, un sistema cuantitativo especializado exclusivamente en trading de tokens creados en Pump.fun sobre Solana.



El proyecto anterior, Hades V1, acumuló demasiada complejidad, problemas de APIs, componentes desconectados y capas arquitectónicas que no demostraron un edge cuantitativo.



Por tanto, NO copies automáticamente la arquitectura ni el código de Hades V1.



Puedes inspeccionarlo como referencia para identificar errores, decisiones útiles y componentes reutilizables, pero:



No reutilices código sin justificarlo. No mantengas complejidad heredada por compatibilidad.



Hades V2 debe empezar simple y crecer únicamente cuando los datos demuestren que una nueva capa es necesaria.



1\. OBJETIVO



El objetivo final es construir un sistema capaz de:



Pump.fun

&#x20;  ↓

Detectar nuevos tokens

&#x20;  ↓

Recolectar datos desde etapas tempranas

&#x20;  ↓

Analizar microestructura y comportamiento

&#x20;  ↓

Generar señales cuantitativas

&#x20;  ↓

Paper trading realista

&#x20;  ↓

Medir resultados

&#x20;  ↓

Validar edge estadístico

&#x20;  ↓

Live trading con capital limitado



Pero las fases deben implementarse en orden.



No construir live trading hasta que el sistema haya demostrado estabilidad y edge en paper trading.



2\. RESTRICCIÓN DE MERCADO



El sistema inicialmente trabaja exclusivamente con:



Blockchain: Solana

Token universe: Pump.fun tokens



No implementar todavía:



trading genérico de Solana;

múltiples blockchains;

Binance;

Forex;

XAUUSD;

múltiples DEX;

soporte genérico innecesario para Raydium, Orca, Meteora, etc.



Pump.fun es el universo de investigación.



Si un token migra o cambia de mercado, registrar correctamente el evento. No añadir soporte de trading adicional hasta que sea necesario y se solicite explícitamente.



3\. PRINCIPIO FUNDAMENTAL



La prioridad es:



REAL DATA

↓

DATA QUALITY

↓

RESEARCH

↓

QUANTITATIVE EDGE

↓

PAPER TRADING

↓

RISK CONTROL

↓

LIVE EXECUTION



Nunca:



AI

↓

15 strategies

↓

complex architecture

↓

hope for profitability



Cada componente debe justificar su existencia.



Si un componente no contribuye directamente a:



recolectar datos;

generar una hipótesis;

validar una hipótesis;

ejecutar paper trading;

medir resultados;



no debe existir todavía.



4\. ARQUITECTURA INICIAL



Usar un monolito modular simple.



&#x20;                   PUMP.FUN

&#x20;                      │

&#x20;                      ▼

&#x20;               TOKEN DISCOVERY

&#x20;                      │

&#x20;                      ▼

&#x20;                DATA PROVIDER

&#x20;                      │

&#x20;                      ▼

&#x20;                 NORMALIZER

&#x20;                      │

&#x20;                      ▼

&#x20;                 DATABASE

&#x20;                      │

&#x20;         ┌────────────┼────────────┐

&#x20;         ▼            ▼            ▼

&#x20;     FEATURES      TRACKING      SECURITY

&#x20;         │            │

&#x20;         └──────┬─────┘

&#x20;                ▼

&#x20;           SIGNAL ENGINE

&#x20;                │

&#x20;                ▼

&#x20;           RISK ENGINE

&#x20;                │

&#x20;                ▼

&#x20;          PAPER EXECUTION

&#x20;                │

&#x20;                ▼

&#x20;           OUTCOME ENGINE

&#x20;                │

&#x20;                ▼

&#x20;             DATASET

&#x20;                │

&#x20;                ▼

&#x20;             RESEARCH



Inicialmente:



un proceso principal;

PostgreSQL como fuente de verdad;

tareas async controladas;

sin Kafka;

sin Redis;

sin event sourcing;

sin CQRS;

sin microservicios.



No añadir infraestructura adicional hasta demostrar que existe un cuello de botella real.



5\. TECNOLOGÍAS



Utilizar:



Python 3.12+

PostgreSQL

SQLAlchemy

Alembic

Pydantic

FastAPI

httpx

asyncio

Docker

Docker Compose

Pytest

Ruff

MyPy



Usar una estructura limpia pero pragmática.



6\. APIs Y FUENTES DE DATOS



Este punto es crítico.



Antes de escribir adaptadores, investiga qué fuentes actuales, documentadas y utilizables permiten obtener datos de Pump.fun y Solana.



No inventes endpoints.



No asumas que APIs antiguas siguen funcionando.



Para cada fuente candidata, documenta:



Provider

Purpose

Authentication

Rate limits

Latency

Reliability

Required data

Cost

Fallback suitability



Seleccionar:



1 PRIMARY DATA SOURCE

\+

1 FALLBACK DATA SOURCE



No integrar múltiples APIs por defecto.



Cada provider debe tener:



timeout;

retry limitado;

exponential backoff;

rate-limit handling;

schema validation;

structured error logging;

circuit breaker simple si es necesario.



Nunca hacer:



except Exception:

&#x20;   pass



Nunca marcar un provider como saludable sin haber realizado una comprobación real.



7\. TOKEN DISCOVERY



El sistema debe detectar tokens nuevos relevantes de Pump.fun.



Para cada token:



token\_address

symbol

name

creator\_address

created\_at

discovered\_at

source



El sistema debe diferenciar:



CREATED

DISCOVERED

TRACKING

ACTIVE

MIGRATED

INACTIVE

DEAD



Debe ser idempotente.



Un reinicio nunca debe provocar duplicados.



8\. TRACKING DESDE EL NACIMIENTO



Los primeros minutos son críticos.



El sistema debe usar un tracking adaptativo:



Age 0–5 min      → alta frecuencia

Age 5–30 min     → frecuencia media

Age 30–120 min   → frecuencia menor

Age 2h–24h       → frecuencia baja



Los valores exactos deben ser configurables.



Ejemplo:



EARLY\_TRACKING\_SECONDS=300

EARLY\_SNAPSHOT\_INTERVAL\_SECONDS=10





MEDIUM\_TRACKING\_SECONDS=1800

MEDIUM\_SNAPSHOT\_INTERVAL\_SECONDS=30





NORMAL\_SNAPSHOT\_INTERVAL\_SECONDS=60





LONG\_TERM\_SNAPSHOT\_INTERVAL\_SECONDS=300



No hacer polling innecesario de miles de tokens simultáneamente.



Implementar límites y priorización.



9\. DATOS A RECOLECTAR



Solo guardar métricas realmente disponibles.



Posibles métricas:



timestamp

token\_age

price

market\_cap

liquidity

volume

buy\_volume

sell\_volume

buy\_count

sell\_count

transaction\_count

unique\_buyers

unique\_sellers

bonding\_curve\_progress

holder\_count



Además:



provider\_name

observed\_at

received\_at

stored\_at

raw\_provider\_reference



Si una métrica no está disponible:



NULL



Nunca inventar datos.



10\. FEATURES CUANTITATIVAS



Calcular inicialmente features relacionadas con la microestructura de Pump.fun.



Por ejemplo:



token\_age\_seconds





buy\_sell\_ratio





buy\_volume\_ratio





transaction\_velocity





buyer\_velocity





seller\_velocity





volume\_velocity





market\_cap\_velocity





price\_velocity





volume\_acceleration





buyer\_acceleration





transaction\_acceleration





liquidity\_change





market\_cap\_change



Cada feature debe:



tener definición matemática clara;

estar documentada;

poder reproducirse;

indicar qué datos utiliza.



No añadir RSI, MACD o indicadores tradicionales sin una hipótesis específica que justifique su uso.



11\. SNAPSHOT INMUTABLE EN T0



Este es un requisito obligatorio.



Cuando se genere una señal o setup investigable:



SIGNAL CREATED

&#x20;     ↓

FEATURE SNAPSHOT AT T0

&#x20;     ↓

IMMUTABLE STORAGE



Nunca entrenar un modelo utilizando información futura.



Cada observación debe contener:



observation\_id

token\_address

observed\_at

feature\_vector

feature\_version



Las features utilizadas para tomar una decisión deben permanecer intactas.



12\. SIGNAL ENGINE



Inicialmente no quiero 15 estrategias.



Crear una interfaz simple:



class Strategy(Protocol):

&#x20;   name: str





&#x20;   async def evaluate(

&#x20;       self,

&#x20;       market\_state: MarketState,

&#x20;   ) -> Signal | None:

&#x20;       ...



Empezar con una sola estrategia experimental:



EARLY MOMENTUM HYPOTHESIS



La hipótesis debe ser configurable, no presentada como verdad.



Ejemplo conceptual:



Token young

\+

buying activity accelerating

\+

volume increasing

\+

sell pressure below threshold

\+

liquidity above minimum



Esto genera una señal de investigación.



No asumir que es rentable.



13\. RISK ENGINE



Incluso en paper trading, implementar gestión de riesgo.



Cada señal debe pasar por:



MAX TOKEN AGE

MIN LIQUIDITY

MAX SPREAD / ESTIMATED SLIPPAGE

MAX POSITION SIZE

MAX OPEN POSITIONS

MAX DAILY LOSS

MAX DRAWDOWN

STALE DATA CHECK



Toda señal debe tener:



signal\_created\_at

decision\_at

data\_age\_ms



Si los datos son demasiado antiguos:



STALE\_SIGNAL



y debe rechazarse.



No ejecutar decisiones basadas en información obsoleta.



14\. PAPER EXECUTION REALISTA



El paper trading no debe asumir fills perfectos.



Modelar:



entry price

exit price

estimated slippage

fees

latency

liquidity constraints

partial fill possibility if applicable



Cada trade debe guardar:



trade\_id

token

signal\_id

strategy

entry\_time

entry\_price

position\_size





exit\_time

exit\_price





gross\_pnl

fees

slippage

net\_pnl





exit\_reason



Razones de salida:



TAKE\_PROFIT

STOP\_LOSS

TRAILING\_STOP

TIMEOUT

RISK\_EXIT

MANUAL

15\. OUTCOME ENGINE



Independientemente de si una señal se ejecuta, registrar qué ocurrió.



Para cada señal/snapshot:



return\_1m

return\_5m

return\_15m

return\_30m

return\_1h





MFE

MAE



Preparar triple-barrier labeling:



UPPER BARRIER = configurable

LOWER BARRIER = configurable

TIME BARRIER = configurable



Ejemplo:



Did +30% happen before -20%?





YES / NO / TIMEOUT



Pero permitir múltiples configuraciones para investigación.



16\. DATASET DE INVESTIGACIÓN



El sistema debe terminar produciendo:



TOKEN

\+

FEATURE SNAPSHOT AT T0

\+

FUTURE OUTCOME



Ejemplo:



token\_address

observed\_at





token\_age\_seconds

market\_cap

liquidity

volume\_1m

buyers\_1m

sellers\_1m

buy\_sell\_ratio

market\_cap\_velocity

volume\_acceleration





label\_tp\_30\_sl\_20





mfe

mae





return\_5m

return\_15m

return\_1h



Este dataset debe poder exportarse posteriormente para investigación.



17\. RESEARCH



No implementar machine learning complejo inicialmente.



Primero implementar herramientas para responder:



¿Cuántas señales hubo?





¿Cuántas alcanzaron TP?





¿Cuántas alcanzaron SL?





¿Cuál fue el MFE promedio?





¿Cuál fue el MAE promedio?





¿Cuál fue el expectancy?





¿Cuál fue el profit factor?





¿Cómo cambian los resultados según:

&#x20;   token age

&#x20;   liquidity

&#x20;   volume

&#x20;   buy/sell ratio

&#x20;   buyer velocity?



Solo cuando existan suficientes datos:



DATASET

↓

TRAIN / VALIDATION / TEST

↓

OUT-OF-SAMPLE VALIDATION

↓

WALK FORWARD



Nunca usar datos futuros accidentalmente.



18\. BASE DE DATOS



PostgreSQL es la fuente de verdad.



El sistema debe sobrevivir:



container restart

process restart

temporary provider failure

network failure



Al reiniciar:



DATABASE

↓

RECOVER ACTIVE TOKENS

↓

RECOVER TRACKING STATE

↓

CONTINUE



No perder tokens ni posiciones de paper trading.



19\. OBSERVABILIDAD



Crear:



GET /health

GET /status



El estado debe ser real.



Ejemplo:



{

&#x20; "status": "healthy",

&#x20; "tokens\_discovered": 0,

&#x20; "tokens\_tracking": 0,

&#x20; "snapshots\_total": 0,

&#x20; "signals\_total": 0,

&#x20; "paper\_trades": 0,

&#x20; "last\_discovery\_at": null,

&#x20; "last\_snapshot\_at": null,

&#x20; "provider\_status": {

&#x20;   "primary": "healthy",

&#x20;   "fallback": "healthy"

&#x20; }

}



Nunca devolver números falsos.



Registrar métricas de:



provider failures

rate limits

timeouts

snapshot latency

data age

tokens tracked

signals generated

paper trades

20\. SEGURIDAD



Hasta una fase posterior:



NO PRIVATE KEYS

NO WALLET SIGNER

NO REAL TRANSACTIONS

NO LIVE BUY

NO LIVE SELL



El código debe ser incapaz de ejecutar operaciones reales.



Paper trading solamente.



21\. FASES

PHASE 0 — FOUNDATION



Implementar:



repository

configuration

PostgreSQL

Alembic

Docker

structured logging

FastAPI

health/status

tests



Validar antes de continuar.



PHASE 1 — VERIFY DATA SOURCES



Antes de construir todo el pipeline:



investigar fuentes actuales;

probar conectividad real;

probar endpoints reales;

validar schemas;

probar rate limits;

documentar resultados.



Crear:



docs/DATA\_SOURCES.md



No continuar hasta confirmar que tenemos una fuente funcional.



PHASE 2 — TOKEN DISCOVERY



Implementar:



discover

validate

persist

deduplicate

recover after restart

PHASE 3 — SNAPSHOT TRACKING



Implementar:



adaptive tracking

market snapshots

persistence

data validation

stale detection

PHASE 4 — FEATURE ENGINE



Implementar únicamente las features documentadas.



Cada feature debe tener tests.



PHASE 5 — SIGNAL RESEARCH



Implementar una sola hipótesis:



EARLY MOMENTUM



Generar señales sin operar.



PHASE 6 — PAPER TRADING



Añadir:



risk engine

position management

realistic execution simulation

fees

slippage

TP/SL

PHASE 7 — OUTCOME + ANALYTICS



Medir resultados y producir dataset de investigación.



22\. REGLA DE CALIDAD



Después de cada fase:



ruff check .

ruff format --check .

mypy src

pytest



Además:



ejecutar el sistema;

probar con datos reales;

revisar logs;

verificar PostgreSQL;

documentar errores;

no esconder problemas.



No continuar automáticamente si la fase actual falla.



23\. REGLA ANTI-SOBREINGENIERÍA



Antes de añadir cualquier componente, pregúntate:



¿Qué problema concreto resuelve esto hoy?



Si no existe una respuesta clara:



NO IMPLEMENTARLO.



No añadir patrones arquitectónicos por estética.



No crear interfaces para posibilidades hipotéticas.



No crear cinco estrategias.



No crear AI agents.



No crear un Research Lab separado.



No añadir servicios adicionales.



24\. INSTRUCCIÓN FINAL



Primero:



inspecciona el entorno actual;

identifica dónde crear Hades V2;

verifica que no se sobrescribirá Hades V1;

crea un nuevo repositorio/directorio;

implementa exclusivamente PHASE 0.



Al terminar PHASE 0, detente.



Entrega:



FILES CREATED

ARCHITECTURE DECISIONS

COMMANDS TO RUN

TEST RESULTS

REAL PROBLEMS FOUND

WHAT IS REQUIRED FOR PHASE 1



No avances a Phase 1 automáticamente.



La meta no es crear el bot más complejo.



La meta es construir un sistema que pueda responder, con datos reales:



¿Qué condiciones observables en los primeros minutos de un token de Pump.fun tienen valor predictivo y producen una expectativa positiva después de slippage, fees y riesgo?



Solo después de demostrar eso se añadirá ML más avanzado y, eventualmente, ejecución real.

