"""
cli.py  (interfaces)
--------------------
Punto de entrada de linea de comandos para probar el pipeline completo.
Lee un .xlsx, lo procesa y genera el reporte de conciliacion.

Reemplaza al antiguo test_local.py, ahora usando la arquitectura por capas:
  infrastructure.excel_reader  -> lee el Excel
  application.processor        -> parsea, construye payload, postea
  infrastructure.report_writer -> genera el .xlsx de conciliacion

Uso (desde la raiz del proyecto C:\\Proyectos\\sap_bank_integration):
    python -m src.sap_bank.interfaces.cli                  # dry-run
    python -m src.sap_bank.interfaces.cli --sap-real       # insercion real
    python -m src.sap_bank.interfaces.cli --xlsx ruta.xlsx # archivo especifico
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from datetime import datetime

import yaml

from src.sap_bank.application.processor import process_rows
from src.sap_bank.infrastructure.excel_reader import read_payments
from src.sap_bank.infrastructure.report_writer import write_report
from src.sap_bank.infrastructure.sap_client import DryRunClient, SapClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cli")


def _load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main() -> int:
    parser = argparse.ArgumentParser(description="SAP Bank Integration — CLI de prueba")
    parser.add_argument("--config",   default="config/config.yaml",
                        help="Ruta al config.yaml (default: config/config.yaml)")
    parser.add_argument("--xlsx",     default=None,
                        help="Archivo .xlsx especifico. Sin esto toma el primero de inbound/")
    parser.add_argument("--sap-real", action="store_true",
                        help="Inserta en SAP real. Sin este flag corre en dry-run.")
    args = parser.parse_args()

    cfg = _load_config(args.config)

    accounts_cfg  = cfg["accounts"]
    idem_cfg      = cfg.get("idempotency", {})
    max_workers   = int(cfg.get("concurrency", {}).get("max_workers", 6))
    reports_dir   = cfg["paths"]["reports"]
    inbound_dir   = cfg["paths"]["inbound"]
    processed_dir = cfg["paths"]["processed"]
    errors_dir    = cfg["paths"]["errors"]

    # Cliente SAP (real o simulado)
    if args.sap_real:
        client = SapClient(cfg["sap"], cfg.get("retry", {}))
        client.ensure_session()
        log.info("=== MODO SAP REAL — las inserciones son reales ===")
    else:
        client = DryRunClient()
        log.info("=== MODO DRY-RUN — no se insertara nada en SAP ===")

    # Resolver el archivo de entrada
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

    # 1. Leer el Excel (infrastructure)
    raw_rows = read_payments(xlsx_path)
    if not raw_rows:
        log.warning("El archivo no tiene filas de datos validas.")
        return 0

    log.info("Filas leidas: %s", len(raw_rows))
    log.info("=== Iniciando pipeline ===")

    # 2. Procesar (application)
    summary = process_rows(
        raw_rows     = raw_rows,
        source_name  = source_name,
        client       = client,
        accounts_cfg = accounts_cfg,
        max_workers  = max_workers,
        idem_cfg     = idem_cfg,
    )

    # 3. Reporte (infrastructure)
    os.makedirs(reports_dir, exist_ok=True)
    report_path = write_report(summary, reports_dir)

    # 4. Mover archivo segun resultado (solo en modo real)
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
    print(f"  Total filas : {summary.total}")
    print(f"  EXITO       : {summary.exitos}")
    print(f"  ERROR       : {summary.errores}")
    print(f"  OBSERVADO   : {summary.observados}")
    print(f"  OMITIDA     : {summary.omitidas}")
    print(f"  Reporte en  : {report_path}")
    print("=" * 60)

    if args.sap_real:
        client.logout()

    return 0 if summary.all_success else 1


if __name__ == "__main__":
    raise SystemExit(main())