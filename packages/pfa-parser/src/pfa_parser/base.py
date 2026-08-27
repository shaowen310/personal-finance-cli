"""Base classes for bank statement parsers."""

from dataclasses import dataclass, field
from abc import ABC, abstractmethod

from pfa_ir_schema import ParsedStatement


@dataclass
class Transaction:
    """A single parsed transaction — the unified model for downstream consumers."""

    date: str
    description: str
    amount: float  # signed: negative = debit (outflow), positive = credit (inflow)
    currency: str = "SGD"
    account_name: str = ""
    account_no: str = ""
    account_type: str = ""
    balance_after: float | None = None
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    link_labels: list[str] = field(default_factory=list)
    raw_line: str = ""


class BankStatementParser(ABC):
    """Abstract base class for bank statement parsers."""

    @abstractmethod
    def parse(self, file_path: str) -> ParsedStatement:
        """Parse a bank statement file and return the full ParsedStatement IR."""
        ...

    @abstractmethod
    def supports_format(self, file_path: str) -> bool:
        """Check if this parser supports the given file format."""
        ...
