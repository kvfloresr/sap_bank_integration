"""
test_journal.py
---------------
Prueba local del watcher de JournalEntries.

Uso:
  python test_journal.py                    # dry-run (no envía a SAP)
  python test_journal.py --sap-real         # envía a SAP como ASIENTO PRELIMINAR
  python test_journal.py --txt ruta.txt     # archivo específico
  python test_journal.py --config config/config_bk.yaml
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

import yaml

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("test_journal")

sys.path.insert(0, os.path.dirname(__file__))
from src.sap_bank.interfaces.journal_watcher import (
    parse_txt, build_journal_payload, SapJournalClient, resolve_accounts
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",   default="config/config_bk.yaml")
    parser.add_argument("--txt",      default=None)
    parser.add_argument("--sap-real", action="store_true")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    if args.txt:
        txt_path = args.txt
    else:
        paths    = cfg["paths"]
        inbound  = paths.get("journal_inbound", paths.get("inbound"))
        archivos = sorted(f for f in os.listdir(inbound) if f.lower().endswith(".txt"))
        if not archivos:
            log.error("No hay archivos .txt en '%s'", inbound)
            return 1
        txt_path = os.path.join(inbound, archivos[0])

    log.info("Archivo: %s", txt_path)

    file_date = datetime.fromtimestamp(os.path.getmtime(txt_path)).strftime("%Y-%m-%d")
    memo, lines = parse_txt(txt_path)

    log.info("Fecha documento : %s", file_date)
    log.info("Memo cabecera   : %s", memo)
    log.info("Líneas válidas  : %s", len(lines))

    if not lines:
        log.error("Sin filas válidas.")
        return 1

    # Verificar que el asiento está cuadrado
    total_debe  = sum(l["debe"]  for l in lines)
    total_haber = sum(l["haber"] for l in lines)
    log.info("\nTotal DEBE  : %.2f", total_debe)
    log.info("Total HABER : %.2f", total_haber)
    if abs(total_debe - total_haber) > 0.01:
        log.warning("⚠ Asiento NO cuadrado (diferencia: %.2f)", total_debe - total_haber)
    else:
        log.info("✓ Asiento cuadrado")

    if not args.sap_real:
        # Mostrar payload sin resolver (dry-run)
        payload = build_journal_payload(memo, lines, file_date)
        log.info("\n=== PAYLOAD (dry-run, sin resolver cuentas) ===")
        log.info("ReferenceDate : %s", payload["ReferenceDate"])
        log.info("Memo          : %s", payload["Memo"])
        log.info("Líneas JE     : %s", len(payload["JournalEntryLines"]))
        log.info("Primeras 3 líneas:")
        for l in payload["JournalEntryLines"][:3]:
            log.info("  %s", l)
        log.info("\n=== DRY-RUN — no se envió a SAP ===")
        log.info("Usar --sap-real para insertar en SAP como asiento preliminar")
        return 0

    # ── Modo SAP real ──────────────────────────────────────────
    log.info("\n=== CONECTANDO A SAP ===")
    client = SapJournalClient(cfg["sap"], cfg.get("retry", {}))
    client.login()

    # Resolver cuentas FormatCode -> _SYS interno
    log.info("Resolviendo %s cuentas contables...", len({l["cuenta"] for l in lines}))
    lines_resueltas = resolve_accounts(lines, client)
    if not lines_resueltas:
        log.error("Ninguna cuenta pudo resolverse.")
        client.logout()
        return 1

    log.info("Cuentas resueltas: %s/%s", len(lines_resueltas), len(lines))

    # Construir payload base
    payload = build_journal_payload(memo, lines_resueltas, file_date)

    log.info("\n=== PAYLOAD RESUELTO ===")
    log.info("ReferenceDate : %s", payload["ReferenceDate"])
    log.info("Memo          : %s", payload["Memo"])
    log.info("Líneas JE     : %s", len(payload["JournalEntryLines"]))
    log.info("Primeras 3 líneas:")
    for l in payload["JournalEntryLines"][:3]:
        log.info("  %s", l)

    # Envolver en estructura JournalVouchersService_Add (asiento preliminar)
    payload_voucher = {
        "JournalVoucher": {
            "JournalEntry": {
                "ReferenceDate":     payload["ReferenceDate"],
                "DueDate":           payload["ReferenceDate"],
                "Memo":              payload["Memo"],
                "JournalEntryLines": payload["JournalEntryLines"],
            }
        }
    }

    log.info("\n=== ENVIANDO A SAP (ASIENTO PRELIMINAR) ===")
    result = client.post_journal_voucher(payload_voucher)
    client.logout()

    if result["ok"]:
        log.info("✓ ÉXITO — Asiento preliminar creado — JdtNum: %s", result["jdt_num"])
    else:
        log.error("✗ ERROR SAP: %s", result["error"])
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())