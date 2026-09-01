"""
journal_watcher.py  (src/sap_bank/interfaces/journal_watcher.py)
----------------------------------------------------------------
Watcher automatico para Bolivian Foods.
Detecta archivos .txt en la carpeta Inbound, los convierte en un
JournalVoucher (asiento PRELIMINAR) de SAP Business One (Service Layer).

Estructura del TXT (delimitado por |):
UNIDAD DE NEGOCIO | CENTRO DE COSTO | CUENTA | CUENTA ASOCIADA |
DETALLE | DEBE | HABER | GLOSA
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from datetime import datetime

import requests
import urllib3
import yaml
from src.sap_bank.application.account_resolver import AccountResolver

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SCAN_INTERVAL = 30
MAX_LOG_DIRS  = 5
LOCK_EXT      = ".lock"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bf_watcher")


# ══════════════════════════════════════════════════════════════
# LECTOR DE TXT
# ══════════════════════════════════════════════════════════════

def parse_txt(path: str) -> tuple[str, list[dict]]:
    lines: list[dict] = []
    memo  = ""

    with open(path, encoding="utf-8-sig") as fh:
        for i, raw in enumerate(fh):
            row = raw.strip()
            if not row:
                continue
            cols = row.split("|")
            if i == 0:
                continue

            while len(cols) < 8:
                cols.append("")

            unidad    = cols[0].strip()
            cc        = cols[1].strip()
            cuenta    = cols[2].strip()
            detalle   = cols[4].strip()
            debe_raw  = cols[5].strip().replace(",", ".")
            haber_raw = cols[6].strip().replace(",", ".")
            glosa     = cols[7].strip() if len(cols) > 7 else ""

            try:
                debe  = float(debe_raw)  if debe_raw  else 0.0
                haber = float(haber_raw) if haber_raw else 0.0
            except ValueError:
                log.warning("Fila %s ignorada — monto no numérico: %s", i + 1, raw.strip())
                continue

            if debe == 0.0 and haber == 0.0:
                continue

            if not cuenta:
                log.warning("Fila %s ignorada — sin cuenta contable.", i + 1)
                continue

            cuenta = cuenta.replace("-", "")

            if not memo and detalle:
                memo = detalle

            lines.append({
                "cuenta":  cuenta,
                "debe":    debe,
                "haber":   haber,
                "detalle": detalle,
                "glosa":   glosa,
                "unidad":  unidad,
                "cc":      cc,
            })

    return memo, lines


# ══════════════════════════════════════════════════════════════
# BUILDER DE PAYLOAD
# ══════════════════════════════════════════════════════════════

def build_journal_payload(memo: str, lines: list[dict], file_date: str) -> dict:
    """Construye el payload base (sin wrapper de voucher)."""
    je_lines = []
    for l in lines:
        es_bp = l.get("es_bp", False)
        if es_bp:
            line: dict = {
                "ShortName": l["cuenta"],
                "Debit":     l["debe"],
                "Credit":    l["haber"],
                "LineMemo":  l["detalle"],
            }
        else:
            line = {
                "AccountCode": l["cuenta"],
                "Debit":       l["debe"],
                "Credit":      l["haber"],
                "LineMemo":    l["detalle"],
            }
        if l.get("glosa"):
            line["Reference1"] = l["glosa"]
        if l.get("unidad"):
            line["CostingCode"]  = l["unidad"]
        if l.get("cc"):
            line["CostingCode2"] = l["cc"]
        je_lines.append(line)

    return {
        "ReferenceDate":     file_date,
        "DueDate":           file_date,
        "TaxDate":           file_date,
        "Memo":              memo or "Asiento Contable Bolivian Foods",
        "JournalEntryLines": je_lines,
    }


def build_journal_voucher_payload(memo: str, lines: list[dict], file_date: str) -> dict:
    """
    Envuelve el payload en la estructura de JournalVouchersService_Add
    (asiento preliminar):
        { JournalVoucher: { JournalEntry: { ... } } }
    """
    base = build_journal_payload(memo, lines, file_date)
    return {
        "JournalVoucher": {
            "JournalEntry": {
                "ReferenceDate":     base["ReferenceDate"],
                "DueDate":           base["DueDate"],
                "Memo":              base["Memo"],
                "JournalEntryLines": base["JournalEntryLines"],
            }
        }
    }


# ══════════════════════════════════════════════════════════════
# CLIENTE SAP SERVICE LAYER
# ══════════════════════════════════════════════════════════════

class SapJournalClient:
    def __init__(self, sap_cfg: dict, retry_cfg: dict):
        self.base_url    = sap_cfg["base_url"].rstrip("/")
        self.company_db  = sap_cfg["company_db"]
        self.username    = sap_cfg["username"]
        self.password    = sap_cfg["password"]
        self.verify_ssl  = sap_cfg.get("verify_ssl", False)
        self.max_retries = retry_cfg.get("max_attempts", 3)
        self.backoff     = retry_cfg.get("backoff_seconds", 5)
        self._session    = requests.Session()
        self._logged_in  = False

    def login(self):
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

    def logout(self):
        try:
            self._session.post(f"{self.base_url}/Logout",
                            verify=self.verify_ssl, timeout=10)
        except Exception:
            pass
        self._logged_in = False

    def post_journal_voucher(self, payload: dict) -> dict:
        """
        POST /JournalVouchersService_Add — asiento PRELIMINAR.
        Retorna: {'ok': bool, 'jdt_num': int | None, 'error': str}
        SAP devuelve HTTP 200 en éxito.
        """
        if not self._logged_in:
            self.login()

        url = f"{self.base_url}/JournalVouchersService_Add"
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self._session.post(url, json=payload,
                                    verify=self.verify_ssl, timeout=60)

                if r.status_code in (200, 204):
                    jdt_num = None
                    if r.status_code == 200 and r.content:
                        data    = r.json()
                        jdt_num = (data.get("JdtNum") or
                                data.get("Number") or
                                data.get("AbsEntry"))
                    log.info("✓ Asiento preliminar creado — Number: %s", jdt_num)
                    return {"ok": True, "jdt_num": jdt_num, "error": ""}

                if r.status_code == 401:
                    log.warning("Sesión expirada — re-login (intento %s)", attempt)
                    self._logged_in = False
                    self.login()
                    continue

                if r.status_code in (500, 502, 503) and attempt < self.max_retries:
                    wait = self.backoff * attempt
                    log.warning("HTTP %s — reintento %s/%s en %ss...",
                                r.status_code, attempt, self.max_retries, wait)
                    time.sleep(wait)
                    continue

                try:
                    err = r.json().get("error", {}).get("message", {})
                    msg = err.get("value", r.text) if isinstance(err, dict) else str(err)
                    code = r.json().get("error", {}).get("code", "")
                except Exception:
                    msg, code = r.text, ""
                log.error("SAP error %s [%s]: %s", r.status_code, code, msg)
                return {"ok": False, "jdt_num": None, "error": f"[{code}] {msg}"}

            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    time.sleep(self.backoff * attempt)
                else:
                    return {"ok": False, "jdt_num": None, "error": str(exc)}

        return {"ok": False, "jdt_num": None, "error": "Agotados los reintentos"}

    def post_journal_entry(self, payload: dict) -> dict:
        """
        Alias de compatibilidad.
        Si el payload ya tiene la estructura JournalVoucher lo envía como
        preliminar. Si no, lo envía como definitivo a /JournalEntries.
        """
        if "JournalVoucher" in payload:
            return self.post_journal_voucher(payload)

        # Fallback: asiento definitivo (estructura original)
        if not self._logged_in:
            self.login()
        url = f"{self.base_url}/JournalEntries"
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self._session.post(url, json=payload,
                                    verify=self.verify_ssl, timeout=60)
                if r.status_code == 201:
                    data = r.json()
                    return {"ok": True, "doc_entry": data.get("JdtNum"), "error": ""}
                if r.status_code == 401:
                    log.warning("Sesión expirada — re-login...")
                    self.login()
                    continue
                if r.status_code in (500, 502, 503) and attempt < self.max_retries:
                    time.sleep(self.backoff * attempt)
                    continue
                try:
                    err = r.json().get("error", {}).get("message", {})
                    msg = err.get("value", r.text) if isinstance(err, dict) else str(err)
                except Exception:
                    msg = r.text
                return {"ok": False, "doc_entry": None, "error": msg}
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    time.sleep(self.backoff * attempt)
                else:
                    return {"ok": False, "doc_entry": None, "error": str(exc)}
        return {"ok": False, "doc_entry": None, "error": "Agotados los reintentos"}


# ══════════════════════════════════════════════════════════════
# GESTION DE LOGS ROTATIVOS
# ══════════════════════════════════════════════════════════════

def get_log_dir(logs_base: str) -> str:
    os.makedirs(logs_base, exist_ok=True)
    existing = sorted([
        d for d in os.listdir(logs_base)
        if os.path.isdir(os.path.join(logs_base, d))
    ])
    while len(existing) >= MAX_LOG_DIRS:
        oldest = os.path.join(logs_base, existing.pop(0))
        shutil.rmtree(oldest, ignore_errors=True)
        log.info("Log rotado — eliminado: %s", oldest)
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    new_dir = os.path.join(logs_base, ts)
    os.makedirs(new_dir, exist_ok=True)
    return new_dir


def setup_file_logger(log_dir: str, filename: str) -> logging.FileHandler:
    log_path = os.path.join(log_dir, filename)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s"))
    logging.getLogger().addHandler(fh)
    return fh


def remove_file_logger(fh: logging.FileHandler):
    logging.getLogger().removeHandler(fh)
    fh.close()


# ══════════════════════════════════════════════════════════════
# PROCESADOR DE ARCHIVO
# ══════════════════════════════════════════════════════════════

def _is_business_partner(code: str, client: "SapJournalClient") -> bool:
    try:
        safe = code.replace("'", "''")
        resp = client._session.get(
            f"{client.base_url}/BusinessPartners",
            params={"$filter": f"CardCode eq '{safe}'", "$select": "CardCode", "$top": 1},
            verify=client.verify_ssl, timeout=10,
        )
        if resp.status_code == 200:
            return len(resp.json().get("value", [])) > 0
    except Exception:
        pass
    return False


_bp_cache: dict[str, bool] = {}


def resolve_accounts(lines: list[dict], client: "SapJournalClient") -> list[dict]:
    resolver = AccountResolver(
        session  = client._session,
        base_url = client.base_url,
        verify   = client.verify_ssl,
    )

    codigos = list({l["cuenta"] for l in lines})
    log.info("Resolviendo %s códigos únicos...", len(codigos))

    posibles_bp = []
    for c in codigos:
        sys_code = resolver.resolve(c)
        if sys_code == c:
            posibles_bp.append(c)

    for c in posibles_bp:
        if c not in _bp_cache:
            _bp_cache[c] = _is_business_partner(c, client)
        if _bp_cache[c]:
            log.debug("BP detectado: %s", c)

    resueltas = []
    for l in lines:
        codigo   = l["cuenta"]
        sys_code = resolver.resolve(codigo)
        if sys_code != codigo:
            resueltas.append({**l, "cuenta": sys_code, "es_bp": False})
        elif _bp_cache.get(codigo, False):
            resueltas.append({**l, "cuenta": codigo, "es_bp": True})
        else:
            log.warning("Código no resuelto: %s — se omite la línea", codigo)

    resueltos_cta = sum(1 for r in resueltas if not r["es_bp"])
    resueltos_bp  = sum(1 for r in resueltas if r["es_bp"])
    log.info("Resueltos: %s cuentas + %s BPs = %s/%s líneas",
            resueltos_cta, resueltos_bp, len(resueltas), len(lines))
    return resueltas


def process_file(txt_path: str, cfg: dict, logs_base: str):
    """Procesa un archivo TXT como un único JournalVoucher (asiento preliminar)."""
    filename   = os.path.basename(txt_path)
    base_name  = os.path.splitext(filename)[0]
    paths      = cfg["paths"]
    processed  = paths.get("journal_processed", paths.get("processed"))
    errors_dir = paths.get("journal_errors",    paths.get("errors"))

    log_dir = get_log_dir(logs_base)
    fh      = setup_file_logger(log_dir, f"{base_name}.log")

    log.info("═" * 60)
    log.info("Procesando: %s", filename)

    try:
        file_date = datetime.fromtimestamp(os.path.getmtime(txt_path)).strftime("%Y-%m-%d")
        log.info("Fecha del documento: %s", file_date)

        client = SapJournalClient(cfg["sap"], cfg.get("retry", {}))
        client.login()

        memo, lines = parse_txt(txt_path)
        log.info("Filas leídas: %s | Memo: %s", len(lines), memo)

        if not lines:
            log.error("Sin filas válidas — se mueve a Errores/")
            _move_file(txt_path, errors_dir, filename)
            return

        lines = resolve_accounts(lines, client)
        if not lines:
            log.error("Ninguna cuenta resuelta — se mueve a Errores/")
            _move_file(txt_path, errors_dir, filename)
            return

        # Asiento PRELIMINAR
        payload_voucher = build_journal_voucher_payload(memo, lines, file_date)
        log.info("Payload construido: %s líneas", len(lines))

        result = client.post_journal_voucher(payload_voucher)
        client.logout()

        if result["ok"]:
            log.info("✓ ÉXITO — Asiento preliminar — JdtNum: %s", result["jdt_num"])
            _move_file(txt_path, processed, filename)
        else:
            log.error("✗ ERROR SAP: %s", result["error"])
            _move_file(txt_path, errors_dir, filename)

    except Exception as exc:
        log.exception("Error inesperado: %s", exc)
        _move_file(txt_path, errors_dir, filename)
    finally:
        log.info("═" * 60)
        remove_file_logger(fh)


def _move_file(src: str, dest_dir: str, filename: str):
    os.makedirs(dest_dir, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    name, ext = os.path.splitext(filename)
    shutil.move(src, os.path.join(dest_dir, f"{name}_{ts}{ext}"))
    log.info("Archivo movido a: %s/%s_%s%s", dest_dir, name, ts, ext)


# ══════════════════════════════════════════════════════════════
# WATCHER PRINCIPAL
# ══════════════════════════════════════════════════════════════

def run_watcher(cfg: dict):
    inbound   = cfg["paths"]["inbound"]
    logs_base = cfg["paths"].get("logs", os.path.join(
        os.path.dirname(cfg["paths"]["processed"]), "Logs"
    ))
    interval  = cfg.get("scheduler", {}).get("interval_seconds", SCAN_INTERVAL)

    os.makedirs(inbound, exist_ok=True)
    os.makedirs(logs_base, exist_ok=True)

    log.info("Watcher BF Journal iniciado (modo: ASIENTO PRELIMINAR)")
    log.info("  Endpoint: JournalVouchersService_Add")
    log.info("  Empresa:  %s", cfg["sap"]["company_db"])

    while True:
        try:
            archivos = sorted([
                f for f in os.listdir(inbound)
                if f.lower().endswith(".txt")
                and not f.endswith(LOCK_EXT)
                and not os.path.exists(os.path.join(inbound, f + LOCK_EXT))
            ])
            if archivos:
                log.info("Encontrados %s archivo(s).", len(archivos))
            for filename in archivos:
                txt_path  = os.path.join(inbound, filename)
                lock_path = txt_path + LOCK_EXT
                try:
                    with open(lock_path, "w") as lf:
                        lf.write(datetime.now().isoformat())
                except Exception:
                    log.warning("No se pudo crear lock para %s.", filename)
                    continue
                try:
                    process_file(txt_path, cfg, logs_base)
                finally:
                    try:
                        os.remove(lock_path)
                    except Exception:
                        pass
        except Exception as exc:
            log.exception("Error en ciclo watcher: %s", exc)
        time.sleep(interval)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/config_bk.yaml")
    args = parser.parse_args()
    if not os.path.exists(args.config):
        print(f"ERROR: Config no encontrado: {args.config}")
        sys.exit(1)
    with open(args.config, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    run_watcher(cfg)


if __name__ == "__main__":
    main()