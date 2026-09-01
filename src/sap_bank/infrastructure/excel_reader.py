"""
excel_reader.py  (infrastructure)
---------------------------------
Lee el Excel de pagos recibidos del banco y lo convierte en una lista de
dicts listos para parse_row(). Conoce la ESTRUCTURA FISICA del archivo
(que columna es que), aislando ese detalle del resto del sistema.

Estructura del Excel (LTH v2):
A (0): MODULO SAP        -> informativo (determina tipo si no hay col Tipo)
B (1): Fecha             -> fecha del documento (YYYY-MM-DD)
C (2): Cuenta mayor/Cod  -> cuenta banco (TransferAccount)
D (3): Cuenta asociada   -> cuenta_destino (PaymentAccounts.AccountCode)
E (4): Crédito           -> monto
F (5): Comentarios       -> descripcion (Remarks en SAP)
G (6): Unidad de Negocio -> ProfitCenter   (dim 1)
H (7): Centro de Costo   -> ProfitCenter2  (dim 2)
I (8): Segmento          -> ProfitCenter3  (dim 3)
J (9): Partida Flujo     -> informativo
K (10): Glosa            -> informativo
L (11): Referencia       -> referencia (idempotencia, si se activa)
M (12): Tipo             -> tipo_pago (Transferencia / Efectivo / Tarjeta)
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import openpyxl

logger = logging.getLogger(__name__)

# Mapeo del texto de la columna "Tipo" / "MODULO SAP" al enum de PaymentType
TIPO_MAP = {
    "transferencia":            "TRANSFERENCIA",
    "transferencia bancaria":   "TRANSFERENCIA",
    "efectivo":                 "EFECTIVO",
    "tarjeta":                  "TARJETA",
    "otros":                    "OTROS",
}


def _s(val: Any) -> str:
    """Convierte cualquier valor de celda a string limpio."""
    if val is None:
        return ""
    return str(val).strip()


def _cc(val: Any) -> str:
    """
    Convierte un código de dimensión (CostingCode / Centro de Costo) a
    string limpio, preservando siempre dos decimales cuando corresponde.

    Ejemplos:
        1.1   -> "1.10"
        1.09  -> "1.09"
        1.0   -> "1"
        3     -> "3"
        "1.10"-> "1.10"
    """
    if val is None:
        return ""
    if isinstance(val, float):
        # Si tiene parte decimal significativa, formatear con 2 decimales
        if val != int(val):
            return f"{val:.2f}"
        else:
            return str(int(val))
    if isinstance(val, int):
        return str(val)
    # Texto: devolver tal cual (ya viene formateado desde Excel como texto)
    return str(val).strip()


def _parse_fecha(val: Any) -> str:
    """
    Convierte la fecha de la columna B a 'YYYY-MM-DD'.
    Acepta datetime (lo normal en openpyxl) o texto.
    """
    if val is None:
        return ""
    if isinstance(val, (datetime, date)):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s  # se devuelve tal cual; parse_row decidira si es valida


def _parse_monto(val: Any) -> str:
    """
    Normaliza el monto (columna Crédito) a string que parse_row entiende.
    Acepta numeros nativos de Excel o texto con prefijo 'BS'.
    """
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val).strip()
    for prefix in ("BS ", "Bs. ", "Bs ", "$ ", "$"):
        if s.upper().startswith(prefix.upper()):
            s = s[len(prefix):]
            break
    return s.strip()


def read_payments(xlsx_path: str) -> list[dict]:
    """
    Lee el Excel y devuelve una lista de dicts con las claves que espera
    parse_row(). Ignora la fila de encabezado, filas vacias y filas sin
    monto valido (como notas o comentarios sueltos).
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    rows_out: list[dict] = []

    for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if i == 1:
            continue                      # encabezado
        if all(c is None for c in row):
            continue                      # fila vacia

        modulo       = _s(row[0])  if len(row) > 0  else ""
        fecha        = _parse_fecha(row[1]) if len(row) > 1 else ""
        cuenta_mayor = _s(row[2])  if len(row) > 2  else ""
        cuenta_asoc  = _s(row[3])  if len(row) > 3  else ""
        monto        = _parse_monto(row[4]) if len(row) > 4 else ""
        comentario   = _s(row[5])  if len(row) > 5  else ""
        unidad_neg   = _cc(row[6]) if len(row) > 6  else ""   # dim 1 — usa _cc
        centro_costo = _cc(row[7]) if len(row) > 7  else ""   # dim 2 — usa _cc
        segmento     = _cc(row[8]) if len(row) > 8  else ""   # dim 3 — usa _cc
        partida      = _s(row[9])  if len(row) > 9  else ""
        glosa        = _s(row[10]) if len(row) > 10 else ""
        referencia   = _s(row[11]) if len(row) > 11 else ""
        tipo_txt     = _s(row[12]) if len(row) > 12 else ""

        # Filtrar filas basura: sin monto o sin cuenta destino no son pagos
        if not monto or not cuenta_asoc:
            logger.debug("Fila %s ignorada (sin monto o sin cuenta destino).", i)
            continue

        # Determinar tipo de pago: prioridad a la columna Tipo (M); si no,
        # se infiere del MODULO SAP (col A).
        tipo_raw = TIPO_MAP.get(tipo_txt.lower())
        if not tipo_raw:
            if "banco" in modulo.lower():
                tipo_raw = "TRANSFERENCIA"
            elif "cuenta" in modulo.lower():
                tipo_raw = "TRANSFERENCIA"
            else:
                tipo_raw = "TRANSFERENCIA"

        d: dict = {
            "fecha":          fecha,
            "descripcion":    comentario,
            "tipo_pago":      tipo_raw,
            "monto":          monto,
            "cuenta_destino": cuenta_asoc,
            "moneda":         "BS",
            "cuenta_banco":   cuenta_mayor,
            "cuenta_caja":    cuenta_mayor,
            "codigo_tarjeta": "",
            "num_cupon":      "",
            "referencia":     referencia,
            "centro_costo":   unidad_neg,    # dim 1 -> ProfitCenter
            "centro_costo2":  centro_costo,  # dim 2 -> ProfitCenter2
            "centro_costo3":  segmento,      # dim 3 -> ProfitCenter3
            "unidad_negocio": unidad_neg,
            "glosa":          glosa,
            "partida_flujo":  partida,
        }
        rows_out.append(d)
        logger.debug(
            "Fila %s -> fecha=%s tipo=%s monto=%s banco=%s destino=%s pc=%s/%s/%s",
            i, fecha, tipo_raw, monto, cuenta_mayor, cuenta_asoc,
            unidad_neg, centro_costo, segmento,
        )

    logger.info("Excel '%s' leido: %s filas de datos.", xlsx_path, len(rows_out))
    return rows_out