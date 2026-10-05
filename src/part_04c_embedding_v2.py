# v5.2 embedding backend: current NVIDIA free multilingual embedding endpoint.
# NVIDIA requires input_type=query for searches and input_type=passage for indexed memory.
MEMORY_EMBEDDING_MODEL = os.getenv(
    "MEMORY_EMBEDDING_MODEL",
    "nvidia/nemotron-3-embed-1b",
).strip() or "nvidia/nemotron-3-embed-1b"


def _memory_embed(text: str, input_type: str = "passage") -> list[float]:
    if not MEMORY_STRUCTURED_ENABLED:
        return []
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return []
    if input_type not in {"query", "passage"}:
        input_type = "passage"

    response = llm_client.embeddings.create(
        model=MEMORY_EMBEDDING_MODEL,
        input=[text[:12000]],
        encoding_format="float",
        extra_body={
            "input_type": input_type,
            "truncate": "END",
        },
    )
    return list(response.data[0].embedding)


def retrieve_relevant_memories(user_id: int, query: str) -> list[dict]:
    if not MEMORY_STRUCTURED_ENABLED:
        return []

    items = memory_list_items(
        user_id,
        status="active",
        kinds=("fact", "project", "decision", "preference", "open_topic"),
        limit=MEMORY_MAX_ITEMS,
    )
    if not items:
        return []

    query_embedding: list[float] = []
    try:
        query_embedding = _memory_embed(query, "query")
    except Exception:
        logger.exception("Semantic memory query embedding failed; using lexical fallback")

    ranked = []
    for item in items:
        semantic = 0.0
        emb = _load_embedding(item.get("embedding", ""))
        if query_embedding and emb:
            semantic = _cosine_similarity(query_embedding, emb)
        lexical = _lexical_memory_score(query, item.get("content", ""))
        score = max(semantic, lexical * 0.72)
        score += max(0, int(item.get("importance", 3)) - 3) * 0.015
        if item.get("kind") == "open_topic":
            score += 0.02
        ranked.append((score, item))

    ranked.sort(key=lambda x: x[0], reverse=True)
    selected = [item for score, item in ranked if score >= MEMORY_MIN_SIMILARITY]
    if not selected and ranked:
        selected = [item for score, item in ranked[:2] if score >= 0.12]
    return selected[:MEMORY_SEMANTIC_TOP_K]


def _close_matching_topic(user_id: int, topic_text: str) -> bool:
    topics = memory_get_open_topics(user_id, 20)
    if not topics:
        return False

    normalized = _normalize_memory_text(topic_text)
    for topic in topics:
        old = _normalize_memory_text(topic["content"])
        if normalized in old or old in normalized:
            memory_update_item(int(topic["id"]), status="closed")
            return True

    try:
        q_emb = _memory_embed(topic_text, "query")
    except Exception:
        q_emb = []

    best = (0.0, None)
    if q_emb:
        for topic in topics:
            emb = _load_embedding(topic.get("embedding", ""))
            score = _cosine_similarity(q_emb, emb) if emb else 0.0
            if score > best[0]:
                best = (score, topic)
    if best[1] is not None and best[0] >= 0.55:
        memory_update_item(int(best[1]["id"]), status="closed")
        return True
    return False
