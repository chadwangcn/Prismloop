"""Provider Pod resolution is internal and never request-controlled."""

from __future__ import annotations

import pytest

from src.acep_client import PodStatus
from src.volc_pod import PodResolutionError, SingleOnlineVolcPodResolver


class FakeACEP:
    def __init__(self, pods):
        self._pods = pods

    def list_pod(self):
        return self._pods


def pod(pod_id, online=True, name="poc"):
    return PodStatus(pod_id=pod_id, online=online, intranet_ip="", eip_address="", name=name)


def test_resolves_the_single_online_pod():
    resolver = SingleOnlineVolcPodResolver(FakeACEP([pod("pod-1")]))

    assert resolver.resolve_pod_id() == "pod-1"


def test_refuses_ambiguous_or_empty_provider_profile():
    with pytest.raises(PodResolutionError, match="found 0"):
        SingleOnlineVolcPodResolver(FakeACEP([])).resolve_pod_id()
    with pytest.raises(PodResolutionError, match="found 2"):
        SingleOnlineVolcPodResolver(FakeACEP([pod("pod-1"), pod("pod-2")])).resolve_pod_id()
