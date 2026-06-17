"""
Pure-Python vector similarity for embedding-based ranking.

Embeddings are stored as JSONB and no PostgreSQL vector extension is configured
in this deployment. Similarity between two vectors is therefore computed here
in plain Python rather than using a database-side vector index or numpy.

Used by the recommendation endpoints (`routers/events.py`,
`routers/workshops.py`) to rank events/workshops against a participant's
query or saved preference embedding.
"""
import math


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    Cosine similarity between two equal-length vectors, in [-1.0, 1.0].

    Returns 0.0 for a zero vector on either side (a participant who has never
    searched or set a preference has an all-zero `embedding.event`/
    `embedding.workshop`, and an all-zero vector has no defined direction to
    compare) rather than raising a division-by-zero error.
    """
    if not a or not b or len(a) != len(b):
        return 0.0

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
