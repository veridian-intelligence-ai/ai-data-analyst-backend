"""
Data-source adapter contract.

The orchestrator depends only on this seam: `execute_query` returning a
QueryResult. Power BI is the first (and, in this project, the only) adapter;
the seam exists so the architecture can later grow other sources without
touching the AI layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryResult:
    """Tabular result of a query against a semantic model."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SemanticModel:
    """A semantic model (dataset) visible in the workspace."""

    id: str
    name: str
    workspace_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseDataSourceAdapter(ABC):
    """Contract every data-source adapter implements."""

    @abstractmethod
    def health_check(self) -> bool:
        """Return True when the data source is reachable and authorized."""

    @abstractmethod
    def list_models(self) -> list[SemanticModel]:
        """Enumerate the semantic models available in the configured workspace."""

    @abstractmethod
    def execute_query(self, model_id: str, query: str) -> QueryResult:
        """Execute a query (DAX for Power BI) against one semantic model."""
