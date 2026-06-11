"""
test_local.py
-------------
Lee un .xlsx desde paths.inbound, resuelve los codigos de cuentas del Excel
al formato interno de SAP, mapea centros de costo (ProfitCenter), procesa
cada fila y genera el reporte de conciliacion.

Modos:
    python test_local.py                  # dry-run
    python test_local.py --sap-real       # insercion real
    python test_local.py --xlsx ruta.xlsx # archivo especifico
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from datetime import datetime

import openpyxl
import yaml

sys.path.insert(0, os.path.dirname(__file__))

from account_resolver import AccountResolver
from models import CsvRow, FileSummary, PaymentType, ProcessResult, RowStatus
from processor import build_payload, parse_row
from report_writer import write_report
from sap_client import DryRunClient, SapClient

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("test_local")


# ─────────────────────────────────────────────────────────────────────────────
# MAPEO de columnas del Excel del banco:
#   A (0): MODULO SAP        → tipo_pago
#   B (1): Código cuenta     → cuenta_caja/banco (se resuelve a _SYS)
#   C (2): Nombre cuenta     → descripcion
#   D (3): Cuenta asociada   → cuenta_destino (se resuelve a _SYS)
#   E (4): Débito            → monto
#   F (5): Crédito           → monto
#   G (6): Comentarios       → referencia
#   H (7): Unidad de Negocio → unidad_negocio (referencia)
#   I (8): Centro de Costo   → centro_costo  → ProfitCenter
#   J (9): Centro de Costo 2 → centro_costo2 → ProfitCenter2
#   K (10): Partida Flujo    → se ignora
# ─────────────────────────────────────────────────────────────────────────────

MODULO_A_TIPO: dict[str, str] = {
    "pagos recibidos - banco":   "TRANSFERENCIA",
    "pagos recibidos - cuenta":  "EFECTIVO",
    "pagos recibidos - tarjeta": "TARJETA",
}


def _parse_bs_amount(raw) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    for prefix in ("BS ", "Bs. ", "Bs ", "$ ", "$"):
        if s.upper().startswith(prefix.upper()):
            s = s[len(prefix):]
            break
    s = s.replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(",", "") if s.index(",") < s.index(".") \
            else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    return s


def _str(val) -> str:
    return str(val or "").strip()


def xlsx_to_raw_rows(xlsx_path, accounts_cfg, resolver) -> list[dict]:
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    rows_out: list[dict] = []

    # Periodo abierto en TAJIBOS_QA para pruebas.
    # EN PRODUCCION: from datetime import date; today = date.today().isoformat()
    today = "2026-02-28"

    all_codes: list[str] = []
    raw_data:  list[tuple] = []
    for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if i == 1 or all(c is None for c in row):
            continue
        all_codes.extend([_str(row[1]), _str(row[3])])
        raw_data.append((i, row))

    log.info("Resolviendo %s codigos de cuentas contra SAP...", len(set(all_codes)))
    resolver.resolve_many(all_codes)

    for i, row in raw_data:
        modulo        = _str(row[0])
        cuenta_mayor  = _str(row[1])
        nombre_sn     = _str(row[2])
        cuenta_asoc   = _str(row[3])
        debito_raw    = row[4]
        credito_raw   = row[5]
        comentario    = _str(row[6])
        unidad_neg    = _str(row[7])   # H: Unidad de Negocio  -> ProfitCenter  (dim 1)
        centro_costo  = _str(row[8])   # I: Centro de Costo    -> ProfitCenter2 (dim 2)
        segmento      = _str(row[9]) if len(row) > 9 else ""   # J: Segmento -> ProfitCenter3 (dim 3)

        tipo_raw    = MODULO_A_TIPO.get(modulo.lower(), "TRANSFERENCIA")
        monto_str   = _parse_bs_amount(debito_raw) or _parse_bs_amount(credito_raw)
        descripcion = comentario or nombre_sn or modulo

        cuenta_mayor_res = resolver.resolve(cuenta_mayor) if cuenta_mayor \
            else accounts_cfg.get(
                "cash_account" if tipo_raw == "EFECTIVO" else "transfer_account", ""
            )
        cuenta_asoc_res = resolver.resolve(cuenta_asoc) if cuenta_asoc else ""

        d: dict = {
            "fecha":          today,
            "descripcion":    descripcion,
            "tipo_pago":      tipo_raw,
            "monto":          monto_str,
            "cuenta_destino": cuenta_asoc_res,
            "moneda":         "BS",
            "cuenta_caja":    cuenta_mayor_res if tipo_raw == "EFECTIVO"      else "",
            "cuenta_banco":   cuenta_mayor_res if tipo_raw == "TRANSFERENCIA" else "",
            "codigo_tarjeta": cuenta_mayor_res if tipo_raw == "TARJETA"       else "",
            "num_cupon":      "",
            "referencia":     comentario,
            "centro_costo":   unidad_neg,     # dim 1 (UNIDAD DE NEGOCIO) -> ProfitCenter
            "centro_costo2":  centro_costo,   # dim 2 (CENTRO DE COSTO)   -> ProfitCenter2
            "centro_costo3":  segmento,       # dim 3 (SEGMENTO)          -> ProfitCenter3
            "unidad_negocio": unidad_neg,
        }
        rows_out.append(d)
        log.debug(
            "Fila %s -> tipo=%s monto=%s destino=%s->%s pc1=%s pc2=%s pc3=%s",
            i, tipo_raw, monto_str, cuenta_asoc, cuenta_asoc_res,
            unidad_neg, centro_costo, segmento,
        )

    return rows_out


def run_pipeline(raw_rows, source_name, client, accounts_cfg, idem_cfg) -> FileSummary:
    summary   = FileSummary(source_name=source_name)
    idem_on   = bool(idem_cfg.get("enabled"))
    ref_field = idem_cfg.get("reference_field", "U_RefBanco")

    for idx, raw in enumerate(raw_rows, start=2):
        try:
            csv_row: CsvRow = parse_row(raw, idx)
        except ValueError as exc:
            log.warning("Linea %s -> OBSERVADO: %s", idx, exc)
            summary.results.append(ProcessResult(
                line_num=idx, status=RowStatus.OBSERVADO,
                cuenta_destino=(raw.get("cuenta_destino") or "").strip(),
                error=f"Fila invalida: {exc}",
            ))
            continue

        if idem_on:
            ref_value = csv_row.referencia or csv_row.descripcion
            if ref_value:
                existing = client.incoming_payment_exists(ref_field, ref_value)
                if existing is not None:
                    log.info("Linea %s -> OMITIDA (DocEntry=%s)", idx, existing)
                    summary.results.append(ProcessResult(
                        line_num=idx, status=RowStatus.OMITIDA,
                        cuenta_destino=csv_row.cuenta_destino,
                        doc_entry=existing,
                        error=f"Ya existe en SAP (DocEntry={existing})",
                    ))
                    continue

        payload = build_payload(csv_row, accounts_cfg)
        if idem_on:
            ref_value = csv_row.referencia or csv_row.descripcion
            if ref_value:
                payload[ref_field] = ref_value

        log.info("Linea %s | %s | moneda=%s | monto=%.2f | pc=%s | payload OK",
                 idx, csv_row.tipo_pago.value, csv_row.moneda,
                 csv_row.monto, csv_row.centro_costo)
        log.debug("  payload: %s", payload)

        outcome = client.post_incoming_payment(payload)
        if outcome.ok:
            log.info("  -> EXITO (DocEntry=%s, DocNum=%s)",
                     outcome.doc_entry, outcome.doc_num)
            summary.results.append(ProcessResult(
                line_num=idx, status=RowStatus.EXITO,
                cuenta_destino=csv_row.cuenta_destino,
                doc_entry=outcome.doc_entry, doc_num=outcome.doc_num,
            ))
        else:
            log.error("  -> ERROR HTTP %s: %s", outcome.status_code, outcome.error)
            summary.results.append(ProcessResult(
                line_num=idx, status=RowStatus.ERROR,
                cuenta_destino=csv_row.cuenta_destino,
                error=outcome.error,
            ))

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="SAP Bank Integration — prueba local")
    parser.add_argument("--config",   default="config.yaml")
    parser.add_argument("--xlsx",     default=None)
    parser.add_argument("--sap-real", action="store_true")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    accounts_cfg  = cfg["accounts"]
    idem_cfg      = cfg.get("idempotency", {})
    reports_dir   = cfg["paths"]["reports"]
    inbound_dir   = cfg["paths"]["inbound"]
    processed_dir = cfg["paths"]["processed"]
    errors_dir    = cfg["paths"]["errors"]

    if args.sap_real:
        client = SapClient(cfg["sap"], cfg.get("retry", {}))
        client.ensure_session()
        log.info("=== MODO SAP REAL — las inserciones son reales ===")
        resolver = AccountResolver(
            session=client._session,
            base_url=cfg["sap"]["base_url"],
            verify=cfg["sap"].get("verify_ssl", True),
        )
    else:
        client = DryRunClient()
        class _NoOpResolver:
            def resolve(self, code): return code
            def resolve_many(self, codes): return {c: c for c in codes}
        resolver = _NoOpResolver()

    if args.xlsx:
        xlsx_path = args.xlsx
    else:
        archivos = sorted(f for f in os.listdir(inbound_dir) if f.lower().endswith(".xlsx"))
        if not archivos:
            log.error("No hay archivos .xlsx en '%s'.", inbound_dir)
            return 1
        xlsx_path = os.path.join(inbound_dir, archivos[0])

    if not os.path.exists(xlsx_path):
        log.error("Archivo no encontrado: %s", xlsx_path)
        return 1

    source_name = os.path.basename(xlsx_path)
    log.info("=== Leyendo: %s ===", xlsx_path)

    raw_rows = xlsx_to_raw_rows(xlsx_path, accounts_cfg, resolver)
    if not raw_rows:
        log.warning("El archivo no tiene filas de datos.")
        return 0

    log.info("Filas leidas: %s", len(raw_rows))
    log.info("=== Iniciando pipeline ===")

    summary = run_pipeline(raw_rows, source_name, client, accounts_cfg, idem_cfg)

    os.makedirs(reports_dir, exist_ok=True)
    report_path = write_report(summary, reports_dir)

    if args.sap_real:
        base, ext = os.path.splitext(source_name)
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_name  = f"{base}_{ts}{ext}"
        if summary.all_success:
            os.makedirs(processed_dir, exist_ok=True)
            dest = os.path.join(processed_dir, new_name)
            shutil.move(xlsx_path, dest)
            log.info("Archivo movido a Procesados: %s", dest)
        else:
            os.makedirs(errors_dir, exist_ok=True)
            dest = os.path.join(errors_dir, new_name)
            shutil.move(xlsx_path, dest)
            log.warning("Archivo con incidencias movido a Errores: %s", dest)

    print("\n" + "=" * 60)
    print(f"  RESULTADO: {'OK' if summary.all_success else 'CON INCIDENCIAS'}")
    print("=" * 60)
    print(f"  Total filas     : {summary.total}")
    print(f"  EXITO           : {summary.exitos}")
    print(f"  ERROR           : {summary.errores}")
    print(f"  OBSERVADO       : {summary.observados}")
    print(f"  OMITIDA         : {summary.omitidas}")
    print(f"  Reporte en      : {report_path}")
    print("=" * 60)

    if args.sap_real:
        client.logout()

    return 0 if summary.all_success else 1


if __name__ == "__main__":
    raise SystemExit(main())