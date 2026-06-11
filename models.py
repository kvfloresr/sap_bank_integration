"""
models.py
---------
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
        key = (value or "").strip().upper()
        return cls(key)


class RowStatus(str, Enum):
    EXITO     = "EXITO"
    ERROR     = "ERROR"
    OBSERVADO = "OBSERVADO"
    OMITIDA   = "OMITIDA"


@dataclass
class CsvRow:
    """Representa una fila valida del Excel, ya parseada y tipada."""
    line_num:       int
    fecha:          str
    descripcion:    str
    tipo_pago:      PaymentType
    monto:          float
    cuenta_destino: str
    moneda:         str           = "BS"
    cuenta_caja:    Optional[str] = None
    cuenta_banco:   Optional[str] = None
    codigo_tarjeta: Optional[str] = None
    num_cupon:      Optional[str] = None
    referencia:     Optional[str] = None
    # Centros de costo (ProfitCenter) — hasta 3 niveles segun el Excel.
    # Mapean a ProfitCenter / ProfitCenter2 / ProfitCenter3 en PaymentAccounts.
    centro_costo:   Optional[str] = None   # dim 1 (UNIDAD DE NEGOCIO) -> ProfitCenter
    centro_costo2:  Optional[str] = None   # dim 2 (CENTRO DE COSTO)   -> ProfitCenter2
    centro_costo3:  Optional[str] = None   # dim 3 (SEGMENTO)          -> ProfitCenter3
    unidad_negocio: Optional[str] = None   # referencia adicional


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