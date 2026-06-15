"""
models.py  (domain)
-------------------
Modelos de dominio: enums y dataclasses que representan una fila del Excel
y el resultado de procesarla. Sin logica de negocio ni I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PaymentType(str, Enum):
    EFECTIVO      = "EFECTIVO"
    TARJETA       = "TARJETA"
    TRANSFERENCIA = "TRANSFERENCIA"
    OTROS         = "OTROS"

    @classmethod
    def from_str(cls, value: str) -> "PaymentType":
        """
        Normaliza el texto del Excel a un PaymentType.
        Acepta variantes: 'Transferencia' -> TRANSFERENCIA, 'Efectivo' -> EFECTIVO, etc.
        """
        key = (value or "").strip().upper()
        # Mapeo de sinonimos comunes del Excel
        alias = {
            "TRANSFERENCIA":        "TRANSFERENCIA",
            "TRANSFER":             "TRANSFERENCIA",
            "TRANSFERENCIA BANCARIA": "TRANSFERENCIA",
            "EFECTIVO":             "EFECTIVO",
            "TARJETA":              "TARJETA",
            "OTROS":                "OTROS",
        }
        normalized = alias.get(key, key)
        return cls(normalized)


class RowStatus(str, Enum):
    EXITO     = "EXITO"
    ERROR     = "ERROR"
    OBSERVADO = "OBSERVADO"
    OMITIDA   = "OMITIDA"


@dataclass
class CsvRow:
    """Representa una fila valida del Excel, ya parseada y tipada."""
    line_num:       int
    fecha:          str            # YYYY-MM-DD (viene de la columna Fecha del Excel)
    descripcion:    str            # Comentarios -> Remarks en SAP
    tipo_pago:      PaymentType
    monto:          float
    cuenta_destino: str            # Cuenta asociada -> PaymentAccounts.AccountCode
    moneda:         str           = "BS"
    cuenta_caja:    Optional[str] = None
    cuenta_banco:   Optional[str] = None   # Cuenta de mayor -> TransferAccount
    codigo_tarjeta: Optional[str] = None
    num_cupon:      Optional[str] = None
    referencia:     Optional[str] = None   # columna Referencia (no se usa si idem off)
    centro_costo:   Optional[str] = None   # dim 1 (UNIDAD NEGOCIO) -> ProfitCenter
    centro_costo2:  Optional[str] = None   # dim 2 (CENTRO COSTO)   -> ProfitCenter2
    centro_costo3:  Optional[str] = None   # dim 3 (SEGMENTO)       -> ProfitCenter3
    unidad_negocio: Optional[str] = None   # referencia adicional
    glosa:          Optional[str] = None   # columna Glosa (informativa)
    partida_flujo:  Optional[str] = None   # columna Partida Flujo (informativa)


@dataclass
class ProcessResult:
    line_num:       int
    status:         RowStatus
    cuenta_destino: str           = ""
    doc_entry:      Optional[int] = None
    doc_num:        Optional[int] = None
    error:          str           = ""

    @property
    def is_success(self) -> bool:
        return self.status == RowStatus.EXITO


@dataclass
class FileSummary:
    source_name: str
    results:     list[ProcessResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def exitos(self) -> int:
        return sum(1 for r in self.results if r.status == RowStatus.EXITO)

    @property
    def errores(self) -> int:
        return sum(1 for r in self.results if r.status == RowStatus.ERROR)

    @property
    def observados(self) -> int:
        return sum(1 for r in self.results if r.status == RowStatus.OBSERVADO)

    @property
    def omitidas(self) -> int:
        return sum(1 for r in self.results if r.status == RowStatus.OMITIDA)

    @property
    def all_success(self) -> bool:
        return self.total > 0 and self.errores == 0 and self.observados == 0