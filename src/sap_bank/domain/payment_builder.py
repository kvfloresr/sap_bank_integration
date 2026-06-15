"""
payment_builder.py  (domain)
----------------------------
Logica PURA de construccion del payload de IncomingPayments.
No depende de SAP, ni de archivos, ni de red. Solo transforma un CsvRow
en el diccionario JSON que espera Service Layer.

Esto es el corazon del dominio: dada una fila, produce el documento SAP.
Se puede testear sin conexion a nada.
"""

from __future__ import annotations

from src.sap_bank.domain.models import CsvRow, PaymentType

DEFAULT_CURRENCY = "BS"


def build_payload(row: CsvRow, accounts_cfg: dict) -> dict:
    """
    Construye el JSON de IncomingPayments (DocType rAccount).

    - PaymentAccounts lleva siempre la contrapartida contable y, si la fila
      trae centros de costo, las dimensiones ProfitCenter / 2 / 3.
    - DocCurrency solo se envia si la moneda NO es la local (BS); para la
      moneda local SAP usa la moneda por defecto de la empresa.
    - Segun tipo_pago se completa CashAccount, PaymentCreditCards o
      TransferAccount.
    """
    payment_line: dict = {
        "AccountCode": row.cuenta_destino,
        "SumPaid":     row.monto,
    }
    if row.centro_costo:
        payment_line["ProfitCenter"] = row.centro_costo
    if row.centro_costo2:
        payment_line["ProfitCenter2"] = row.centro_costo2
    if row.centro_costo3:
        payment_line["ProfitCenter3"] = row.centro_costo3

    payload: dict = {
        "DocDate":         row.fecha,
        "DocType":         "rAccount",
        "Remarks":         row.descripcion,
        "PaymentAccounts": [payment_line],
    }

    # Glosa contable (JournalRemarks): siempre igual a Comentarios (Remarks).
    # Por requerimiento, la glosa del asiento debe coincidir con los comentarios.
    payload["JournalRemarks"] = row.descripcion

    if row.moneda and row.moneda != DEFAULT_CURRENCY:
        payload["DocCurrency"] = row.moneda

    match row.tipo_pago:
        case PaymentType.EFECTIVO:
            payload["CashAccount"] = row.cuenta_caja or accounts_cfg["cash_account"]
            payload["CashSum"]     = row.monto

        case PaymentType.TARJETA:
            try:
                credit_card = int(row.codigo_tarjeta) if row.codigo_tarjeta \
                    else int(accounts_cfg.get("default_card_code", 1))
            except (TypeError, ValueError):
                credit_card = int(accounts_cfg.get("default_card_code", 1))

            cc_line: dict = {
                "CreditCard": credit_card,
                "CreditSum":  row.monto,
            }
            if row.num_cupon:
                cc_line["VoucherNum"] = row.num_cupon
            credit_acct = accounts_cfg.get("credit_card_account")
            if credit_acct:
                cc_line["CreditAcct"] = credit_acct
            payload["PaymentCreditCards"] = [cc_line]

        case PaymentType.TRANSFERENCIA | PaymentType.OTROS:
            payload["TransferAccount"] = row.cuenta_banco or accounts_cfg["transfer_account"]
            payload["TransferSum"]     = row.monto

    return payload