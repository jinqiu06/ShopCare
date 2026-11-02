from shopcare.schemas.policy_models import PolicyChunk, PolicyDocument


def split_policy(
    policy: PolicyDocument,
    chunk_size: int = 300,
    overlap: int = 50,
) -> list[PolicyChunk]:
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("输入数据不合法")

    result: list[PolicyChunk] = []

    start = 0
    index = 0

    while start < len(policy.content):
        end = min(start + chunk_size, len(policy.content))

        chunk = PolicyChunk(
            chunk_id=f"{policy.policy_id}:{policy.version}:{index:03d}",
            policy_id=policy.policy_id,
            version=policy.version,
            title=policy.title,
            category=policy.category,
            effective_from=policy.effective_from,
            effective_until=policy.effective_until,
            text=policy.content[start:end],
            start_char=start,
            end_char=end,
        )
        result.append(chunk)

        if end == len(policy.content):
            break

        start = end - overlap
        index += 1

    return result
