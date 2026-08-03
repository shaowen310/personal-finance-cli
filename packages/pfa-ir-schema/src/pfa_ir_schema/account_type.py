"""Account-type vocabulary for the bank-statement IR.

``account_type`` appears on ``StatementMeta`` and ``Account``.  To keep
the vocabulary consistent across extractors and the schema, every value must be
one of the ``AccountType`` members below.  ``AccountType.normalize`` maps any
string (or ``None`` / empty) to a canonical member, falling back to
``AccountType.UNKNOWN`` so downstream consumers never receive an empty or ad-hoc
value.
"""

from enum import Enum


class AccountType(str, Enum):
    """Canonical account types.

    Inherits from ``str`` so members compare equal to their string value and
    serialise to the plain string (e.g. ``"current"``) in JSON, keeping the
    field backwards-compatible with code that treats ``account_type`` as a str.
    """

    CURRENT = "current"
    CREDIT_CARD = "credit_card"
    EWALLET = "ewallet"
    FIXED_DEPOSIT = "fixed_deposit"
    SRS = "srs"
    UNIT_TRUST = "unit_trust"
    UNKNOWN = "unknown"

    @classmethod
    def normalize(cls, value: str | None) -> "AccountType":
        """Return the member matching ``value``.

        Falls back to ``AccountType.UNKNOWN`` when ``value`` is ``None``, empty,
        or not part of the vocabulary (case-insensitive, surrounding whitespace
        is ignored).
        """
        if not value:
            return cls.UNKNOWN
        candidate = value.strip().lower()
        if not candidate:
            return cls.UNKNOWN
        try:
            return cls(candidate)
        except ValueError:
            return cls.UNKNOWN

    @classmethod
    def values(cls) -> frozenset[str]:
        """The set of all valid string values."""
        return frozenset(m.value for m in cls)


# Backwards-compatible alias for callers that only need the raw value set.
VALID_ACCOUNT_TYPES: frozenset[str] = AccountType.values()
