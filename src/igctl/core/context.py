"""Runtime context shared by CLI commands: resolved account, backend, store, guard."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property

from igctl.backends.base import Backend, IgctlError
from igctl.core.config import Account, BackendName, Config
from igctl.core.storage import Store


@dataclass
class Context:
    account_name: str | None = None
    backend_override: BackendName | None = None
    dry_run: bool = False
    json_output: bool = False
    _backends: dict[str, Backend] = field(default_factory=dict)

    @cached_property
    def config(self) -> Config:
        return Config.load()

    @cached_property
    def account(self) -> Account:
        return self.config.account(self.account_name)

    @cached_property
    def store(self) -> Store:
        return Store()

    @cached_property
    def guard(self):
        from igctl.core.ratelimit import Guard

        return Guard(self.account.name, self.account.limits, self.store, dry_run=self.dry_run)

    def backend(self, prefer: BackendName | None = None) -> Backend:
        """The backend for this account, honouring ``--backend`` then ``prefer``."""
        name = self.backend_override or prefer or self.account.backend
        if name not in self._backends:
            self._backends[name] = make_backend(name, self.account)
        return self._backends[name]


def make_backend(name: BackendName, account: Account) -> Backend:
    if name == "graph":
        from igctl.backends.graph import GraphBackend

        return GraphBackend(account)
    if name == "private":
        from igctl.backends.private import PrivateBackend

        return PrivateBackend(account)
    raise IgctlError(f"Unknown backend '{name}'.")
