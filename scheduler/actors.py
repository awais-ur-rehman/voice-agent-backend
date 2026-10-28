"""Least-privilege identity required by shared domain transactions."""

from typing import Protocol
from uuid import UUID


class AuditActor(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def tenant_id(self) -> UUID: ...
