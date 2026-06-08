"""
models.py
---------
Modelos de dominio: enums y dataclasses que representan una fila del CSV
y el resultado de procesarla. Sin logica de negocio ni I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PaymentType(str, Enum):
    """Medio de pago indicado en la columna tipo_pago del CSV."""
    EFECTIVO = "EFECTIVO"
    TARJETA = "TARJETA"
    TRANSFERENCIA = "TRANSFERENCIA"
    OTROS = "OTROS"

    @classmethod
    def from_str(cls, value: str) -> "PaymentType":
        """Normaliza el texto del CSV a un PaymentType. Lanza ValueError si no aplica."""
        key = (value or "").strip().upper()
        return cls(key)


class RowStatus(str, Enum):
    """Estado final de una fila tras intentar procesarla."""
    EXITO = "EXITO"          # SAP devolvio 201, hay DocEntry
    ERROR = "ERROR"          # SAP rechazo (400) o se agotaron reintentos (5xx)
    OBSERVADO = "OBSERVADO"  # la fila no se pudo parsear; no se intento el POST
    OMITIDA = "OMITIDA"      # ya existia en SAP (idempotencia); no se reposteo


@dataclass
class CsvRow:
    """Representa una fila valida del CSV, ya parseada y tipada."""
    line_num: int                  # numero de linea en el archivo (1 = encabezado)
    fecha: str                     # YYYY-MM-DD
    descripcion: str
    tipo_pago: PaymentType
    monto: float
    cuenta_destino: str
    moneda: str = "BOB"            # ISO 4217. Default BOB = moneda local, sin conversion
    cuenta_caja: Optional[str] = None
    cuenta_banco: Optional[str] = None
    codigo_tarjeta: Optional[str] = None
    num_cupon: Optional[str] = None
    referencia: Optional[str] = None


@dataclass
class ProcessResult:
    """Resultado del procesamiento de una fila. Es lo que se vuelca al reporte Excel."""
    line_num: int
    status: RowStatus
    cuenta_destino: str = ""
    doc_entry: Optional[int] = None
    doc_num: Optional[int] = None
    error: str = ""

    @property
    def is_success(self) -> bool:
        return self.status == RowStatus.EXITO


@dataclass
class FileSummary:
    """Resumen del procesamiento de un archivo completo."""
    source_name: str
    results: list[ProcessResult] = field(default_factory=list)

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
        """
        True si NO hubo ERROR ni OBSERVADO. Las OMITIDA (ya existian en SAP)
        cuentan como resultado limpio.
        """
        return self.total > 0 and self.errores == 0 and self.observados == 0