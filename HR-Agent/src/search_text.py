"""Shared normalization for retrieval and network-free test embeddings."""
import hashlib
import math
import re
import unicodedata


def query_terms(text: str) -> list[str]:
    # Normalize compatibility characters before case folding so equivalent forms
    # produce the same terms. Match Unicode word characters except underscores.
    normalized_text = unicodedata.normalize("NFKC", text)
    casefolded_text = normalized_text.casefold()
    term_pattern = r"[^\W_]+"
    return re.findall(term_pattern, casefolded_text)


def deterministic_embedding(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    # A stable hash assigns each term to a coordinate without a model or network.
    # Repeated terms and hash collisions both add to the same coordinate's count.
    for term in query_terms(text):
        encoded_term = term.encode("utf-8")
        digest = hashlib.sha256(encoded_term).digest()
        hash_prefix = digest[:8]
        hash_number = int.from_bytes(hash_prefix, "big")
        position = hash_number % dimensions
        vector[position] += 1.0

    # Scale nonzero vectors to unit length for cosine comparisons. Keep a zero
    # vector unchanged when there are no terms, avoiding division by zero.
    squared_length = sum(value * value for value in vector)
    magnitude = math.sqrt(squared_length)
    if not magnitude:
        return vector

    normalized_vector: list[float] = []
    for value in vector:
        normalized_value = value / magnitude
        normalized_vector.append(normalized_value)
    return normalized_vector
