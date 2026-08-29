"""Non-public Volc Pod selection for provider adapters."""

from __future__ import annotations

from typing import Any, Protocol


class PodResolutionError(RuntimeError):
    """The configured provider profile cannot resolve exactly one usable Pod."""


class PodResolver(Protocol):
    def resolve_pod_id(self) -> str:
        ...


class SingleOnlineVolcPodResolver:
    """Resolve the only online Pod visible to the configured ACEP product.

    The public Harness request never includes a Pod ID.  Deployments with more
    than one online Pod must supply a scheduler-backed resolver instead of
    relying on accidental list ordering.
    """

    def __init__(self, acep_client: Any, pod_name: str | None = None) -> None:
        self._acep_client = acep_client
        self._pod_name = pod_name

    def resolve_pod_id(self) -> str:
        candidates = [pod for pod in self._acep_client.list_pod() if pod.online]
        if self._pod_name:
            candidates = [pod for pod in candidates if pod.name == self._pod_name]
        if len(candidates) != 1:
            selector = f"name={self._pod_name}" if self._pod_name else "single online pod"
            raise PodResolutionError(f"provider profile requires exactly one pod ({selector}), found {len(candidates)}")
        return candidates[0].pod_id
