from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import yaml

from src.sap_bank.infrastructure.sap_client import DryRunClient, SapClient
from watcher import Watcher


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def setup_logging(log_cfg: dict) -> None:
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_file = log_cfg.get("file")
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=handlers,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="SAP Bank Statement Integration")
    parser.add_argument("--config", default="config.yaml", help="Ruta al config.yaml")
    parser.add_argument("--once", action="store_true", help="Ejecuta un solo ciclo y termina")
    parser.add_argument("--dry-run", action="store_true",
                        help="No inserta en SAP: registra los payloads y deja el archivo en inbound")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.get("logging", {}))
    log = logging.getLogger("main")

    client = DryRunClient() if args.dry_run else SapClient(cfg["sap"], cfg.get("retry", {}))
    watcher = Watcher(
        paths_cfg=cfg["paths"],
        accounts_cfg=cfg["accounts"],
        max_workers=int(cfg.get("concurrency", {}).get("max_workers", 6)),
        client=client,
        idem_cfg=cfg.get("idempotency", {}),
        move_files=not args.dry_run,
    )

    # Login inicial para fallar rapido si las credenciales/URL estan mal
    try:
        client.ensure_session()
    except Exception:
        log.exception("No se pudo establecer la sesion inicial con Service Layer. Abortando.")
        return 1

    interval = int(cfg.get("scheduler", {}).get("interval_seconds", 60))
    log.info("Daemon iniciado. Intervalo=%ss. Inbound=%s", interval, cfg["paths"]["inbound"])

    try:
        while True:
            try:
                watcher.scan_once()
            except Exception:
                log.exception("Error no controlado durante el ciclo de escaneo.")
            if args.once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("Interrumpido por el usuario. Cerrando...")
    finally:
        client.logout()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())