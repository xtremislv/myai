"""ACL domain types."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Action(str, Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    LIST = "list"
    ADMIN = "admin"
    WILDCARD = "*"


class Effect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class Subject:
    """An authenticated actor: a user, agent, or service."""

    id: str
    roles: tuple[str, ...] = field(default_factory=tuple)

    @property
    def principals(self) -> tuple[str, ...]:
        # A subject matches policies bound to its id, any of its roles, or "*".
        return (self.id, *self.roles, "*")


@dataclass(frozen=True)
class Resource:
    """Typed resource, e.g. type='object', id='reports/q3.pdf' -> 'object:reports/q3.pdf'."""

    type: str
    id: str

    def urn(self) -> str:
        return f"{self.type}:{self.id}"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
