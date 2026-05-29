# SAP Bank Statement Integration

Servicio desatendido que lee CSV bancarios y crea **IncomingPayments** (`DocType: rAccount`)
en SAP Business One vía Service Layer, con **POST individuales en pool de hilos**,
manejo de sesión/401, lock de archivos y reporte Excel de conciliación.

## Por qué POST individuales (y no `$batch`)

El batch de Service Layer **se detiene en la primera sub-petición que falla**, así que no
sirve para un flujo donde los errores parciales son normales y se necesita estado por fila.
Con POST individuales cada documento es independiente: una fila que falla no afecta al resto
y obtienes el reporte EXITO/ERROR/OBSERVADO limpio. Para <1000 registros, el pool de hilos
acotado da rendimiento de sobra sin saturar Service Layer.

## Estructura

| Archivo | Responsabilidad |
|---|---|
| `config.yaml` | Todos los parámetros (URL, credenciales, rutas, cuentas default, retry, concurrencia). |
| `models.py` | Enums y dataclasses del dominio. |
| `sap_client.py` | Login, cookie B1SESSION, POST, re-login en 401, retry/backoff en 5xx. **Thread-safe.** |
| `processor.py` | Parse CSV, validación, construcción de payload por tipo, pool de hilos. |
| `watcher.py` | Escaneo de Inbound, lock `.lock`, movimiento a Procesados/Errores. |
| `report_writer.py` | Reporte `.xlsx` con color por estado. |
| `main.py` | Daemon + bucle con intervalo. |

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Edita `config.yaml`: `sap.base_url`, `company_db`, credenciales, las cuentas default y las rutas.
En **producción** pon `verify_ssl` apuntando al certificado corporativo (no `false`).

## Uso

```bash
python main.py            # daemon continuo
python main.py --once     # un solo ciclo (cron / pruebas)
python main.py --config /ruta/config.yaml
```

Deja los CSV en `paths.inbound`. El servicio los procesa, genera el reporte en `paths.reports`
y mueve el archivo a `processed/` (todo EXITO) o `errors/` (al menos un ERROR/OBSERVADO).

## CSV de entrada

UTF-8 con BOM, delimitado por `;`. Columnas: `fecha;descripcion;tipo_pago;monto;cuenta_destino;cuenta_caja;cuenta_banco;codigo_tarjeta;num_cupon;referencia`.
`tipo_pago` ∈ {EFECTIVO, TARJETA, TRANSFERENCIA, OTROS}. El monto acepta coma o punto decimal.
`referencia` es opcional: el ID único de la transacción bancaria, usado para idempotencia (ver abajo).

## Idempotencia (evitar pagos duplicados)

El lock `.lock` evita procesar el mismo archivo dos veces a la vez, pero **no** evita
reposteos si un CSV se vuelve a dejar (renombrado o reenviado desde `errors/`). Para cerrar
ese hueco, activa `idempotency` en `config.yaml`:

```yaml
idempotency:
  enabled: true
  reference_field: "U_RefBanco"   # UDF de ORCT (créalo antes en SAP) o "Remarks"
```

Con esto, antes de cada POST se hace un `GET ...IncomingPayments?$filter=<campo> eq '<ref>'`.
Si ya existe, la fila se marca **OMITIDA** (no se repostea) y el reporte muestra el `DocEntry`
existente. La referencia se toma de la columna `referencia` del CSV (o de `descripcion` si esa
columna está vacía) y se guarda en `reference_field` al insertar.

- **Recomendado:** crea un UDF en ORCT (ej. `U_RefBanco`) y filtra por ahí. Es limpio y no pisa
  la glosa.
- Si usas `"Remarks"`, ten en cuenta que la glosa pasa a ser la referencia.
- Costo: un `GET` extra por fila. Para <1000 registros es despreciable. Déjalo en `false` si el
  control de archivos te basta.

Estados del reporte: **EXITO** (verde), **ERROR** (rojo), **OBSERVADO** (amarillo, fila
no parseable), **OMITIDA** (azul, ya existía en SAP). Un archivo va a `processed/` si solo
tiene EXITO y/o OMITIDA; va a `errors/` si hay algún ERROR u OBSERVADO.

## ⚠️ Antes del go-live: verifica el esquema real

1. **Campos de tarjeta.** El diseño original decía `CreditCards`/`CreditCardCode`, pero Service
   Layer real usa la colección **`PaymentCreditCards`** y el campo **`CreditCard`** (entero, el
   código de la tarjeta dada de alta en SAP). El código ya usa los nombres correctos. Según tu
   localización/versión, SAP puede exigir además `CardValidUntil`, `CreditAcct` o `CreditCardNumber`.
   Confírmalo contra `GET {base_url}/$metadata` y prueba un POST real en QAS.
2. **`DocType: rAccount`.** Verifica en `ORCT` que el `DocType` quede como `'A'`. Si falla, revisa
   versión de SL y permisos del usuario (IncomingPayments – Add + acceso a las cuentas).
3. **Concurrencia.** `concurrency.max_workers` (default 6). Súbelo con cuidado: hay un límite de
   sesiones concurrentes en Service Layer. Una sola sesión se comparte entre todos los hilos.

## Despliegue como servicio

**Windows (NSSM):**
```bat
nssm install SAPBankIntegration C:\Python310\python.exe C:\SAP_Integration\main.py
nssm set SAPBankIntegration AppDirectory C:\SAP_Integration
nssm set SAPBankIntegration Start SERVICE_AUTO_START
nssm start SAPBankIntegration
```

**Linux (systemd):** unidad en `/etc/systemd/system/sap-bank.service` con `WorkingDirectory`
en el proyecto, `ExecStart=/usr/bin/python3 main.py`, `Restart=always`.

## Si algún día escalas a varios nodos (HA)

El lock por archivo `.lock` solo garantiza una sola instancia. Para multi-nodo, reemplázalo por
un semáforo distribuido (Redis `SETNX` con TTL, o una tabla SQL con estado LOCKED/PROCESSED).
