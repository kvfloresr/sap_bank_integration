"""
processor.py
------------
Logica de negocio:
  - Validacion de filas -> OBSERVADO si no se pueden parsear.
  - Construccion del payload JSON de IncomingPayments segun tipo_pago.
  - Procesamiento con pool de hilos acotado (POST individuales).

TARJETA usa PaymentCreditCards[].CreditCard (entero).

Centros de costo: si la fila trae centro_costo / centro_costo2, se inyectan
en PaymentAccounts como ProfitCenter / ProfitCenter2. Esto es obligatorio en
empresas SAP donde las cuentas de ingreso exigen asignacion de dimension.
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
DEFAULT_CURRENCY  = "BS"


# ----------------------------------------------------------------- parsing

def _parse_monto(raw: str) -> float:
    s = (raw or "").strip().replace(" ", "")
    if not s:
        raise ValueError("monto vacio")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.index(",") > s.index(".") \
            else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        value = float(s)
    except ValueError:
        raise ValueError(f"monto no numerico: '{raw}'")
    if value <= 0:
        raise ValueError(f"monto no positivo: {value}")
    return value


def _parse_moneda(raw: str) -> str:
    # Acepta el codigo de moneda tal cual lo define SAP (ej: BS, USD, EUR, UFV).
    # No se valida longitud porque SAP usa codigos propios (ej: 'BS' de 2 letras).
    s = (raw or "").strip().upper()
    return s or DEFAULT_CURRENCY


def parse_row(raw: dict, line_num: int) -> CsvRow:
    fecha          = (raw.get("fecha")          or "").strip()
    descripcion    = (raw.get("descripcion")    or "").strip()
    cuenta_destino = (raw.get("cuenta_destino") or "").strip()

    if not fecha:
        raise ValueError("campo 'fecha' faltante")
    if not cuenta_destino:
        raise ValueError("campo 'cuenta_destino' faltante")

    try:
        tipo = PaymentType.from_str(raw.get("tipo_pago", ""))
    except ValueError:
        raise ValueError(f"tipo_pago invalido: '{raw.get('tipo_pago')}'")

    monto  = _parse_monto(raw.get("monto",  ""))
    moneda = _parse_moneda(raw.get("moneda", ""))

    def opt(key: str) -> Optional[str]:
        v = (raw.get(key) or "").strip()
        return v or None

    return CsvRow(
        line_num       = line_num,
        fecha          = fecha,
        descripcion    = descripcion,
        tipo_pago      = tipo,
        monto          = monto,
        cuenta_destino = cuenta_destino,
        moneda         = moneda,
        cuenta_caja    = opt("cuenta_caja"),
        cuenta_banco   = opt("cuenta_banco"),
        codigo_tarjeta = opt("codigo_tarjeta"),
        num_cupon      = opt("num_cupon"),
        referencia     = opt("referencia"),
        centro_costo   = opt("centro_costo"),
        centro_costo2  = opt("centro_costo2"),
        centro_costo3  = opt("centro_costo3"),
        unidad_negocio = opt("unidad_negocio"),
    )


# --------------------------------------------------------- payload building

def build_payload(row: CsvRow, accounts_cfg: dict) -> dict:
    """
    Construye el JSON de IncomingPayments.
    - DocCurrency viene del Excel fila por fila.
    - PaymentAccounts incluye ProfitCenter / ProfitCenter2 si la fila los trae.
    """
    payment_line: dict = {
        "AccountCode": row.cuenta_destino,
        "SumPaid":     row.monto,
    }
    if row.centro_costo:
        payment_line["ProfitCenter"] = row.centro_costo
    if row.centro_costo2:
        payment_line["ProfitCenter2"] = row.centro_costo2
    if row.centro_costo3:
        payment_line["ProfitCenter3"] = row.centro_costo3

    payload: dict = {
        "DocDate":         row.fecha,
        "DocType":         "rAccount",
        "Remarks":         row.descripcion,
        "PaymentAccounts": [payment_line],
    }
    # DocCurrency solo se envia si NO es la moneda local.
    # Para la moneda local, SAP usa la moneda por defecto de la empresa.
    if row.moneda and row.moneda != DEFAULT_CURRENCY:
        payload["DocCurrency"] = row.moneda

    match row.tipo_pago:
        case PaymentType.EFECTIVO:
            payload["CashAccount"] = row.cuenta_caja or accounts_cfg["cash_account"]
            payload["CashSum"]     = row.monto

        case PaymentType.TARJETA:
            try:
                credit_card = int(row.codigo_tarjeta) if row.codigo_tarjeta \
                    else int(accounts_cfg.get("default_card_code", 1))
            except (TypeError, ValueError):
                credit_card = int(accounts_cfg.get("default_card_code", 1))

            cc_line: dict = {
                "CreditCard": credit_card,
                "CreditSum":  row.monto,
            }
            if row.num_cupon:
                cc_line["VoucherNum"] = row.num_cupon
            credit_acct = accounts_cfg.get("credit_card_account")
            if credit_acct:
                cc_line["CreditAcct"] = credit_acct
            payload["PaymentCreditCards"] = [cc_line]

        case PaymentType.TRANSFERENCIA | PaymentType.OTROS:
            payload["TransferAccount"] = row.cuenta_banco or accounts_cfg["transfer_account"]
            payload["TransferSum"]     = row.monto

    return payload


# --------------------------------------------------------- file processing

def _read_rows(path: str) -> list[dict]:
    with io.open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        if reader.fieldnames is None:
            raise ValueError("CSV vacio o sin encabezado")
        cols    = {c.strip() for c in reader.fieldnames}
        missing = REQUIRED_COLUMNS - cols
        if missing:
            raise ValueError(f"Faltan columnas requeridas: {sorted(missing)}")
        return list(reader)


def _process_one(
    row: CsvRow,
    client: SapClient,
    accounts_cfg: dict,
    idem_cfg: dict,
) -> ProcessResult:
    payload   = build_payload(row, accounts_cfg)
    idem_on   = bool(idem_cfg.get("enabled"))
    ref_field = idem_cfg.get("reference_field", "U_RefBanco")
    ref_value = row.referencia or row.descripcion

    if idem_on and ref_value:
        payload[ref_field] = ref_value
        existing = client.incoming_payment_exists(ref_field, ref_value)
        if existing is not None:
            logger.info("Linea %s OMITIDA -> ya existe DocEntry=%s", row.line_num, existing)
            return ProcessResult(
                line_num       = row.line_num,
                status         = RowStatus.OMITIDA,
                cuenta_destino = row.cuenta_destino,
                doc_entry      = existing,
                error          = f"Ya existe en SAP (DocEntry={existing})",
            )

    outcome = client.post_incoming_payment(payload)
    if outcome.ok:
        logger.info("Linea %s EXITO -> DocEntry=%s", row.line_num, outcome.doc_entry)
        return ProcessResult(
            line_num       = row.line_num,
            status         = RowStatus.EXITO,
            cuenta_destino = row.cuenta_destino,
            doc_entry      = outcome.doc_entry,
            doc_num        = outcome.doc_num,
        )
    logger.error("Linea %s ERROR (HTTP %s): %s", row.line_num, outcome.status_code, outcome.error)
    return ProcessResult(
        line_num       = row.line_num,
        status         = RowStatus.ERROR,
        cuenta_destino = row.cuenta_destino,
        error          = outcome.error,
    )


def process_file(
    path: str,
    source_name: str,
    client: SapClient,
    accounts_cfg: dict,
    max_workers: int,
    idem_cfg: dict | None = None,
) -> FileSummary:
    idem_cfg = idem_cfg or {}
    summary  = FileSummary(source_name=source_name)
    raw_rows = _read_rows(path)

    valid_rows: list[CsvRow] = []
    for idx, raw in enumerate(raw_rows, start=2):
        try:
            valid_rows.append(parse_row(raw, idx))
        except ValueError as exc:
            logger.warning("Linea %s OBSERVADO: %s", idx, exc)
            summary.results.append(ProcessResult(
                line_num       = idx,
                status         = RowStatus.OBSERVADO,
                cuenta_destino = (raw.get("cuenta_destino") or "").strip(),
                error          = f"Fila invalida: {exc}",
            ))

    if valid_rows:
        client.ensure_session()
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = pool.map(
                lambda r: _process_one(r, client, accounts_cfg, idem_cfg), valid_rows
            )
            summary.results.extend(results)

    summary.results.sort(key=lambda r: r.line_num)
    logger.info(
        "Archivo '%s': %s EXITO, %s ERROR, %s OBSERVADO, %s OMITIDA",
        source_name, summary.exitos, summary.errores,
        summary.observados, summary.omitidas,
    )
    return summary