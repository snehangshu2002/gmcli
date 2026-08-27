"""Per-invocation state shared by every command.

Lives in its own module so commands can import it without importing ``cli``,
which imports them back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .cache import Cache
from .config import Config
from .errors import AuthError, UsageError
from .output import Renderer

if TYPE_CHECKING:  # pragma: no cover
    from .api.client import GmailClient
    from .api.labels import LabelIndex


@dataclass
class AppContext:
    """Global flags, resolved config, and lazily-built API access."""

    account_override: str | None = None
    json_mode: bool = False
    quiet: bool = False
    color: bool = True
    config: Config = field(default_factory=Config)
    _renderer: Renderer | None = field(default=None, repr=False)
    _client: "GmailClient | None" = field(default=None, repr=False)
    _cache: Cache | None = field(default=None, repr=False)
    _labels: "LabelIndex | None" = field(default=None, repr=False)
    _account: str | None = field(default=None, repr=False)

    # -- rendering -----------------------------------------------------------

    @property
    def renderer(self) -> Renderer:
        if self._renderer is None:
            self._renderer = Renderer(
                json_mode=self.json_mode,
                color=self.color and self.config.output.color,
                quiet=self.quiet,
            )
        return self._renderer

    # -- account resolution --------------------------------------------------

    @property
    def account(self) -> str:
        """Which account this invocation acts on.

        Order: ``--account``, then the configured default, then the only
        logged-in account if there is exactly one. With several accounts and no
        default we stop rather than guess — picking the wrong mailbox is worse
        than an error message.
        """
        if self._account:
            return self._account

        from .auth.store import list_accounts

        if self.account_override:
            self._account = self.account_override
            return self._account

        if self.config.default_account:
            self._account = self.config.default_account
            return self._account

        accounts = list_accounts()
        if len(accounts) == 1:
            self._account = accounts[0]
            return self._account
        if not accounts:
            raise AuthError(
                "No account is logged in.",
                hint="Run `gmail auth login --credentials "
                "/path/to/client_secret.json` to get started.",
            )
        raise UsageError(
            f"Several accounts are available ({', '.join(accounts)}) "
            "and no default is set.",
            hint="Pick one with --account, or set a default with "
            "`gmail auth switch <email>`.",
        )

    # -- lazily-built collaborators -----------------------------------------

    @property
    def cache(self) -> Cache:
        if self._cache is None:
            self._cache = Cache(self.account)
        return self._cache

    @property
    def client(self) -> "GmailClient":
        """Build the API client on first use.

        Kept lazy so commands that touch no network (``--help``, ``cache
        clear``, ``send --dry-run``) never trigger a token refresh.
        """
        if self._client is None:
            from .api.client import GmailClient

            self._client = GmailClient.for_account(self.account)
        return self._client

    @property
    def labels(self) -> "LabelIndex":
        if self._labels is None:
            from .api.labels import LabelIndex

            self._labels = LabelIndex.load(self.client, self.cache)
        return self._labels
