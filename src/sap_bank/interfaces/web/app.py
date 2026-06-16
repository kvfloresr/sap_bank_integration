"""
app.py  (interfaces/web)
------------------------
Servidor web Flask para carga manual de pagos recibidos a SAP.
Soporta multiples empresas — el usuario elige cual en la interfaz.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import uuid

import yaml
from flask import Flask, jsonify, render_template, request, send_file, session
from werkzeug.utils import secure_filename

from src.sap_bank.application.processor import parse_row, process_rows
from src.sap_bank.domain.payment_builder import build_payload
from src.sap_bank.infrastructure.excel_reader import read_payments
from src.sap_bank.infrastructure.report_writer import write_report
from src.sap_bank.infrastructure.sap_client import DryRunClient, SapClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("web")

# ── Empresas disponibles ───────────────────────────────────────────────────
# Cada entrada mapea un ID legible a su config.yaml.
# Para agregar una empresa nueva: agregar una entrada aqui y crear el config.
EMPRESAS = {
    "lth": {
        "nombre":  "La Terraza Hotel (TAJIBOS_QA)",
        "config":  "config/config.yaml",
    },
    "bk": {
        "nombre":  "Burger King (BOLIVIAN_FOODS_PROD)",
        "config":  "config/config_bk.yaml",
    },
}

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "sap_bank_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SAP_WEB_SECRET", "cambiar-esta-clave-en-produccion")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB


def load_config(empresa_id: str) -> dict:
    empresa = EMPRESAS.get(empresa_id)
    if not empresa:
        raise ValueError(f"Empresa desconocida: {empresa_id}")
    with open(empresa["config"], encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ── Rutas ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    empresas_list = [
        {"id": k, "nombre": v["nombre"]} for k, v in EMPRESAS.items()
    ]
    return render_template("index.html", empresas=empresas_list)


@app.route("/api/preview", methods=["POST"])
def preview():
    if "file" not in request.files:
        return jsonify({"error": "No se recibio ningun archivo."}), 400

    file      = request.files["file"]
    empresa_id = request.form.get("empresa", "lth")

    if not file.filename:
        return jsonify({"error": "El archivo no tiene nombre."}), 400
    if not file.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "El archivo debe ser .xlsx"}), 400
    if empresa_id not in EMPRESAS:
        return jsonify({"error": "Empresa no válida."}), 400

    file_id    = uuid.uuid4().hex
    safe_name  = secure_filename(file.filename)
    saved_path = os.path.join(UPLOAD_DIR, f"{file_id}__{safe_name}")
    file.save(saved_path)

    try:
        raw_rows = read_payments(saved_path)
    except Exception as exc:
        log.exception("Error leyendo el Excel.")
        os.remove(saved_path)
        return jsonify({"error": f"No se pudo leer el Excel: {exc}"}), 400

    if not raw_rows:
        os.remove(saved_path)
        return jsonify({"error": "El archivo no tiene filas de datos validas."}), 400

    try:
        cfg = load_config(empresa_id)
    except Exception as exc:
        os.remove(saved_path)
        return jsonify({"error": str(exc)}), 400

    accounts_cfg = cfg["accounts"]
    preview_rows = []
    for idx, raw in enumerate(raw_rows, start=2):
        try:
            row     = parse_row(raw, idx)
            payload = build_payload(row, accounts_cfg)
            preview_rows.append({
                "linea":          idx,
                "fecha":          row.fecha,
                "tipo":           row.tipo_pago.value,
                "monto":          row.monto,
                "moneda":         row.moneda,
                "descripcion":    row.descripcion,
                "cuenta_banco":   row.cuenta_banco or "",
                "cuenta_destino": row.cuenta_destino,
                "centro_costo":   row.centro_costo or "",
                "valido":         True,
                "error":          "",
            })
        except ValueError as exc:
            preview_rows.append({
                "linea":          idx,
                "fecha":          raw.get("fecha", ""),
                "tipo":           raw.get("tipo_pago", ""),
                "monto":          raw.get("monto", ""),
                "moneda":         "",
                "descripcion":    raw.get("descripcion", ""),
                "cuenta_banco":   raw.get("cuenta_banco", ""),
                "cuenta_destino": raw.get("cuenta_destino", ""),
                "centro_costo":   raw.get("centro_costo", ""),
                "valido":         False,
                "error":          str(exc),
            })

    session["file_id"]    = file_id
    session["file_path"]  = saved_path
    session["file_name"]  = file.filename
    session["empresa_id"] = empresa_id

    validas     = sum(1 for r in preview_rows if r["valido"])
    invalidas   = len(preview_rows) - validas
    total_monto = sum(r["monto"] for r in preview_rows if r["valido"] and isinstance(r["monto"], (int, float)))

    return jsonify({
        "file_name":    file.filename,
        "empresa":      EMPRESAS[empresa_id]["nombre"],
        "rows":         preview_rows,
        "total":        len(preview_rows),
        "validas":      validas,
        "invalidas":    invalidas,
        "total_monto":  total_monto,
    })


@app.route("/api/process", methods=["POST"])
def process():
    saved_path = session.get("file_path")
    file_name  = session.get("file_name", "archivo.xlsx")
    empresa_id = session.get("empresa_id", "lth")

    if not saved_path or not os.path.exists(saved_path):
        return jsonify({"error": "No hay archivo en sesion. Subi el archivo de nuevo."}), 400

    body     = request.get_json(silent=True) or {}
    sap_real = bool(body.get("sap_real", False))

    try:
        cfg = load_config(empresa_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    accounts_cfg = cfg["accounts"]
    idem_cfg     = cfg.get("idempotency", {})
    max_workers  = int(cfg.get("concurrency", {}).get("max_workers", 6))
    reports_dir  = cfg["paths"]["reports"]

    if sap_real:
        client = SapClient(cfg["sap"], cfg.get("retry", {}))
        try:
            client.ensure_session()
        except Exception as exc:
            log.exception("No se pudo conectar a SAP.")
            return jsonify({"error": f"No se pudo conectar a SAP: {exc}"}), 502
    else:
        client = DryRunClient()

    try:
        raw_rows = read_payments(saved_path)
        summary  = process_rows(
            raw_rows     = raw_rows,
            source_name  = file_name,
            client       = client,
            accounts_cfg = accounts_cfg,
            max_workers  = max_workers,
            idem_cfg     = idem_cfg,
        )
        os.makedirs(reports_dir, exist_ok=True)
        report_path = write_report(summary, reports_dir)
    except Exception as exc:
        log.exception("Error durante el procesamiento.")
        return jsonify({"error": f"Error procesando: {exc}"}), 500
    finally:
        if sap_real:
            try:
                client.logout()
            except Exception:
                pass

    session["report_path"] = report_path

    resultados = [{
        "linea":     r.line_num,
        "estado":    r.status.value,
        "doc_entry": r.doc_entry,
        "doc_num":   r.doc_num,
        "cuenta":    r.cuenta_destino,
        "error":     r.error,
    } for r in summary.results]

    return jsonify({
        "modo":          "SAP real" if sap_real else "Simulación (dry-run)",
        "empresa":       EMPRESAS[empresa_id]["nombre"],
        "resultado":     "OK" if summary.all_success else "CON INCIDENCIAS",
        "total":         summary.total,
        "exitos":        summary.exitos,
        "errores":       summary.errores,
        "observados":    summary.observados,
        "omitidas":      summary.omitidas,
        "resultados":    resultados,
        "tiene_reporte": True,
    })


@app.route("/api/report")
def download_report():
    report_path = session.get("report_path")
    if not report_path or not os.path.exists(report_path):
        return jsonify({"error": "No hay reporte disponible."}), 404
    return send_file(
        report_path,
        as_attachment=True,
        download_name=os.path.basename(report_path),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)