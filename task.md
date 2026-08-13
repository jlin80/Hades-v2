Quiero que construyas desde cero un nuevo proyecto llamado Hades V2.

Este proyecto reemplaza conceptualmente al anterior Hades, pero NO debes reutilizar su arquitectura ni copiar código automáticamente. El proyecto anterior se volvió demasiado complejo, acumuló problemas con APIs, componentes desconectados, event bus, dependencias y capas que nunca llegaron a demostrar un edge cuantitativo.

La prioridad absoluta de Hades V2 es:

Primero construir una plataforma estable de recolección de datos reales de memecoins de Solana. Después demostrar o refutar hipótesis cuantitativas. Solo después construir un sistema de trading.

No construyas funcionalidades futuras por adelantado.

1. PRINCIPIO FUNDAMENTAL

El desarrollo debe seguir estrictamente esta secuencia:

DATA COLLECTION
        ↓
DATA VALIDATION
        ↓
FEATURE SNAPSHOTS
        ↓
OUTCOME TRACKING
        ↓
DATASET
        ↓
QUANTITATIVE RESEARCH
        ↓
HYPOTHESIS VALIDATION
        ↓
BACKTEST
        ↓
PAPER TRADING
        ↓
LIVE TRADING

Está prohibido saltar etapas.

No implementar:

AI Committee
múltiples agentes LLM
15 estrategias
sistema de live trading
wallet signer
gestión de claves privadas
ejecución real
microservicios innecesarios
event sourcing complejo
CQRS innecesario
Kubernetes
Redis si no existe una necesidad concreta demostrada
Research Lab separado
ML complejo antes de tener un dataset real
XGBoost/LightGBM/CatBoost antes de tener suficientes datos y una hipótesis definida

El sistema inicial debe ser pequeño, observable, testeable y funcional.

2. OBJETIVO DE LA FASE 1

Construir un sistema capaz de funcionar 24/7 y responder:

¿Podemos detectar nuevos memecoins/tokens relevantes en Solana y almacenar snapshots históricos completos de su comportamiento de manera fiable?

La Fase 1 NO compra ni vende nada.

3. ARQUITECTURA INICIAL

Quiero una arquitectura simple.

                 SOLANA DATA SOURCES
                         │
                         ▼
                  TOKEN DISCOVERY
                         │
                         ▼
                   DATA COLLECTOR
                         │
                         ▼
                  NORMALIZATION
                         │
                         ▼
                   VALIDATION
                         │
                         ▼
                    DATABASE
                         │
                         ▼
                  SNAPSHOT TRACKER
                         │
                         ▼
                   OUTCOME TRACKER

Inicialmente quiero preferiblemente un monolito modular.

No dividir en múltiples servicios.

Un solo proceso debe ser suficiente mientras la carga lo permita.

La arquitectura debe permitir separar componentes en el futuro, pero no debemos pagar la complejidad ahora.

4. TECNOLOGÍA

Usar:

Python 3.12+
FastAPI
PostgreSQL
SQLAlchemy
Alembic
Pydantic
Docker
Docker Compose
Pytest
Ruff
MyPy

Para observabilidad:

Structured logging
Health checks
Metrics básicas

No añadir Grafana, Prometheus, Redis, Kafka u otros servicios hasta que exista una razón concreta.

5. ESTRUCTURA DEL PROYECTO

Propón una estructura similar a:

hades-v2/
│
├── src/
│   └── hades/
│       │
│       ├── api/
│       │
│       ├── discovery/
│       │   ├── providers/
│       │   ├── models/
│       │   └── service.py
│       │
│       ├── market_data/
│       │   ├── providers/
│       │   ├── normalizers/
│       │   └── service.py
│       │
│       ├── snapshots/
│       │
│       ├── outcomes/
│       │
│       ├── database/
│       │
│       ├── config/
│       │
│       └── observability/
│
├── tests/
│
├── migrations/
│
├── docker/
│
├── docs/
│
├── pyproject.toml
├── docker-compose.yml
├── .env.example
└── README.md

Puedes modificar esta estructura si existe una alternativa más simple y coherente.

No crear capas abstractas innecesarias.

6. DISEÑO DE APIs Y PROVEEDORES

Este punto es crítico.

El proyecto anterior tuvo problemas porque dependía de demasiadas APIs y proveedores.

Hades V2 debe utilizar el principio:

ONE PRIMARY SOURCE
        ↓
ONE FALLBACK SOURCE
        ↓
DATABASE CACHE

No integrar 7 proveedores.

Primero debemos identificar fuentes estables y documentadas para:

descubrimiento de nuevos tokens;
precio;
liquidez;
volumen;
market cap;
número de transacciones;
compradores/vendedores cuando sea posible;
información básica del token.

Diseña una interfaz simple:

class MarketDataProvider(Protocol):
    async def discover_tokens(...) -> list[Token]:
        ...

    async def get_market_snapshot(...) -> MarketSnapshot:
        ...

Pero:

No crees una abstracción gigantesca con 20 métodos si inicialmente solo necesitamos 2 o 3.

Cada provider debe:

tener timeout;
manejar rate limits;
manejar errores de red;
validar respuestas;
detectar cambios de schema;
registrar errores de forma clara;
tener retry limitado;
nunca bloquear todo el sistema indefinidamente.

No ocultar excepciones.

Si un proveedor falla quiero saber:

provider
endpoint
error_type
status_code
token
retry_count
7. MODELO DE DATOS

Diseña una base de datos orientada inicialmente a investigación.

Necesitamos separar:

Token
token_address
symbol
name
discovered_at
first_seen_at
Token Snapshot

Cada snapshot debe contener:

token_address
timestamp

price
market_cap
liquidity
volume

volume_1m
volume_5m
volume_15m

price_change_1m
price_change_5m
price_change_15m

transactions
buyers
sellers

holder_count

raw_provider_data
provider_name

No inventes campos que no podamos obtener.

Si una fuente no proporciona una métrica:

NULL

es mejor que inventar un valor.

8. FEATURE SNAPSHOT EN T0

Este punto es obligatorio.

Cuando detectemos una posible oportunidad o un token relevante, debemos guardar un snapshot inmutable.

Ejemplo:

TOKEN DETECTED

timestamp = T0

price = 0.000012
market_cap = 45000
liquidity = 18000
volume_5m = 12000

buyers_5m = 87
sellers_5m = 34

token_age_seconds = 180

Ese snapshot nunca debe modificarse.

Posteriormente podremos calcular:

T+1 minute
T+5 minutes
T+15 minutes
T+30 minutes
T+1 hour
T+4 hours
T+24 hours
9. OUTCOME TRACKER

Construye un sistema que siga automáticamente cada token detectado.

Para cada snapshot inicial queremos saber qué ocurrió posteriormente.

Ejemplo:

ENTRY SNAPSHOT
        │
        ├── T+1m
        ├── T+5m
        ├── T+15m
        ├── T+30m
        ├── T+1h
        ├── T+4h
        └── T+24h

Registrar:

price_return
max_favorable_excursion
max_adverse_excursion
max_price
min_price
liquidity_change
market_cap_change

También preparar la arquitectura para future labels.

Por ejemplo:

TP +20% before SL -15%
TP +30% before SL -20%
TP +50% before SL -25%

Pero inicialmente el Outcome Tracker solo debe registrar los datos necesarios.

10. DATA QUALITY

No quiero recopilar basura.

Implementa validaciones explícitas.

Ejemplos:

Reject snapshot if:

price <= 0

timestamp invalid

liquidity negative

market_cap negative

token address invalid

provider response malformed

También detectar:

stale data
duplicate snapshots
provider inconsistencies
missing critical fields

Cada problema debe quedar registrado.

No hacer:

except Exception:
    pass

Nunca.

11. OBSERVABILIDAD

Quiero poder responder rápidamente:

¿El sistema está funcionando?

¿Cuándo fue el último token detectado?

¿Cuándo fue el último snapshot?

¿Cuál proveedor está fallando?

¿Cuántos errores hubo?

¿Cuántos tokens estamos siguiendo?

¿Cuánto tiempo tiene el snapshot más viejo pendiente?

Crear un endpoint:

GET /health

Y otro:

GET /status

Que devuelva métricas reales.

Ejemplo:

{
  "status": "healthy",
  "tokens_discovered": 1243,
  "tokens_tracked": 327,
  "snapshots_collected": 48521,
  "last_discovery_at": "...",
  "last_snapshot_at": "...",
  "providers": {
    "primary": "healthy",
    "fallback": "degraded"
  }
}

No devolver valores simulados.

12. DATABASE-FIRST

PostgreSQL es la fuente de verdad.

No mantener información crítica solamente en memoria.

Después de un reinicio el sistema debe poder:

START
↓
READ DATABASE
↓
RECOVER TOKENS BEING TRACKED
↓
CONTINUE SNAPSHOTS

No perder seguimiento por reiniciar el contenedor.

13. IDEMPOTENCIA

Todas las operaciones deben ser seguras frente a reinicios.

Ejemplo:

Si el sistema procesa dos veces:

token X
timestamp T

no debe crear:

snapshot #1
snapshot #2
snapshot #3

accidentalmente.

Usar constraints e idempotency keys cuando sea necesario.

14. MANEJO DE TIEMPO

Todas las fechas deben almacenarse en UTC.

No usar tiempo local internamente.

Cada dato debe distinguir:

observed_at
received_at
stored_at

cuando sea relevante.

Quiero poder calcular:

data_age_ms

Porque datos atrasados pueden ser inútiles para investigación futura.

15. CONFIGURACIÓN

Toda configuración debe venir de:

.env

Crear:

.env.example

Documentar cada variable.

Ejemplo:

DATABASE_URL=

PRIMARY_PROVIDER_ENABLED=true
FALLBACK_PROVIDER_ENABLED=true

DISCOVERY_INTERVAL_SECONDS=30

SNAPSHOT_INTERVAL_SECONDS=60

HTTP_TIMEOUT_SECONDS=10

MAX_RETRIES=3

No hardcodear URLs, API keys o configuraciones importantes.

16. SEGURIDAD

En esta fase:

NO PRIVATE KEYS
NO WALLET
NO SIGNER
NO LIVE TRADING
NO BUY
NO SELL

El proyecto debe ser físicamente incapaz de ejecutar una operación financiera.

Si en algún punto encuentras código o una dependencia que pueda enviar transacciones, no la añadas.

17. TESTING

Cada componente crítico debe tener tests.

Prioridad:

Provider parsing
Schema validation
Retry behavior
Rate limit handling
Database persistence
Idempotency
Restart recovery
Snapshot scheduling
Outcome tracking

El objetivo no es inflar artificialmente el número de tests.

Prefiero:

150 tests realmente útiles

que:

800 tests superficiales

Los comandos de calidad deben incluir:

ruff check .
ruff format --check .
mypy src
pytest

No continúes ignorando errores existentes.

18. DESARROLLO POR FASES

Implementa solo una fase a la vez.

PHASE 0 — PROJECT FOUNDATION

Crear:

repository
pyproject
Docker
Docker Compose
PostgreSQL
Alembic
configuration
logging
health endpoint
basic tests

Antes de continuar, verificar que:

docker compose up

funciona correctamente.

PHASE 1 — TOKEN DISCOVERY

Implementar solamente:

discover tokens
validate token
store token
avoid duplicates

Verificar con datos reales.

No avanzar hasta que:

system can run continuously

sin errores estructurales.

PHASE 2 — MARKET SNAPSHOTS

Agregar:

price
liquidity
volume
market cap
transactions

Guardar snapshots históricos.

PHASE 3 — OUTCOME TRACKING

Agregar seguimiento:

T+1m
T+5m
T+15m
T+30m
T+1h
T+4h
T+24h

Persistente a reinicios.

PHASE 4 — DATA VALIDATION REPORT

Crear reportes para responder:

¿Cuántos tokens detectamos?

¿Cuántos tienen datos completos?

¿Cuántos proveedores fallaron?

¿Cuántos snapshots faltan?

¿Cuál es el delay promedio?

¿Cuántos datos fueron rechazados?
19. REGLA DE STOP

Después de completar cada fase:

Ejecuta todos los tests.
Ejecuta el sistema.
Verifica datos reales.
Revisa logs.
Verifica la base de datos.
Documenta problemas encontrados.
No avances automáticamente a la siguiente fase si la actual tiene problemas.

Quiero que trabajes como un ingeniero que intenta demostrar que el sistema funciona, no simplemente escribir código que parece correcto.

20. NO INVENTAR DATOS

Nunca:

mockear éxito en producción;
marcar un provider como healthy sin comprobarlo;
generar precios falsos;
devolver métricas simuladas;
asumir que una API responde de cierta manera.

Si una API no funciona:

REPORT THE FAILURE

No construyas lógica ficticia alrededor.

21. DOCUMENTACIÓN OBLIGATORIA

Mantener:

README.md
docs/ARCHITECTURE.md
docs/OPERATING.md
docs/KNOWN_ISSUES.md
CHANGELOG.md

KNOWN_ISSUES.md es importante.

Cada problema real debe documentarse con:

Problem
Impact
Root cause
Current status
Next action

No ocultar deuda técnica.

22. CRITERIO DE ÉXITO DE HADES V2 INICIAL

El proyecto inicial estará terminado únicamente cuando pueda:

Run 24/7

y:

detectar tokens;
guardar tokens;
recolectar snapshots;
sobrevivir reinicios;
recuperarse de fallos temporales de proveedores;
registrar errores claramente;
evitar duplicados;
continuar outcome tracking;
producir un dataset histórico real.

El resultado final de esta primera versión debe permitir exportar algo como:

TOKEN
TIMESTAMP
FEATURES_AT_T0
FUTURE_OUTCOMES

Ejemplo:

Token XYZ

T0:
market_cap = 45k
liquidity = 18k
volume_5m = 12k
buyers = 87

T+15m:
return = +22%

T+1h:
return = +68%

MFE = +94%
MAE = -11%

Cuando tengamos suficientes registros reales, entonces podremos hacer investigación cuantitativa.

23. INSTRUCCIÓN FINAL

No empieces construyendo todo el sistema.

Empieza exclusivamente por:

PHASE 0

Primero analiza el entorno actual y crea un plan de implementación concreto.

Después implementa Phase 0.

Al terminar, detente y entrega:

archivos creados;
decisiones arquitectónicas;
comandos para ejecutar;
resultado de tests;
problemas encontrados;
qué falta para Phase 1.

No implementes Phase 1 hasta que Phase 0 esté validada.

La prioridad es:

CORRECTNESS
↓
RELIABILITY
↓
DATA QUALITY
↓
RESEARCH
↓
PERFORMANCE
↓
COMPLEXITY

Nunca al revés.