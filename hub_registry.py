"""Code-owned adapters; explicit host fixture injection, no plugin imports."""
from hub_contract import KINDS, require
from hub_adapter_agy import AgyHubAdapter
from hub_adapter_claude import ClaudeHubAdapter
from hub_adapter_codex import CodexHubAdapter

FACTORIES = dict(agy=AgyHubAdapter,claude=ClaudeHubAdapter,codex=CodexHubAdapter)


class HubRegistry:
    def __init__(self, adapters=()):
        self.adapters={}
        for adapter in adapters:self.register(adapter)

    def register(self, adapter):
        require(adapter.kind in KINDS and adapter.kind not in self.adapters,'HUB_ADAPTER_COLLISION')
        adapter.capabilities()
        self.adapters[adapter.kind]=adapter

    def kinds(self):return tuple(sorted(FACTORIES))

    def get(self, kind):
        require(kind in FACTORIES,'UNKNOWN_HUB_ADAPTER')
        if kind not in self.adapters:self.register(FACTORIES[kind]())
        return self.adapters[kind]

    def doctor_all(self):
        return {kind:self.get(kind).doctor() for kind in self.kinds()}
