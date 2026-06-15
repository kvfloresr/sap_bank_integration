"""
watcher.py
----------
Escanea la carpeta Inbound buscando archivos .xlsx, bloquea cada uno con un
archivo .lock, lo procesa, genera el reporte y mueve el archivo a
Procesados/ (con timestamp en el nombre) o Errores/ segun el resultado.
El lock se libera SIEMPRE en el bloque finally.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime

import src.sap_bank.application.processor as processor
import src.sap_bank.infrastructure.report_writer as report_writer
from src.sap_bank.infrastructure.sap_client import SapClient

logger = logging.getLogger(__name__)


class Watcher:
    def __init__(
        self,
        paths_cfg: dict,
        accounts_cfg: dict,
        max_workers: int,
        client: SapClient,
        idem_cfg: dict | None = None,
        move_files: bool = True,
    ):
        self.inbound   = paths_cfg["inbound"]
        self.processed = paths_cfg["processed"]
        self.errors    = paths_cfg["errors"]
        self.reports   = paths_cfg["reports"]
        self.accounts_cfg = accounts_cfg
        self.max_workers  = max_workers
        self.client       = client
        self.idem_cfg     = idem_cfg or {}
        self.move_files   = move_files          # False en dry-run: no mueve nada

        for d in (self.inbound, self.processed, self.errors, self.reports):
            os.makedirs(d, exist_ok=True)

    def scan_once(self) -> None:
        """
        Un ciclo de escaneo. Procesa todos los .xlsx no bloqueados de Inbound/.
        Los archivos se ordenan alfabeticamente para procesamiento determinista.
        """
        found = False
        for name in sorted(os.listdir(self.inbound)):
            if not name.lower().endswith(".xlsx"):
                continue
            found = True
            src  = os.path.join(self.inbound, name)
            lock = src + ".lock"

            if os.path.exists(lock):
                logger.debug("Saltando '%s': lock activo.", name)
                continue

            self._process_with_lock(name, src, lock)

        if not found:
            logger.debug("Sin archivos .xlsx en '%s'.", self.inbound)

    def _process_with_lock(self, name: str, src: str, lock: str) -> None:
        try:
            # Crear el lock de forma atomica. Si otro proceso lo creo justo
            # ahora, os.open falla con FileExistsError y saltamos el archivo.
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

            # El reporte se genera SIEMPRE, independientemente del resultado
            report_writer.write_report(summary, self.reports)

            if self.move_files:
                dest_dir = self.processed if summary.all_success else self.errors
                self._move_with_timestamp(src, dest_dir, name)
            else:
                logger.info("[DRY-RUN] Archivo '%s' no se mueve (move_files=False).", name)

        except Exception:
            logger.exception("Excepcion critica procesando '%s'. Se mueve a Errores/.", name)
            if self.move_files:
                try:
                    self._move_with_timestamp(src, self.errors, name)
                except Exception:
                    logger.exception("Tampoco se pudo mover '%s' a Errores/.", name)
        finally:
            # El lock se libera SIEMPRE, incluso si hubo excepcion
            if os.path.exists(lock):
                try:
                    os.remove(lock)
                except OSError:
                    logger.warning("No se pudo eliminar el lock '%s'.", lock)

    @staticmethod
    def _move_with_timestamp(src: str, dest_dir: str, name: str) -> None:
        """
        Mueve el archivo a dest_dir agregando la fecha y hora al nombre.
        Patron: <nombre_sin_extension>_YYYYMMDD_HHMMSS.xlsx
        Ejemplo: EstadoCuenta_BNB_20260602_160530.xlsx

        Si el archivo origen ya no existe (movido externamente), lo ignora.
        """
        if not os.path.exists(src):
            logger.warning("Archivo '%s' ya no existe al intentar moverlo.", src)
            return

        os.makedirs(dest_dir, exist_ok=True)

        # Construir nombre con timestamp
        base, ext = os.path.splitext(name)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_name = f"{base}_{ts}{ext}"
        dest = os.path.join(dest_dir, new_name)

        shutil.move(src, dest)
        logger.info("Archivo movido a: %s", dest)