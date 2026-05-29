"""
processor.py
------------
Logica de negocio:
  - Parse del CSV (UTF-8 BOM, delimitado por ';').
  - Validacion de filas -> OBSERVADO si no se pueden parsear.
  - Construccion del payload JSON de IncomingPayments segun tipo_pago.
  - Procesamiento del archivo con un pool de hilos acotado (POST individuales).

Nota sobre TARJETA: el documento de diseno usaba "CreditCards"/"CreditCardCode",
pero el esquema real de Service Layer usa la coleccion PaymentCreditCards y el
campo CreditCard (entero). Aqui se usan los nombres reales. Valida igual contra
GET {base_url}/$metadata por si tu version exige campos extra (CardValidUntil,
CreditAcct, etc.).
"""

from __future__ import annotations

import csv
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from models import CsvRow, FileSummary, PaymentType, ProcessResult, RowStatus
from sap_client import SapClient

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"fecha", "descripcion", "tipo_pago", "monto", "cuenta_destino"}


# --------------------------------------------------------------------- parsing

def _parse_monto(raw: str) -> float:
    """Acepta separador decimal coma o punto. Lanza ValueError si no es numerico."""
    s = (raw or "").strip().replace(" ", "")
    if not s:
        raise ValueError("monto vacio")
    # Si tiene coma y punto, asumimos punto = miles y coma = decimal (es-BO)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        value = float(s)
    except ValueError:
        raise ValueError(f"monto no numerico: '{raw}'")
    if value <= 0:
        raise ValueError(f"monto no positivo: {value}")
    return value


def parse_row(raw: dict, line_num: int) -> CsvRow:
    """
    Convierte una fila cruda del CSV en un CsvRow tipado.
    Lanza ValueError con un mensaje descriptivo si la fila es invalida
    (lo captura process_file y la marca OBSERVADO).
    """
    fecha = (raw.get("fecha") or "").strip()
    descripcion = (raw.get("descripcion") or "").strip()
    cuenta_destino = (raw.get("cuenta_destino") or "").strip()

    if not fecha:
        raise ValueError("campo 'fecha' faltante")
    if not cuenta_destino:
        raise ValueError("campo 'cuenta_destino' faltante")

    try:
        tipo = PaymentType.from_str(raw.get("tipo_pago", ""))
    except ValueError:
        raise ValueError(f"tipo_pago invalido: '{raw.get('tipo_pago')}'")

    monto = _parse_monto(raw.get("monto", ""))

    def opt(key: str) -> Optional[str]:
        v = (raw.get(key) or "").strip()
        return v or None

    return CsvRow(
        line_num=line_num,
        fecha=fecha,
        descripcion=descripcion,
        tipo_pago=tipo,
        monto=monto,
        cuenta_destino=cuenta_destino,
        cuenta_caja=opt("cuenta_caja"),
        cuenta_banco=opt("cuenta_banco"),
        codigo_tarjeta=opt("codigo_tarjeta"),
        num_cupon=opt("num_cupon"),
        referencia=opt("referencia"),
    )


# ------------------------------------------------------------ payload building

def build_payload(row: CsvRow, accounts_cfg: dict) -> dict:
    """
    Construye el JSON de IncomingPayments. La coleccion PaymentAccounts
    (contrapartida contable) se inyecta SIEMPRE, sin importar el tipo.
    """
    payload: dict = {
        "DocDate": row.fecha,
        "DocType": "rAccount",
        "Remarks": row.descripcion,
        "PaymentAccounts": [
            {"AccountCode": row.cuenta_destino, "SumPaid": row.monto}
        ],
    }

    match row.tipo_pago:
        case PaymentType.EFECTIVO:
            payload["CashAccount"] = row.cuenta_caja or accounts_cfg["cash_account"]
            payload["CashSum"] = row.monto

        case PaymentType.TARJETA:
            try:
                credit_card = int(row.codigo_tarjeta) if row.codigo_tarjeta \
                    else int(accounts_cfg.get("default_card_code", 1))
            except (TypeError, ValueError):
                credit_card = int(accounts_cfg.get("default_card_code", 1))

            cc_line = {
                "CreditCard": credit_card,
                "CreditSum": row.monto,
            }
            if row.num_cupon:
                cc_line["VoucherNum"] = row.num_cupon
            credit_acct = accounts_cfg.get("credit_card_account")
            if credit_acct:
                cc_line["CreditAcct"] = credit_acct
            payload["PaymentCreditCards"] = [cc_line]

        case PaymentType.TRANSFERENCIA | PaymentType.OTROS:
            payload["TransferAccount"] = row.cuenta_banco or accounts_cfg["transfer_account"]
            payload["TransferSum"] = row.monto

    return payload


# ----------------------------------------------------------- file processing

def _read_rows(path: str) -> list[dict]:
    """Lee el CSV con UTF-8 BOM y delimitador ';'. Devuelve lista de dicts."""
    # utf-8-sig consume automaticamente el BOM si esta presente
    with io.open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        if reader.fieldnames is None:
            raise ValueError("CSV vacio o sin encabezado")
        cols = {c.strip() for c in reader.fieldnames}
        missing = REQUIRED_COLUMNS - cols
        if missing:
            raise ValueError(f"Faltan columnas requeridas en el encabezado: {sorted(missing)}")
        return list(reader)


def _process_one(
    row: CsvRow,
    client: SapClient,
    accounts_cfg: dict,
    idem_cfg: dict,
) -> ProcessResult:
    """Construye el payload y lo postea. Pensado para correr en un hilo del pool.

    Si la idempotencia esta activa y la fila tiene una referencia, primero verifica
    en SAP si ya existe un documento con esa referencia. Si existe -> OMITIDA (no
    repostea). Si no existe, inyecta la referencia en el payload antes de insertar.
    """
    payload = build_payload(row, accounts_cfg)

    idem_on = bool(idem_cfg.get("enabled"))
    ref_field = idem_cfg.get("reference_field", "U_RefBanco")
    ref_value = row.referencia or row.descripcion  # clave de deduplicacion

    if idem_on and ref_value:
        # Guardar la referencia en el documento para poder deduplicar en el futuro
        payload[ref_field] = ref_value
        existing = client.incoming_payment_exists(ref_field, ref_value)
        if existing is not None:
            logger.info("Linea %s OMITIDA -> ya existe DocEntry=%s (ref=%s)",
                        row.line_num, existing, ref_value)
            return ProcessResult(
                line_num=row.line_num,
                status=RowStatus.OMITIDA,
                cuenta_destino=row.cuenta_destino,
                doc_entry=existing,
                error=f"Ya existe en SAP (DocEntry={existing}, {ref_field}={ref_value})",
            )
    elif idem_on and not ref_value:
        logger.warning("Linea %s sin referencia: se inserta sin chequeo de duplicado.",
                       row.line_num)

    outcome = client.post_incoming_payment(payload)
    if outcome.ok:
        logger.info("Linea %s EXITO -> DocEntry=%s", row.line_num, outcome.doc_entry)
        return ProcessResult(
            line_num=row.line_num,
            status=RowStatus.EXITO,
            cuenta_destino=row.cuenta_destino,
            doc_entry=outcome.doc_entry,
            doc_num=outcome.doc_num,
        )
    logger.error("Linea %s ERROR (HTTP %s): %s", row.line_num, outcome.status_code, outcome.error)
    return ProcessResult(
        line_num=row.line_num,
        status=RowStatus.ERROR,
        cuenta_destino=row.cuenta_destino,
        error=outcome.error,
    )


def process_file(
    path: str,
    source_name: str,
    client: SapClient,
    accounts_cfg: dict,
    max_workers: int,
    idem_cfg: dict | None = None,
) -> FileSummary:
    """
    Procesa un CSV completo:
      1. Parsea todas las filas (las invalidas -> OBSERVADO, no se postean).
      2. Postea las validas en paralelo con un pool acotado (con idempotencia opcional).
      3. Devuelve el resumen ordenado por numero de linea.
    """
    idem_cfg = idem_cfg or {}
    summary = FileSummary(source_name=source_name)
    raw_rows = _read_rows(path)

    valid_rows: list[CsvRow] = []
    # La fila 1 es el encabezado; los datos empiezan en la linea 2.
    for idx, raw in enumerate(raw_rows, start=2):
        try:
            valid_rows.append(parse_row(raw, idx))
        except ValueError as exc:
            logger.warning("Linea %s OBSERVADO: %s", idx, exc)
            summary.results.append(
                ProcessResult(
                    line_num=idx,
                    status=RowStatus.OBSERVADO,
                    cuenta_destino=(raw.get("cuenta_destino") or "").strip(),
                    error=f"Fila invalida: {exc}",
                )
            )

    # POST de las filas validas en paralelo
    if valid_rows:
        client.ensure_session()
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = pool.map(
                lambda r: _process_one(r, client, accounts_cfg, idem_cfg), valid_rows
            )
            summary.results.extend(results)

    summary.results.sort(key=lambda r: r.line_num)
    logger.info(
        "Archivo '%s' procesado: %s EXITO, %s ERROR, %s OBSERVADO, %s OMITIDA (total %s)",
        source_name, summary.exitos, summary.errores, summary.observados,
        summary.omitidas, summary.total,
    )
    return summary
