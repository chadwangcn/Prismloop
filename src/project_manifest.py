"""Prismloop product project manifests.

Project manifests keep repository, Figma, Android, cloud-phone and test-plan
selection out of Agent instructions.  A task consumes a manifest plus a
``ResolvedProjectPin`` so that mutable project configuration and immutable
execution evidence remain separate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

import yaml


class ProjectManifestError(ValueError):
    """Raised when a project manifest is incomplete or unsafe."""


_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_GITHUB_REPOSITORY_RE = re.compile(
    r"^(?:https://github\.com/)?(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
_FIGMA_FILE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
_ANDROID_PACKAGE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$"
)
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MUTABLE_PIN_VALUES = {"latest", "head", "main", "master", "current"}


@dataclass(frozen=True)
class GitHubSource:
    repository: str
    default_ref: str

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.repository}.git"


@dataclass(frozen=True)
class FigmaSource:
    provider: str
    file_key: str
    file_url: Optional[str]
    version_policy: str
    version: Optional[str]
    contract_path: str
    pages: List[str]
    frames: List[str]


@dataclass(frozen=True)
class AndroidBuild:
    package_name: str
    workspace_subdir: str
    build_command: str
    apk_glob: str
    min_sdk: int
    target_sdk: int


@dataclass(frozen=True)
class CloudPhoneExecution:
    environment_ref: str
    device_profiles: Dict[str, Dict[str, Any]]
    pools: Dict[str, str]


@dataclass(frozen=True)
class TestPlans:
    catalog: Dict[str, str]
    developer_smoke: str
    qa_acceptance: str


@dataclass(frozen=True)
class ArtifactPolicy:
    store_ref: str
    prefix_template: str
    url_ttl_seconds: int


@dataclass(frozen=True)
class ProjectManifest:
    schema_version: str
    project_id: str
    display_name: str
    status: str
    source: GitHubSource
    design: FigmaSource
    android: AndroidBuild
    execution: CloudPhoneExecution
    test_plans: TestPlans
    artifacts: ArtifactPolicy

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def config_sha256(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ResolvedProjectPin:
    schema_version: str
    project_id: str
    project_config_sha256: str
    repository: str
    clone_url: str
    requested_ref: str
    git_commit: str
    git_tree: Optional[str]
    figma_file_key: str
    figma_version: str
    figma_contract_path: str
    developer_smoke_plan: str
    qa_acceptance_plan: str
    artifact_store_ref: str
    artifact_prefix: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectManifestError(f"{name} must be an object")
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ProjectManifestError(f"{name} contains unknown fields: {unknown}")


def _string(data: Mapping[str, Any], key: str, name: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProjectManifestError(f"{name}.{key} must be a non-empty string")
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        raise ProjectManifestError(f"{name}.{key} is still a placeholder")
    return value


def _optional_string(data: Mapping[str, Any], key: str, name: str) -> Optional[str]:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProjectManifestError(f"{name}.{key} must be a non-empty string")
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        raise ProjectManifestError(f"{name}.{key} is still a placeholder")
    return value


def _string_list(data: Mapping[str, Any], key: str, name: str) -> List[str]:
    values = data.get(key, [])
    if not isinstance(values, list) or any(
        not isinstance(item, str) or not item.strip() for item in values
    ):
        raise ProjectManifestError(f"{name}.{key} must be a list of strings")
    return [item.strip() for item in values]


def _repo_relative_path(value: str, name: str, allow_dot: bool = False) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ProjectManifestError(f"{name} must stay within the project repository")
    if not allow_dot and value in {"", "."}:
        raise ProjectManifestError(f"{name} must name a repository-relative file")
    return value


def _normalize_github_repository(value: str) -> str:
    match = _GITHUB_REPOSITORY_RE.fullmatch(value)
    if not match:
        raise ProjectManifestError(
            "source.github.repository must be owner/repo or an HTTPS GitHub URL"
        )
    return f"{match.group('owner')}/{match.group('repo')}"


def _validate_figma_url(file_url: str, file_key: str) -> None:
    parsed = urlparse(file_url)
    if parsed.scheme != "https" or parsed.netloc not in {"figma.com", "www.figma.com"}:
        raise ProjectManifestError("design.figma.file_url must be an HTTPS Figma URL")
    if file_key not in parsed.path:
        raise ProjectManifestError(
            "design.figma.file_url does not contain design.figma.file_key"
        )
    if parsed.username or parsed.password or parsed.query:
        raise ProjectManifestError(
            "design.figma.file_url must not contain credentials or query parameters"
        )


def load_project_manifest(path: str | Path) -> ProjectManifest:
    """Load and validate a product project manifest from YAML or JSON."""
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"project manifest not found: {manifest_path}")

    with manifest_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    root = _mapping(raw, "project")
    _reject_unknown(
        root,
        {
            "schema_version",
            "project_id",
            "display_name",
            "status",
            "source",
            "design",
            "android",
            "execution",
            "test_plans",
            "artifacts",
        },
        "project",
    )

    schema_version = _string(root, "schema_version", "project")
    if schema_version != "prismloop.project.v1":
        raise ProjectManifestError(
            f"unsupported schema_version: {schema_version}"
        )

    project_id = _string(root, "project_id", "project")
    if not _PROJECT_ID_RE.fullmatch(project_id):
        raise ProjectManifestError(
            "project.project_id must use lowercase letters, digits and hyphens"
        )
    status = _string(root, "status", "project")
    if status not in {"draft", "active", "retired"}:
        raise ProjectManifestError("project.status must be draft, active or retired")

    source = _mapping(root.get("source"), "source")
    _reject_unknown(source, {"github"}, "source")
    github = _mapping(source.get("github"), "source.github")
    _reject_unknown(github, {"repository", "default_ref"}, "source.github")
    repository = _normalize_github_repository(
        _string(github, "repository", "source.github")
    )
    default_ref = _string(github, "default_ref", "source.github")
    if any(char.isspace() for char in default_ref):
        raise ProjectManifestError("source.github.default_ref must not contain whitespace")

    design = _mapping(root.get("design"), "design")
    _reject_unknown(design, {"figma"}, "design")
    figma = _mapping(design.get("figma"), "design.figma")
    _reject_unknown(
        figma,
        {
            "provider",
            "file_key",
            "file_url",
            "version_policy",
            "version",
            "contract_path",
            "pages",
            "frames",
        },
        "design.figma",
    )
    provider = _string(figma, "provider", "design.figma")
    if provider != "figma":
        raise ProjectManifestError("design.figma.provider must be figma")
    file_key = _string(figma, "file_key", "design.figma")
    if not _FIGMA_FILE_KEY_RE.fullmatch(file_key):
        raise ProjectManifestError("design.figma.file_key has an invalid format")
    file_url = _optional_string(figma, "file_url", "design.figma")
    if file_url:
        _validate_figma_url(file_url, file_key)
    version_policy = _string(figma, "version_policy", "design.figma")
    if version_policy not in {"pinned", "resolve_at_run"}:
        raise ProjectManifestError(
            "design.figma.version_policy must be pinned or resolve_at_run"
        )
    version = _optional_string(figma, "version", "design.figma")
    if version_policy == "pinned" and not version:
        raise ProjectManifestError(
            "design.figma.version is required when version_policy is pinned"
        )
    contract_path = _repo_relative_path(
        _string(figma, "contract_path", "design.figma"),
        "design.figma.contract_path",
    )

    android = _mapping(root.get("android"), "android")
    _reject_unknown(
        android,
        {
            "package_name",
            "workspace_subdir",
            "build_command",
            "apk_glob",
            "min_sdk",
            "target_sdk",
        },
        "android",
    )
    package_name = _string(android, "package_name", "android")
    if not _ANDROID_PACKAGE_RE.fullmatch(package_name):
        raise ProjectManifestError("android.package_name has an invalid format")
    workspace_subdir = _repo_relative_path(
        _string(android, "workspace_subdir", "android"),
        "android.workspace_subdir",
        allow_dot=True,
    )
    build_command = _string(android, "build_command", "android")
    apk_glob = _repo_relative_path(
        _string(android, "apk_glob", "android"), "android.apk_glob"
    )
    min_sdk = android.get("min_sdk")
    target_sdk = android.get("target_sdk")
    if not isinstance(min_sdk, int) or min_sdk < 21:
        raise ProjectManifestError("android.min_sdk must be an integer >= 21")
    if not isinstance(target_sdk, int) or target_sdk < min_sdk:
        raise ProjectManifestError("android.target_sdk must be >= android.min_sdk")

    execution = _mapping(root.get("execution"), "execution")
    _reject_unknown(
        execution, {"environment_ref", "device_profiles", "pools"}, "execution"
    )
    environment_ref = _string(execution, "environment_ref", "execution")
    device_profiles_raw = _mapping(
        execution.get("device_profiles"), "execution.device_profiles"
    )
    if not device_profiles_raw:
        raise ProjectManifestError("execution.device_profiles must not be empty")
    device_profiles: Dict[str, Dict[str, Any]] = {}
    for profile_id, profile_value in device_profiles_raw.items():
        if not isinstance(profile_id, str) or not _PROJECT_ID_RE.fullmatch(profile_id):
            raise ProjectManifestError(
                f"invalid execution.device_profiles key: {profile_id!r}"
            )
        profile = dict(_mapping(profile_value, f"execution.device_profiles.{profile_id}"))
        allowed_profile = {
            "platform",
            "android_api",
            "resolution",
            "dpi",
            "orientation",
            "capabilities",
        }
        _reject_unknown(
            profile, allowed_profile, f"execution.device_profiles.{profile_id}"
        )
        if profile.get("platform") != "android":
            raise ProjectManifestError(
                f"execution.device_profiles.{profile_id}.platform must be android"
            )
        if not isinstance(profile.get("android_api"), int):
            raise ProjectManifestError(
                f"execution.device_profiles.{profile_id}.android_api must be an integer"
            )
        resolution = profile.get("resolution")
        if not isinstance(resolution, str) or not re.fullmatch(r"\d{2,5}x\d{2,5}", resolution):
            raise ProjectManifestError(
                f"execution.device_profiles.{profile_id}.resolution must be WIDTHxHEIGHT"
            )
        if profile.get("orientation") not in {"portrait", "landscape"}:
            raise ProjectManifestError(
                f"execution.device_profiles.{profile_id}.orientation is invalid"
            )
        capabilities = profile.get("capabilities", [])
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) or not item for item in capabilities
        ):
            raise ProjectManifestError(
                f"execution.device_profiles.{profile_id}.capabilities must be strings"
            )
        device_profiles[profile_id] = profile

    pools_raw = _mapping(execution.get("pools"), "execution.pools")
    _reject_unknown(pools_raw, {"dev", "qa", "regression"}, "execution.pools")
    pools: Dict[str, str] = {}
    for pool_name in ("dev", "qa"):
        profile_id = _string(pools_raw, pool_name, "execution.pools")
        if profile_id not in device_profiles:
            raise ProjectManifestError(
                f"execution.pools.{pool_name} references unknown profile {profile_id}"
            )
        pools[pool_name] = profile_id
    regression = pools_raw.get("regression")
    if regression is not None:
        if not isinstance(regression, str) or regression not in device_profiles:
            raise ProjectManifestError(
                "execution.pools.regression must reference a device profile"
            )
        pools["regression"] = regression

    test_plans = _mapping(root.get("test_plans"), "test_plans")
    _reject_unknown(
        test_plans, {"catalog", "developer_smoke", "qa_acceptance"}, "test_plans"
    )
    catalog_raw = _mapping(test_plans.get("catalog"), "test_plans.catalog")
    if not catalog_raw:
        raise ProjectManifestError("test_plans.catalog must not be empty")
    catalog: Dict[str, str] = {}
    for plan_id, plan_path in catalog_raw.items():
        if not isinstance(plan_id, str) or not _PROJECT_ID_RE.fullmatch(plan_id):
            raise ProjectManifestError(f"invalid test plan id: {plan_id!r}")
        if not isinstance(plan_path, str) or not plan_path.strip():
            raise ProjectManifestError(f"test plan {plan_id} must have a path")
        catalog[plan_id] = _repo_relative_path(
            plan_path.strip(), f"test_plans.catalog.{plan_id}"
        )
    developer_smoke = _string(test_plans, "developer_smoke", "test_plans")
    qa_acceptance = _string(test_plans, "qa_acceptance", "test_plans")
    for role, plan_id in {
        "developer_smoke": developer_smoke,
        "qa_acceptance": qa_acceptance,
    }.items():
        if plan_id not in catalog:
            raise ProjectManifestError(
                f"test_plans.{role} references unknown plan {plan_id}"
            )

    artifacts = _mapping(root.get("artifacts"), "artifacts")
    _reject_unknown(
        artifacts, {"store_ref", "prefix_template", "url_ttl_seconds"}, "artifacts"
    )
    store_ref = _string(artifacts, "store_ref", "artifacts")
    prefix_template = _string(artifacts, "prefix_template", "artifacts")
    if "{project_id}" not in prefix_template or "{git_commit}" not in prefix_template:
        raise ProjectManifestError(
            "artifacts.prefix_template must contain {project_id} and {git_commit}"
        )
    url_ttl_seconds = artifacts.get("url_ttl_seconds")
    if not isinstance(url_ttl_seconds, int) or not 60 <= url_ttl_seconds <= 86400:
        raise ProjectManifestError(
            "artifacts.url_ttl_seconds must be between 60 and 86400"
        )

    return ProjectManifest(
        schema_version=schema_version,
        project_id=project_id,
        display_name=_string(root, "display_name", "project"),
        status=status,
        source=GitHubSource(repository=repository, default_ref=default_ref),
        design=FigmaSource(
            provider=provider,
            file_key=file_key,
            file_url=file_url,
            version_policy=version_policy,
            version=version,
            contract_path=contract_path,
            pages=_string_list(figma, "pages", "design.figma"),
            frames=_string_list(figma, "frames", "design.figma"),
        ),
        android=AndroidBuild(
            package_name=package_name,
            workspace_subdir=workspace_subdir,
            build_command=build_command,
            apk_glob=apk_glob,
            min_sdk=min_sdk,
            target_sdk=target_sdk,
        ),
        execution=CloudPhoneExecution(
            environment_ref=environment_ref,
            device_profiles=device_profiles,
            pools=pools,
        ),
        test_plans=TestPlans(
            catalog=catalog,
            developer_smoke=developer_smoke,
            qa_acceptance=qa_acceptance,
        ),
        artifacts=ArtifactPolicy(
            store_ref=store_ref,
            prefix_template=prefix_template,
            url_ttl_seconds=url_ttl_seconds,
        ),
    )


def resolve_project_pin(
    manifest: ProjectManifest,
    *,
    git_commit: str,
    figma_version: Optional[str] = None,
    git_tree: Optional[str] = None,
) -> ResolvedProjectPin:
    """Create an immutable execution pin from a validated project manifest.

    Network resolution is intentionally outside this function.  GitHub and
    Figma adapters must resolve mutable refs first, then pass the exact values
    here for validation and receipt generation.
    """
    git_commit = git_commit.strip().lower()
    if not _GIT_SHA_RE.fullmatch(git_commit):
        raise ProjectManifestError("git_commit must be a full 40-character SHA")
    if git_tree is not None:
        git_tree = git_tree.strip().lower()
        if not _GIT_SHA_RE.fullmatch(git_tree):
            raise ProjectManifestError("git_tree must be a full 40-character SHA")

    resolved_figma_version = (figma_version or manifest.design.version or "").strip()
    if not resolved_figma_version:
        raise ProjectManifestError(
            "figma_version is required to create an immutable project pin"
        )
    if resolved_figma_version.lower() in _MUTABLE_PIN_VALUES:
        raise ProjectManifestError("figma_version must be immutable, not a moving alias")

    prefix = manifest.artifacts.prefix_template.format(
        project_id=manifest.project_id,
        git_commit=git_commit,
        figma_version=resolved_figma_version,
    )
    return ResolvedProjectPin(
        schema_version="prismloop.resolved-project-pin.v1",
        project_id=manifest.project_id,
        project_config_sha256=manifest.config_sha256,
        repository=manifest.source.repository,
        clone_url=manifest.source.clone_url,
        requested_ref=manifest.source.default_ref,
        git_commit=git_commit,
        git_tree=git_tree,
        figma_file_key=manifest.design.file_key,
        figma_version=resolved_figma_version,
        figma_contract_path=manifest.design.contract_path,
        developer_smoke_plan=manifest.test_plans.developer_smoke,
        qa_acceptance_plan=manifest.test_plans.qa_acceptance,
        artifact_store_ref=manifest.artifacts.store_ref,
        artifact_prefix=prefix,
    )
