from pathlib import Path

from pydantic import ValidationError

from shopcare.schemas.policy_models import PolicyDocument


def load_policy(path: Path) -> PolicyDocument:
    text = path.read_text(encoding="utf-8")

    try:
        return PolicyDocument.model_validate_json(text)
    except ValidationError as exc:
        raise ValueError(f"政策文件格式不正确：{path.name}") from exc


def load_policies(directory: Path) -> list[PolicyDocument]:
    if not directory.exists():
        raise FileNotFoundError(f"政策目录不存在：{directory}")

    if not directory.is_dir():
        raise NotADirectoryError(f"路径不是目录：{directory}")

    policies: list[PolicyDocument] = []
    seen: set[tuple[str, str]] = set()

    for path in sorted(directory.glob("*.json")):
        policy = load_policy(path)

        key = (policy.policy_id, policy.version)

        if key in seen:
            raise ValueError(f"政策版本重复：{key}，文件：{path.name}")

        seen.add(key)
        policies.append(policy)

    return policies
