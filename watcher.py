"""
watcher.py
----------
Escanea la carpeta Inbound, bloquea cada CSV con un archivo .lock, lo procesa,
genera el reporte y mueve el archivo a Procesados/ o Errores/ segun el resultado.
El lock se libera SIEMPRE en el bloque finally.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime

import processor
import report_writer
from sap_client import SapClient

logger = logging.getLogger(__name__)


class Watcher:
    def __init__(self, paths_cfg: dict, accounts_cfg: dict, max_workers: int,
                 client: SapClient, idem_cfg: dict | None = None):
        self.inbound = paths_cfg["inbound"]
        self.processed = paths_cfg["processed"]
        self.errors = paths_cfg["errors"]
        self.reports = paths_cfg["reports"]
        self.accounts_cfg = accounts_cfg
        self.max_workers = max_workers
        self.client = client
        self.idem_cfg = idem_cfg or {}
        for d in (self.inbound, self.processed, self.errors, self.reports):
            os.makedirs(d, exist_ok=True)

    def scan_once(self) -> None:
        """Un ciclo de escaneo. Procesa todos los CSV no bloqueados de Inbound."""
        for name in sorted(os.listdir(self.inbound)):
            if not name.lower().endswith(".csv"):
                continue
            src = os.path.join(self.inbound, name)
            lock = src + ".lock"

            if os.path.exists(lock):
                logger.debug("Saltando '%s': lock activo.", name)
                continue

            self._process_with_lock(name, src, lock)

    def _process_with_lock(self, name: str, src: str, lock: str) -> None:
        try:
            # Crear el lock de forma atomica. Si otro proceso lo creo justo
            # ahora, x falla con FileExistsError y saltamos el archivo.
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
            except FileExistsError:
                logger.debug("Lock recien creado por otra instancia. Saltando '%s'.", name)
                return

            logger.info("Procesando archivo '%s'...", name)
            summary = processor.process_file(
                path=src,
                source_name=name,
                client=self.client,
                accounts_cfg=self.accounts_cfg,
                max_workers=self.max_workers,
                idem_cfg=self.idem_cfg,
            )

            # El reporte se genera SIEMPRE
            report_writer.write_report(summary, self.reports)

            # Destino segun resultado
            dest_dir = self.processed if summary.all_success else self.errors
            self._move(src, dest_dir, name)

        except Exception:
            logger.exception("Excepcion critica procesando '%s'. Se mueve a Errores/.", name)
            try:
                self._move(src, self.errors, name)
            except Exception:
                logger.exception("Tampoco se pudo mover '%s' a Errores/.", name)
        finally:
            # El lock se libera SIEMPRE
            if os.path.exists(lock):
                try:
                    os.remove(lock)
                except OSError:
                    logger.warning("No se pudo eliminar el lock '%s'.", lock)

    @staticmethod
    def _move(src: str, dest_dir: str, name: str) -> None:
        if not os.path.exists(src):
            return
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        # Evitar colision de nombres: si ya existe, anteponer timestamp
        if os.path.exists(dest):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(dest_dir, f"{ts}_{name}")
        shutil.move(src, dest)
        logger.info("Archivo movido a %s", dest)
