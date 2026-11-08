"""Deterministic, bounded hybrid retrieval under explicit server authority."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import text

from knowledge.embedding import EmbeddingUnavailable
from knowledge.profile import PROFILE

Purpose = Literal["preview", "runtime", "test"]
Reason = Literal[
    "ready",
    "no_sources",
    "processing_pending",
    "below_threshold",
    "service_unavailable",
    "rejected",
    "stale",
]


@dataclass(frozen=True)
class KnowledgeScope:
    tenant_id: UUID
    agent_id: UUID
    purpose: Purpose
    session_id: UUID | None = None
    test_id: UUID | None = None

    def __post_init__(self):
        if self.purpose not in {"preview", "runtime", "test"}:
            raise ValueError("invalid_knowledge_purpose")


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: UUID
    source_id: UUID
    generation_id: UUID
    profile: str
    source_revision: int  # Current resource identity; deliberately NOT generation revision.
    generation_source_revision: int
    name: str
    heading: str
    language: str
    text: str
    cosine: float
    score: float

    def reference(self):
        return {
            "chunk_id": str(self.chunk_id),
            "source_id": str(self.source_id),
            "generation_id": str(self.generation_id),
            "profile": self.profile,
            "source_revision": self.source_revision,
            "generation_source_revision": self.generation_source_revision,
            "cosine": self.cosine,
            "score": self.score,
        }


@dataclass(frozen=True)
class RetrievalResult:
    reason: Reason
    chunks: tuple[RetrievedChunk, ...] = ()


# MATERIALIZED prevents pgvector's approximate ORDER BY path before authorization.
# The scan is always exact and capped at 16,384 active chunks (+1 overflow sentinel).
# No RRF score is ever used as a semantic relevance threshold.
SEARCH_SQL = text("""
WITH corpus AS MATERIALIZED (
 SELECT c.id AS chunk_id,c.source_id,c.generation_id,g.profile,s.revision AS source_revision,
 g.source_revision AS generation_source_revision,s.name,left(c.heading,200) AS heading,left(c.language,16) AS language,
 left(c.text,4000) AS text,c.embedding,c.search
 FROM knowledge_chunks c
 JOIN knowledge_generations g ON (g.tenant_id,g.agent_id,g.source_id,g.id)=
   (c.tenant_id,c.agent_id,c.source_id,c.generation_id)
 JOIN knowledge_sources s ON (s.tenant_id,s.agent_id,s.id,s.active_generation_id)=
   (c.tenant_id,c.agent_id,c.source_id,c.generation_id)
 WHERE c.tenant_id=:tenant AND c.agent_id=:agent AND s.deleted_at IS NULL
 AND s.status <> 'deleted' AND g.sealed_at IS NOT NULL AND g.profile=:profile
 LIMIT 16385
), scored AS MATERIALIZED (
 SELECT *,1-(embedding <=> CAST(:vector AS vector)) AS cosine,
 ts_rank_cd(search,plainto_tsquery('simple',:query)) AS keyword FROM corpus
 WHERE (SELECT count(*) FROM corpus)<=16384
), dense AS (
 SELECT chunk_id,row_number() OVER(ORDER BY cosine DESC,chunk_id) AS rank
 FROM scored ORDER BY cosine DESC,chunk_id LIMIT 20
), keyword AS (
 SELECT chunk_id,row_number() OVER(ORDER BY keyword DESC,chunk_id) AS rank
 FROM scored WHERE keyword>0 ORDER BY keyword DESC,chunk_id LIMIT 20
), fused AS (
 SELECT chunk_id,sum(1.0/(60+rank)) AS score
 FROM (SELECT * FROM dense UNION ALL SELECT * FROM keyword) candidates GROUP BY chunk_id
)
 , selected AS (
SELECT s.chunk_id,s.source_id,s.generation_id,s.profile,s.source_revision,
 s.generation_source_revision,s.name,s.heading,s.language,s.text,s.cosine,f.score
FROM scored s JOIN fused f USING(chunk_id)
WHERE s.cosine>=0.55
ORDER BY f.score DESC,s.cosine DESC,s.chunk_id LIMIT 4
)
SELECT * FROM selected
UNION ALL
SELECT NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL
WHERE (SELECT count(*) FROM corpus)>16384
ORDER BY score DESC NULLS LAST,cosine DESC,chunk_id
""")


def retrieve_chunks(db, scope, vector, query):
    import json

    rows = list(
        db.execute(
            SEARCH_SQL,
            {
                "tenant": scope.tenant_id,
                "agent": scope.agent_id,
                "profile": PROFILE.fingerprint,
                "vector": json.dumps(vector),
                "query": query,
            },
        ).mappings()
    )
    if any(row["chunk_id"] is None for row in rows):
        raise EmbeddingUnavailable("corpus_limit")
    return tuple(RetrievedChunk(**(dict(row) | {"score": float(row["score"])})) for row in rows)
