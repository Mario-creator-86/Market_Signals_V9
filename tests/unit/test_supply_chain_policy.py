from pathlib import Path

from tools.supply_chain_check import ROOT, validate_requirement_files


def test_project_requirement_files_are_exactly_pinned():
    assert validate_requirement_files(
        [ROOT / "requirements.txt", ROOT / "requirements-dev.txt"]
    ) == []


def test_policy_rejects_ranges_urls_and_dependency_confusion(tmp_path: Path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "\n".join(
            (
                "safe-package>=1.0",
                "remote @ https://example.test/remote.whl",
                "multipart==1.0.0",
            )
        ),
        encoding="utf-8",
    )

    issues = validate_requirement_files([requirements])

    assert len(issues) == 3
    assert any("exact == version pin" in issue for issue in issues)
    assert any("URL requirements are forbidden" in issue for issue in issues)
    assert any("dependency-confusion" in issue for issue in issues)


def test_github_actions_are_pinned_to_full_commit_shas():
    workflows = tuple((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows
    for workflow in workflows:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("- uses:"):
                stripped = stripped[2:].strip()
            if not stripped.startswith("uses:"):
                continue
            reference = stripped.removeprefix("uses:").strip().split("#", 1)[0].strip()
            assert "@" in reference, f"{workflow.name}: missing action reference"
            revision = reference.rsplit("@", 1)[1]
            assert len(revision) == 40 and all(
                character in "0123456789abcdef" for character in revision.lower()
            ), f"{workflow.name}: action is not SHA-pinned: {reference}"


def test_container_base_image_is_digest_pinned():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    from_lines = [
        line.strip() for line in dockerfile.splitlines() if line.strip().startswith("FROM ")
    ]
    assert from_lines
    assert all("@sha256:" in line for line in from_lines)
