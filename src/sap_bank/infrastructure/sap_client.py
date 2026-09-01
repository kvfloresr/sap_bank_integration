"""
sap_client.py — Cliente SAP Service Layer
Bolivian Foods (BOLIVIAN_FOODS_PROD)

Endpoint de asientos: JournalVouchersService_Add (asientos preliminares)
Estructura validada por Rolando — 28/08/2026
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# BUILDER DE PAYLOAD — Bolivian Foods
# ══════════════════════════════════════════════════════════════

def _build_bf_line(cuenta: str, debe: float, haber: float,
                   detalle: str = "", glosa: str = "",
                   unidad: str = "", cc: str = "",
                   udf_override: dict | None = None) -> dict:
    """
    Construye una línea de asiento con los UDFs requeridos por Bolivian Foods.
    Los campos U_* son los campos de usuario obligatorios validados.
    udf_override permite sobreescribir UDFs específicos por línea (ej. U_IMPORTE).
    """
    line: dict[str, Any] = {
        "AccountCode":      cuenta,
        "Credit":           round(haber, 2),
        "Debit":            round(debe, 2),

        # Memo de línea
        "LineMemo":         detalle,

        # UDFs obligatorios Bolivian Foods (valores base)
        "U_NUMPOL":         "N/A",
        "U_TIPOCOM":        1,
        "U_EXENTO":         0,
        "U_ICE":            0,
        "U_TIPODOC":        10,
        "U_SERIECOD":       0,
        "U_DESCTOBR":       0,
        "U_NumContrato":    0,
        "U_MontoDoc":       0.00,
        "U_MonPag":         0.00,
        "U_MonAcu":         0.00,
        "U_IMPORTE":        round(debe + haber, 2),  # monto de la línea
        "U_TASACERO":       0,
        "U_CODFORPI":       "0",
        "U_BOLBSP":         0,
        "U_NROTRAM":        "0",
        "U_ESTADOFC":       "V",
        "U_IEHD":           0,
        "U_IPJ":            0,
        "U_TASAS":          0,
        "U_OP_EXENTO":      0,
        "U_B_cuf":          "0",
        "U_GIFTCARD":       0.00,
        "U_TIPO_CONTRATO":  1,
        "U_TIPO_TRAN":      1,
        "U_NUM_CONTRATO":   "0",
    }

    # Campos opcionales
    if glosa:
        line["Reference1"] = glosa
    if unidad:
        line["CostingCode"]  = unidad   # Unidad de Negocio
    if cc:
        line["CostingCode2"] = cc       # Centro de Costo

    # Sobreescribir UDFs si se pasan explícitamente
    if udf_override:
        line.update(udf_override)

    return line


def build_journal_voucher_payload(memo: str, lines: list[dict],
                                   ref_date: str) -> dict:
    """
    Construye el payload completo para JournalVouchersService_Add.

    Estructura validada:
        { "JournalVoucher": { "JournalEntry": { ... "JournalEntryLines": [...] } } }

    Cada elemento de `lines` debe tener:
        cuenta   (str)   — AccountCode / _SYS...
        debe     (float)
        haber    (float)
        detalle  (str)   — LineMemo
        glosa    (str)   — Reference1 (opcional)
        unidad   (str)   — CostingCode (opcional)
        cc       (str)   — CostingCode2 (opcional)
        udf      (dict)  — override de UDFs (opcional)
    """
    entry_lines = [
        _build_bf_line(
            cuenta   = l["cuenta"],
            debe     = float(l.get("debe", 0)),
            haber    = float(l.get("haber", 0)),
            detalle  = l.get("detalle", ""),
            glosa    = l.get("glosa", ""),
            unidad   = l.get("unidad", ""),
            cc       = l.get("cc", ""),
            udf_override = l.get("udf"),
        )
        for l in lines
    ]

    return {
        "JournalVoucher": {
            "JournalEntry": {
                "ReferenceDate":     ref_date,
                "DueDate":           ref_date,
                "Memo":              memo,
                "JournalEntryLines": entry_lines,
            }
        }
    }


# ══════════════════════════════════════════════════════════════
# CLIENTE SAP SERVICE LAYER
# ══════════════════════════════════════════════════════════════

class SapJournalClient:
    def __init__(self, sap_cfg: dict, retry_cfg: dict | None = None):
        self.base_url    = sap_cfg["base_url"].rstrip("/")
        self.company_db  = sap_cfg["company_db"]
        self.username    = sap_cfg["username"]
        self.password    = sap_cfg["password"]
        self.verify_ssl  = sap_cfg.get("verify_ssl", False)
        retry_cfg        = retry_cfg or {}
        self.max_retries = retry_cfg.get("max_attempts", 3)
        self.backoff     = retry_cfg.get("backoff_seconds", 5)
        self._session    = requests.Session()
        self._logged_in  = False

    # ── Sesión ───────────────────────────────────────────────

    def login(self) -> None:
        url  = f"{self.base_url}/Login"
        body = {
            "CompanyDB": self.company_db,
            "UserName":  self.username,
            "Password":  self.password,
        }
        r = self._session.post(url, json=body, verify=self.verify_ssl, timeout=30)
        r.raise_for_status()
        self._logged_in = True
        log.info("Login SAP OK — %s", self.company_db)

    def logout(self) -> None:
        try:
            self._session.post(
                f"{self.base_url}/Logout",
                verify=self.verify_ssl, timeout=10
            )
        except Exception:
            pass
        self._logged_in = False
        log.info("Logout SAP OK")

    def _ensure_session(self) -> None:
        if not self._logged_in:
            self.login()

    # ── Inserción de asiento preliminar ──────────────────────

    def post_journal_voucher(self, payload: dict) -> dict:
        """
        POST /JournalVouchersService_Add — asiento preliminar.

        Retorna:
            { "ok": bool, "jdt_num": int | None, "error": str }

        Respuesta exitosa de SAP: HTTP 200 con el voucher creado.
        """
        self._ensure_session()
        url = f"{self.base_url}/JournalVouchersService_Add"

        for attempt in range(1, self.max_retries + 1):
            try:
                r = self._session.post(
                    url, json=payload,
                    verify=self.verify_ssl, timeout=60
                )

                # ── Éxito ──
                if r.status_code == 200:
                    data = r.json()
                    # El número de voucher puede venir en distintos campos
                    jdt_num = (
                        data.get("JdtNum") or
                        data.get("Number") or
                        data.get("AbsEntry")
                    )
                    log.info("✓ Asiento preliminar creado — JdtNum/Number: %s", jdt_num)
                    return {"ok": True, "jdt_num": jdt_num, "error": ""}

                # ── Sesión expirada ──
                if r.status_code == 401:
                    log.warning("Sesión expirada — re-login (intento %s)", attempt)
                    self._logged_in = False
                    self.login()
                    continue

                # ── Error de validación SAP (no reintentar) ──
                if r.status_code in (400, 405):
                    try:
                        err = r.json()
                        msg = err.get("error", {}).get("message", {}).get("value", r.text)
                        code = err.get("error", {}).get("code", "")
                    except Exception:
                        msg, code = r.text, ""
                    log.error("SAP error %s [%s]: %s", r.status_code, code, msg)
                    return {"ok": False, "jdt_num": None, "error": f"[{code}] {msg}"}

                # ── Error de servidor — reintentar con backoff ──
                if r.status_code in (500, 502, 503):
                    wait = self.backoff * attempt
                    log.warning("SAP %s — reintento %s/%s en %ss",
                                r.status_code, attempt, self.max_retries, wait)
                    time.sleep(wait)
                    continue

                # ── Cualquier otro status ──
                log.error("SAP respuesta inesperada %s: %s", r.status_code, r.text[:300])
                return {"ok": False, "jdt_num": None,
                        "error": f"HTTP {r.status_code}: {r.text[:200]}"}

            except requests.exceptions.Timeout:
                log.warning("Timeout (intento %s/%s)", attempt, self.max_retries)
                if attempt < self.max_retries:
                    time.sleep(self.backoff * attempt)
            except requests.exceptions.RequestException as exc:
                log.error("Error de red: %s", exc)
                return {"ok": False, "jdt_num": None, "error": str(exc)}

        return {"ok": False, "jdt_num": None,
                "error": f"Agotados {self.max_retries} reintentos"}

    def post_journal_entry(self, payload: dict) -> dict:
        """
        Alias de compatibilidad → post_journal_voucher().
        Mantiene el watcher existente funcionando sin cambios.
        El payload debe ser la estructura JournalVouchersService_Add.
        """
        return self.post_journal_voucher(payload)