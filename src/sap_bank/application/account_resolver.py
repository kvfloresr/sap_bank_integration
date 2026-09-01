from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class AccountResolver:
    """
    Resuelve codigos de cuentas del Excel al Code interno de SAP.

    Estrategia de resolucion (en orden):
    1. Buscar por ExternalCode eq 'codigo'  (ej: '111500001-0')
    2. Buscar por Code eq 'codigo'           (ej: '_SYS00000000032')
    3. Si no encuentra nada, devuelve el codigo original y SAP decidira
    si es valido o no (el error quedara registrado en el reporte).
    """

    def __init__(self, session: requests.Session, base_url: str, verify):
        self._session  = session
        self._base_url = base_url.rstrip("/")
        self._verify   = verify
        self._cache: dict[str, str] = {}   # codigo_original -> Code interno SAP

    def resolve(self, code: str) -> str:
        """
        Devuelve el Code interno de SAP para el codigo dado.
        Usa cache para no repetir GETs en el mismo archivo.
        """
        if not code:
            return code

        if code in self._cache:
            logger.debug("Cache hit: '%s' -> '%s'", code, self._cache[code])
            return self._cache[code]

        # Intentar resolver
        resolved = self._fetch_by_external_code(code) \
                or self._fetch_by_format_code(code) \
                or self._fetch_by_code(code)

        if resolved:
            logger.info("Cuenta resuelta: '%s' -> '%s'", code, resolved)
            self._cache[code] = resolved
            return resolved

        # No se pudo resolver — usar el codigo original
        logger.warning(
            "No se pudo resolver la cuenta '%s' en SAP. "
            "Se usara el codigo original — SAP puede rechazarlo.", code
        )
        self._cache[code] = code
        return code

    def _fetch_by_external_code(self, code: str) -> Optional[str]:
        """Busca la cuenta por ExternalCode (codigo del banco, ej: '111500001-0')."""
        safe = code.replace("'", "''")
        try:
            resp = self._session.get(
                f"{self._base_url}/ChartOfAccounts",
                params={
                    "$filter": f"ExternalCode eq '{safe}'",
                    "$select": "Code,Name,ExternalCode",
                    "$top": 1,
                },
                verify=self._verify,
                timeout=15,
            )
            if resp.status_code == 200:
                rows = resp.json().get("value", [])
                if rows:
                    logger.debug("ExternalCode '%s' -> Code='%s' Name='%s'",
                                code, rows[0]["Code"], rows[0].get("Name"))
                    return rows[0]["Code"]
        except Exception as exc:
            logger.warning("Error buscando ExternalCode '%s': %s", code, exc)
        return None

    def _fetch_by_format_code(self, code: str) -> Optional[str]:
        """Busca la cuenta por FormatCode (ej: '52101011' sin guión)."""
        safe = code.replace("'", "''")
        try:
            resp = self._session.get(
                f"{self._base_url}/ChartOfAccounts",
                params={
                    "$filter": f"FormatCode eq '{safe}'",
                    "$select": "Code,Name,FormatCode",
                    "$top": 1,
                },
                verify=self._verify,
                timeout=15,
            )
            if resp.status_code == 200:
                rows = resp.json().get("value", [])
                if rows:
                    logger.debug("FormatCode '%s' -> Code='%s' Name='%s'",
                                code, rows[0]["Code"], rows[0].get("Name"))
                    return rows[0]["Code"]
        except Exception as exc:
            logger.warning("Error buscando FormatCode '%s': %s", code, exc)
        return None

    def _fetch_by_code(self, code: str) -> Optional[str]:
        """Busca la cuenta por Code directo (por si el Excel ya trae el _SYS...)."""
        safe = code.replace("'", "''")
        try:
            resp = self._session.get(
                f"{self._base_url}/ChartOfAccounts",
                params={
                    "$filter": f"Code eq '{safe}'",
                    "$select": "Code,Name",
                    "$top": 1,
                },
                verify=self._verify,
                timeout=15,
            )
            if resp.status_code == 200:
                rows = resp.json().get("value", [])
                if rows:
                    logger.debug("Code directo '%s' encontrado: Name='%s'",
                                code, rows[0].get("Name"))
                    return rows[0]["Code"]
        except Exception as exc:
            logger.warning("Error buscando Code '%s': %s", code, exc)
        return None

    def resolve_many(self, codes: list[str]) -> dict[str, str]:
        """Resuelve una lista de codigos de una vez. Devuelve dict original->interno."""
        return {c: self.resolve(c) for c in set(codes) if c}