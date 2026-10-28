from datetime import date

from shopcare.schemas.policy_models import PolicyDocument


def is_policy_effective(
    policy: PolicyDocument,
    as_of: date,
) -> bool:
    if as_of < policy.effective_from:
        return False
    elif policy.effective_until is not None and as_of >= policy.effective_until:
        return False
    else:
        return True


def filter_applicable_policies(
    policies: list[PolicyDocument], category: str, as_of: date
) -> list[PolicyDocument]:
    results: list[PolicyDocument] = []

    for policy in policies:
        if policy.category == category and is_policy_effective(policy, as_of):
            results.append(policy)

    return results
