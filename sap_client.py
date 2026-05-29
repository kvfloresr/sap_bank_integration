"""
sap_client.py
-------------
Cliente para SAP Business One Service Layer.

Responsabilidades:
  - Login y gestion de la cookie B1SESSION.
  - POST a IncomingPayments.
  - Re-login transparente ante HTTP 401 (sesion expirada).
  - Reintentos con backoff ante 5xx.
  - Es THREAD-SAFE: una sola sesion/cookie compartida por todos los hilos del
    ThreadPoolExecutor. El re-login se serializa con un lock y un contador de
    "generacion" para que, si varios hilos reciben 401 a la vez, solo UNO
    renueve la sesion y el resto reutilice la cookie ya refrescada.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class SapAuthError(Exception):
    """No se pudo autenticar contra Service Layer."""


@dataclass
class PostOutcome:
    """Resultado de bajo nivel de un POST a IncomingPayments."""
    ok: bool
    status_code: int
    doc_entry: Optional[int] = None
    doc_num: Optional[int] = None
    error: str = ""


class SapClient:
    def __init__(self, sap_cfg: dict, retry_cfg: dict):
        self.base_url: str = sap_cfg["base_url"].rstrip("/")
        self.company_db: str = sap_cfg["company_db"]
        self.username: str = sap_cfg["username"]
        self.password: str = sap_cfg["password"]
        self.verify = sap_cfg.get("verify_ssl", True)

        self.max_attempts: int = int(retry_cfg.get("max_attempts", 3))
        self.backoff_seconds: float = float(retry_cfg.get("backoff_seconds", 5))

        self._session = requests.Session()
        self._login_lock = threading.Lock()
        # _generation cambia cada vez que se hace login con exito. Sirve para que
        # los hilos sepan si "su" sesion ya quedo obsoleta y otro la renovo.
        self._generation = 0

        # Silenciar el warning de certificado auto-firmado cuando verify=False
        if self.verify is False:
            try:
                requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
            except Exception:
                pass

    # ------------------------------------------------------------------ login

    def login(self) -> None:
        """Hace login y guarda la cookie B1SESSION en la sesion. Thread-safe."""
        with self._login_lock:
            self._do_login_locked()

    def _do_login_locked(self) -> None:
        """Debe llamarse con _login_lock ya tomado."""
        url = f"{self.base_url}/Login"
        payload = {
            "CompanyDB": self.company_db,
            "UserName": self.username,
            "Password": self.password,
        }
        try:
            resp = self._session.post(url, json=payload, verify=self.verify, timeout=30)
        except requests.RequestException as exc:
            raise SapAuthError(f"Fallo de red en Login: {exc}") from exc

        if resp.status_code != 200:
            raise SapAuthError(
                f"Login rechazado por Service Layer (HTTP {resp.status_code}): {resp.text[:300]}"
            )
        # La cookie B1SESSION queda registrada automaticamente en self._session.
        self._generation += 1
        logger.info("Login en Service Layer OK (generacion=%s)", self._generation)

    def ensure_session(self) -> None:
        """Asegura que exista una sesion antes de empezar a procesar."""
        if "B1SESSION" not in self._session.cookies.get_dict():
            self.login()

    def _relogin_if_stale(self, seen_generation: int) -> None:
        """
        Re-login serializado. Si la generacion no cambio desde que el hilo empezo
        su peticion, este hilo renueva la sesion. Si otro hilo ya la renovo
        (generacion distinta), no hace nada y reutiliza la cookie nueva.
        """
        with self._login_lock:
            if self._generation == seen_generation:
                logger.warning("Sesion expirada (HTTP 401). Re-login...")
                self._do_login_locked()
            else:
                logger.debug("Otro hilo ya renovo la sesion. Reutilizando cookie.")

    # ------------------------------------------------------- incoming payment

    def post_incoming_payment(self, payload: dict) -> PostOutcome:
        """
        Inserta un IncomingPayment. Maneja:
          201 -> exito (devuelve DocEntry / DocNum)
          401 -> re-login + 1 reintento
          400 -> error de datos, NO reintenta
          5xx -> reintenta con backoff hasta max_attempts
        """
        url = f"{self.base_url}/IncomingPayments"
        attempt = 0
        relogin_done = False

        while True:
            attempt += 1
            generation_before = self._generation
            try:
                resp = self._session.post(
                    url, json=payload, verify=self.verify, timeout=60
                )
            except requests.RequestException as exc:
                # Error de red: tratar como 5xx (reintentable)
                if attempt < self.max_attempts:
                    self._sleep_backoff(attempt)
                    continue
                return PostOutcome(False, 0, error=f"Error de red tras {attempt} intentos: {exc}")

            code = resp.status_code

            if code == 201:
                data = self._safe_json(resp)
                return PostOutcome(
                    ok=True,
                    status_code=201,
                    doc_entry=data.get("DocEntry"),
                    doc_num=data.get("DocNum"),
                )

            if code == 401 and not relogin_done:
                self._relogin_if_stale(generation_before)
                relogin_done = True
                attempt -= 1  # el reintento por 401 no consume cupo de 5xx
                continue

            if code == 400:
                return PostOutcome(False, 400, error=self._extract_sap_error(resp))

            if code in (500, 502, 503):
                if attempt < self.max_attempts:
                    self._sleep_backoff(attempt)
                    continue
                return PostOutcome(
                    False, code,
                    error=f"Agotados {self.max_attempts} reintentos. Ultimo error: "
                          f"{self._extract_sap_error(resp)}",
                )

            # Cualquier otro codigo no esperado
            return PostOutcome(False, code, error=self._extract_sap_error(resp))

    # ----------------------------------------------------------------- helpers

    def incoming_payment_exists(self, field: str, value: str) -> Optional[int]:
        """
        Idempotencia: busca un IncomingPayment cuyo `field` sea igual a `value`.
        Devuelve el DocEntry del documento existente, o None si no existe.
        Hace un GET con $filter/$select/$top=1 y maneja re-login en 401.
        """
        # Escapar comilla simple segun OData (se duplica)
        safe = value.replace("'", "''")
        flt = f"{field} eq '{safe}'"
        url = f"{self.base_url}/IncomingPayments"
        params = {"$filter": flt, "$select": "DocEntry", "$top": 1}

        relogin_done = False
        while True:
            generation_before = self._generation
            try:
                resp = self._session.get(
                    url, params=params, verify=self.verify, timeout=30
                )
            except requests.RequestException as exc:
                logger.warning("Fallo de red en chequeo de idempotencia: %s", exc)
                return None  # ante la duda, no bloqueamos el POST

            if resp.status_code == 200:
                data = self._safe_json(resp)
                rows = data.get("value", [])
                return rows[0].get("DocEntry") if rows else None

            if resp.status_code == 401 and not relogin_done:
                self._relogin_if_stale(generation_before)
                relogin_done = True
                continue

            logger.warning(
                "Chequeo de idempotencia devolvio HTTP %s: %s",
                resp.status_code, self._extract_sap_error(resp),
            )
            return None  # no pudimos verificar; dejamos que el POST decida


    def _sleep_backoff(self, attempt: int) -> None:
        wait = self.backoff_seconds * attempt
        logger.warning("Reintentando en %.1fs (intento %s)...", wait, attempt + 1)
        time.sleep(wait)

    @staticmethod
    def _safe_json(resp: requests.Response) -> dict:
        try:
            return resp.json()
        except ValueError:
            return {}

    def _extract_sap_error(self, resp: requests.Response) -> str:
        """Extrae el mensaje de error de SAP del cuerpo JSON, si existe."""
        data = self._safe_json(resp)
        try:
            msg = data["error"]["message"]
            if isinstance(msg, dict):
                return str(msg.get("value", msg))
            return str(msg)
        except (KeyError, TypeError):
            return resp.text[:300] if resp.text else f"HTTP {resp.status_code} sin cuerpo"

    def logout(self) -> None:
        """Cierra la sesion en Service Layer (cortesia, libera la sesion del lado SAP)."""
        try:
            self._session.post(f"{self.base_url}/Logout", verify=self.verify, timeout=15)
        except requests.RequestException:
            pass
