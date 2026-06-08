"""
test_local.py
-------------
Prueba local del pipeline completo.

Lee un archivo .xlsx desde la ruta configurada en config.yaml (paths.inbound),
lo procesa fila por fila con la logica real del sistema y genera el reporte
Excel de conciliacion — exactamente igual que en produccion.

Modos de ejecucion:
    python test_local.py                            # dry-run, sin tocar SAP
    python test_local.py --sap-real                 # insercion real en SAP
    python test_local.py --config otra_config.yaml  # config alternativa
    python test_local.py --xlsx ruta/archivo.xlsx   # archivo especifico

En modo --sap-real:
    - Hace login contra SAP con las credenciales del config.yaml
    - Verifica idempotencia (si idem_cfg.enabled=true) antes de cada POST
    - El reporte mostrara DocEntry y DocNum reales
    - El archivo se mueve a Procesados/ con timestamp en el nombre
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from datetime import date, datetime

import openpyxl
import yaml

sys.path.insert(0, os.path.dirname(__file__))

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
# MAPEO: columnas del Excel del banco → dict que espera parse_row()
#
# Estructura fija del Excel entregado por el banco:
#   A (col 0): MODULO SAP       → determina tipo_pago
#   B (col 1): Código cuenta    → cuenta_caja / cuenta_banco / codigo_tarjeta
#   C (col 2): Nombre cuenta    → descripcion (Remarks en SAP)
#   D (col 3): Cuenta asociada  → cuenta_destino (contrapartida contable)
#   E (col 4): Débito           → monto si la fila es debito  ("BS 10,883.16")
#   F (col 5): Crédito          → monto si la fila es credito ("BS 10,883.16")
#   G (col 6): Comentarios      → descripcion adicional / referencia
#   H (col 7): Unidad Negocio   → no se usa en el payload
#   I (col 8): Centro de Costo  → no se usa en el payload
#   J (col 9): Partida Flujo    → no se usa en el payload
#
# MONEDA: el Excel del banco no trae moneda explicita. Se asume BOB (moneda
# local). Si el banco entrega USD u otra moneda, agregar columna "moneda"
# al Excel y mapearla aqui.
#
# CUENTAS: las cuentas del Excel (cuenta_mayor y cuenta_asoc) deben existir
# en SAP. Si no existen, el sistema usara los defaults del config.yaml.
# ─────────────────────────────────────────────────────────────────────────────

MODULO_A_TIPO: dict[str, str] = {
    "pagos recibidos - banco":   "TRANSFERENCIA",
    "pagos recibidos - cuenta":  "EFECTIVO",
    "pagos recibidos - tarjeta": "TARJETA",
}


def _parse_bs_amount(raw) -> str:
    """Convierte 'BS 10,883.16' o 10883.16 a string '10883.16' para parse_row()."""
    if raw is None:
        return ""
    s = str(raw).strip()
    for prefix in ("BS ", "Bs. ", "Bs ", "$ ", "$"):
        if s.upper().startswith(prefix.upper()):
            s = s[len(prefix):]
            break
    s = s.replace(" ", "")
    if "," in s and "." in s:
        if s.index(",") < s.index("."):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    return s


def xlsx_to_raw_rows(xlsx_path: str, accounts_cfg: dict) -> list[dict]:
    """
    Lee el Excel del banco y devuelve lista de dicts con las claves que
    espera parse_row() de processor.py. Salta encabezado y filas vacias.

    Las cuentas del Excel se usan tal cual. Si vienen vacias, el procesador
    usara los defaults del config.yaml (cash_account / transfer_account).
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    rows_out: list[dict] = []
    #today = date.today().isoformat() 
    today = "2026-02-28"


    for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if i == 1:
            continue
        if all(c is None for c in row):
            continue

        modulo       = str(row[0] or "").strip()
        cuenta_mayor = str(row[1] or "").strip()
        nombre_sn    = str(row[2] or "").strip()
        cuenta_asoc  = str(row[3] or "").strip()
        debito_raw   = row[4]
        credito_raw  = row[5]
        comentario   = str(row[6] or "").strip()

        tipo_raw  = MODULO_A_TIPO.get(modulo.lower(), "TRANSFERENCIA")
        monto_str = _parse_bs_amount(debito_raw) or _parse_bs_amount(credito_raw)
        descripcion = comentario or nombre_sn or modulo

        # Si la cuenta del Excel no existe en TAJIBOS_QA, usar el default
        # del config.yaml segun el tipo de pago. Esto evita el error 400
        # "Account is invalid" durante las pruebas. En produccion las cuentas
        # del Excel deben existir en SAP y este fallback no deberia activarse.
        if tipo_raw == "EFECTIVO" and not cuenta_mayor:
            cuenta_mayor = accounts_cfg.get("cash_account", "")
        elif tipo_raw == "TRANSFERENCIA" and not cuenta_mayor:
            cuenta_mayor = accounts_cfg.get("transfer_account", "")

        # cuenta_destino: si viene del Excel se usa tal cual.
        # Si no existe en SAP el POST fallara con 400 y quedara como ERROR
        # en el reporte — comportamiento correcto para produccion.

        d: dict = {
            "fecha":          today,
            "descripcion":    descripcion,
            "tipo_pago":      tipo_raw,
            "monto":          monto_str,
            "cuenta_destino": cuenta_asoc,
            "moneda":         "BOB",
            "cuenta_caja":    cuenta_mayor if tipo_raw == "EFECTIVO"      else "",
            "cuenta_banco":   cuenta_mayor if tipo_raw == "TRANSFERENCIA" else "",
            "codigo_tarjeta": cuenta_mayor if tipo_raw == "TARJETA"       else "",
            "num_cupon":      "",
            "referencia":     comentario,
        }
        rows_out.append(d)
        log.debug("Fila Excel %s -> tipo=%s monto=%s cuenta_mayor=%s destino=%s",
                i, tipo_raw, monto_str, cuenta_mayor, cuenta_asoc)

    return rows_out


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    raw_rows: list[dict],
    source_name: str,
    client,
    accounts_cfg: dict,
    idem_cfg: dict,
) -> FileSummary:
    """
    Parsea, construye payload y postea cada fila.
    Identico a process_file() de processor.py pero secuencial (sin pool de
    hilos) para que el output de consola sea legible durante las pruebas.
    """
    summary = FileSummary(source_name=source_name)
    idem_on   = bool(idem_cfg.get("enabled"))
    ref_field = idem_cfg.get("reference_field", "U_RefBanco")

    for idx, raw in enumerate(raw_rows, start=2):

        # 1. Parseo
        try:
            csv_row: CsvRow = parse_row(raw, idx)
        except ValueError as exc:
            log.warning("Linea %s -> OBSERVADO: %s", idx, exc)
            summary.results.append(ProcessResult(
                line_num=idx,
                status=RowStatus.OBSERVADO,
                cuenta_destino=(raw.get("cuenta_destino") or "").strip(),
                error=f"Fila invalida: {exc}",
            ))
            continue

        # 2. Idempotencia — solo si esta habilitada en config.yaml
        if idem_on:
            ref_value = csv_row.referencia or csv_row.descripcion
            if ref_value:
                existing = client.incoming_payment_exists(ref_field, ref_value)
                if existing is not None:
                    log.info("Linea %s -> OMITIDA (ya existe DocEntry=%s)", idx, existing)
                    summary.results.append(ProcessResult(
                        line_num=idx,
                        status=RowStatus.OMITIDA,
                        cuenta_destino=csv_row.cuenta_destino,
                        doc_entry=existing,
                        error=f"Ya existe en SAP (DocEntry={existing})",
                    ))
                    continue

        # 3. Construir payload
        payload = build_payload(csv_row, accounts_cfg)

        # Agregar referencia al documento SAP solo si idempotencia esta activa
        if idem_on:
            ref_value = csv_row.referencia or csv_row.descripcion
            if ref_value:
                payload[ref_field] = ref_value

        log.info("Linea %s | %s | moneda=%s | monto=%.2f | payload OK",
                idx, csv_row.tipo_pago.value, csv_row.moneda, csv_row.monto)
        log.debug("  payload: %s", payload)

        # 4. POST a SAP
        outcome = client.post_incoming_payment(payload)

        if outcome.ok:
            log.info("  -> EXITO (DocEntry=%s, DocNum=%s)", outcome.doc_entry, outcome.doc_num)
            summary.results.append(ProcessResult(
                line_num=idx,
                status=RowStatus.EXITO,
                cuenta_destino=csv_row.cuenta_destino,
                doc_entry=outcome.doc_entry,
                doc_num=outcome.doc_num,
            ))
        else:
            log.error("  -> ERROR HTTP %s: %s", outcome.status_code, outcome.error)
            summary.results.append(ProcessResult(
                line_num=idx,
                status=RowStatus.ERROR,
                cuenta_destino=csv_row.cuenta_destino,
                error=outcome.error,
            ))

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Prueba local — SAP Bank Integration")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--xlsx", default=None,
                        help="Archivo .xlsx especifico. Sin este flag toma el primero de inbound/")
    parser.add_argument("--sap-real", action="store_true",
                        help="Inserta en SAP real. Sin este flag corre en dry-run.")
    args = parser.parse_args()

    # Cargar config
    with open(args.config, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    accounts_cfg  = cfg["accounts"]
    idem_cfg      = cfg.get("idempotency", {})
    reports_dir   = cfg["paths"]["reports"]
    inbound_dir   = cfg["paths"]["inbound"]
    processed_dir = cfg["paths"]["processed"]
    errors_dir    = cfg["paths"]["errors"]

    # Cliente SAP
    if args.sap_real:
        client = SapClient(cfg["sap"], cfg.get("retry", {}))
        client.ensure_session()
        log.info("=== MODO SAP REAL — las inserciones son reales ===")
    else:
        client = DryRunClient()

    # Resolver archivo de entrada
    if args.xlsx:
        xlsx_path = args.xlsx
    else:
        archivos = sorted(
            f for f in os.listdir(inbound_dir) if f.lower().endswith(".xlsx")
        )
        if not archivos:
            log.error("No hay archivos .xlsx en '%s'.", inbound_dir)
            return 1
        xlsx_path = os.path.join(inbound_dir, archivos[0])

    if not os.path.exists(xlsx_path):
        log.error("Archivo no encontrado: %s", xlsx_path)
        return 1

    source_name = os.path.basename(xlsx_path)
    log.info("=== Leyendo: %s ===", xlsx_path)

    raw_rows = xlsx_to_raw_rows(xlsx_path, accounts_cfg)
    if not raw_rows:
        log.warning("El archivo no tiene filas de datos.")
        return 0

    log.info("Filas leidas: %s", len(raw_rows))
    log.info("=== Iniciando pipeline ===")

    summary = run_pipeline(raw_rows, source_name, client, accounts_cfg, idem_cfg)

    # Reporte Excel — se genera siempre
    os.makedirs(reports_dir, exist_ok=True)
    report_path = write_report(summary, reports_dir)

    # Mover archivo solo en modo SAP real
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

    # Resumen en consola
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