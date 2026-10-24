"""Check documentation links and requirement-to-code references without network access."""

import re
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from scripts._support import message


def anchors(text: str) -> set[str]:
    """Recognize Markdown heading fragments used by repository documents."""
    result = set()
    seen: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE):
        value = re.sub(r"[^\w\s-]", "", heading.lower()).replace(" ", "-")
        count = seen.get(value, 0)
        seen[value] = count + 1
        result.add(f"{value}-{count}" if count else value)
    return result


def validate(root: Path) -> list[str]:
    """Return safe file and contract identities for invalid references."""
    errors: list[str] = []
    root = root.resolve()
    documents = [root / name for name in ("README.md", "CONTRIBUTING.md", "AGENTS.md")]
    documents += sorted((root / "docs").rglob("*.md"))
    for path in documents:
        relative = path.relative_to(root).as_posix()
        if not path.is_file():
            errors.append(relative)
            continue
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            link = urlsplit(target.strip("<>"))
            if link.scheme or link.netloc:
                continue
            destination = (path.parent / unquote(link.path)).resolve() if link.path else path
            if not destination.is_relative_to(root) or not destination.exists():
                errors.append(f"{relative}:link")
            elif (
                link.fragment
                and destination.suffix == ".md"
                and unquote(link.fragment) not in anchors(destination.read_text(encoding="utf-8"))
            ):
                errors.append(f"{relative}:fragment")
    registry = tomllib.loads((root / "docs/requirements/catalog.toml").read_text(encoding="utf-8"))
    if registry.get("format_version") != 1 or not registry.get("requirements"):
        errors.append("requirements:format")
    for identity, requirement in registry.get("requirements", {}).items():
        if not re.fullmatch(r"REQ-[A-Z][A-Z0-9-]*", identity):
            errors.append("requirements:identity")
        if requirement.get("status") not in {"planned", "in_progress", "implemented"}:
            errors.append(f"{identity}:status")
        if not requirement.get("summary") or not requirement.get("acceptance"):
            errors.append(f"{identity}:acceptance")
        for field, prefix in (
            ("sources", ("src/", "scripts/", "bootstrap/")),
            ("tests", ("tests/",)),
        ):
            references = requirement.get(field, [])
            if requirement.get("status") == "implemented" and not references:
                errors.append(f"{identity}:{field}")
            for name in references:
                target = (root / name).resolve()
                if (
                    not name.startswith(prefix)
                    or not target.is_relative_to(root)
                    or not target.is_file()
                ):
                    errors.append(f"{identity}:{field}")
    return sorted(set(errors))


def main(root: Path | None = None) -> None:
    """Fail the gate on stale links or incomplete implemented requirements."""
    errors = validate(root or Path(__file__).resolve().parents[2])
    if errors:
        raise SystemExit(message("quality.docs_invalid", identities=", ".join(errors)))
    print(message("quality.docs_valid"))


if __name__ == "__main__":
    main()
