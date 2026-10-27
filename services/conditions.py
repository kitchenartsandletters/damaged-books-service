# services/conditions.py
"""
Single source of truth for the damaged-condition vocabulary.

Canonical keys are what damaged.inventory.condition accepts
(CHECK condition IN ('light','moderate','heavy')) and what the bulk wizard
API, synthetic barcodes ({handle}_{key}) and CONDITION_META use.

Shopify variant option values are the human titles this app creates
("Light Damage", "Moderate Damage", "Heavy Damage"). The mapping is
deliberately strict: an unrecognised value raises instead of being
coerced, so a new Shopify label surfaces as a loud failure rather than
being silently stored or silently guessed.
"""
from __future__ import annotations

CONDITION_KEYS: tuple[str, ...] = ("light", "moderate", "heavy")


class UnknownConditionError(ValueError):
    pass


def _canon(s: str) -> str:
    return " ".join(s.replace("_", " ").replace("-", " ").lower().split())


# Accepted spellings -> canonical key. Every entry is explicit on purpose.
_ALIASES: dict[str, str] = {}
for _k in CONDITION_KEYS:
    _ALIASES[_k] = _k                    # "light"
    _ALIASES[f"{_k} damage"] = _k        # "Light Damage", "light_damage"
    _ALIASES[f"{_k} damaged"] = _k       # "Light Damaged"


def normalize_condition_key(raw: str | None) -> str | None:
    """
    Map a Shopify option value or legacy key to a canonical condition key.

    Returns None for None/blank input (caller decides what "absent" means).
    Raises UnknownConditionError for any non-blank value outside the vocabulary.
    """
    if raw is None:
        return None
    c = _canon(str(raw))
    if not c:
        return None
    try:
        return _ALIASES[c]
    except KeyError:
        raise UnknownConditionError(
            f"unrecognised condition value {raw!r}; expected one of "
            f"{', '.join(CONDITION_KEYS)} (optionally with ' Damage')"
        ) from None
