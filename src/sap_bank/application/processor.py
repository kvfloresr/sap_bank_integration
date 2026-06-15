"""
processor.py  (application)
---------------------------
Casos de uso: orquesta el dominio y la infraestructura.
  - parse_row: valida y tipa una fila cruda -> CsvRow (o ValueError -> OBSERVADO).
  - process_rows: procesa una lista de filas ya leidas (desde Excel o CSV).
  - process_file: lee un CSV y procesa (compatibilidad con el flujo viejo).

La construccion del payload vive en domain/payment_builder.py (logica pura).
Este modulo solo coordina: parsear -> construir -> postear -> recolectar.
"""

from __future__ import annotations

import csv
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from src.sap_bank.domain.models import (
    CsvRow, FileSummary, PaymentType, ProcessResult, RowStatus,
)
from src.sap_bank.domain.payment_builder import build_payload
from src.sap_bank.infrastructure.sap_client import SapClient

logger = logging.getLogger(__name__)

DEFAULT_CURRENCY = "BS"


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
    s = (raw or "").strip().upper()
    return s or DEFAULT_CURRENCY


def parse_row(raw: dict, line_num: int) -> CsvRow:
    """Convierte una fila cruda (dict) en CsvRow tipado. ValueError si invalida."""
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
        glosa          = opt("glosa"),
        partida_flujo  = opt("partida_flujo"),
    )


# --------------------------------------------------------- procesamiento

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


def process_rows(
    raw_rows: list[dict],
    source_name: str,
    client: SapClient,
    accounts_cfg: dict,
    max_workers: int,
    idem_cfg: dict | None = None,
) -> FileSummary:
    """
    Procesa una lista de filas crudas (dicts) ya leidas desde Excel o CSV.
    Este es el punto de entrada principal que usan tanto la CLI como la web.
    """
    idem_cfg = idem_cfg or {}
    summary  = FileSummary(source_name=source_name)

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