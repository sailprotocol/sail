"""
Discovery transport.

Two backends, selected by the REGISTRY env var (default "local"):
  local  Phase-0 stand-in: hosts write ./registry/<pubkey>.json and clients read the dir.
  nostr  Real Nostr relays: the host publishes a signed kind-38111 listing event; clients
         subscribe, verify each event's signature, and parse listings.

The HostListing.to_nostr_event() / from_nostr_event() shape (shared/listing.py) is the real
one for both backends, so only the transport differs. Phase 1 next step: reach relays over Tor.
"""
from __future__ import annotations

import json
import os
import pathlib

from shared.listing import HostListing, LISTING_KIND
from shared.pow import leading_zero_bits, nip01_id, mine

# Value of the "n" tag that scopes our listings on shared public relays.
NOSTR_TAG_VALUE = "inference-net-v0"


def _pow_target() -> int:
    return int(os.getenv("POW_TARGET", "16"))      # leading-zero bits the host mines (anti-spam)


def _pow_min() -> int:
    return int(os.getenv("POW_MIN_DIFFICULTY", "8"))  # listings below this are rejected by clients


class RegistryBackend:
    def publish(self, listing: HostListing) -> None:
        raise NotImplementedError

    def discover(self) -> list[HostListing]:
        raise NotImplementedError

    def host_pubkey(self) -> str | None:
        """Stable host identity for this backend, or None to let the daemon pick its own."""
        return None


class LocalRegistry(RegistryBackend):
    """Phase-0 local-file transport. No network, no identity."""

    def __init__(self) -> None:
        self.dir = pathlib.Path(os.getenv("REGISTRY_DIR", "./registry"))

    def publish(self, listing: HostListing) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        event = listing.to_nostr_event()
        target = _pow_target()
        if target > 0:
            mine(event, target)  # NIP-13 proof-of-work, same as the real Nostr path
        (self.dir / f"{listing.pubkey}.json").write_text(json.dumps(event))

    def discover(self) -> list[HostListing]:
        if not self.dir.exists():
            return []
        min_diff = _pow_min()
        out = []
        for f in self.dir.glob("*.json"):
            try:
                event = json.loads(f.read_text())
                if min_diff > 0 and leading_zero_bits(nip01_id(event)) < min_diff:
                    continue  # reject under-difficulty listings
                out.append(HostListing.from_nostr_event(event))
            except Exception:
                continue
        return out


def _relays() -> list[str]:
    return [r.strip() for r in os.getenv("NOSTR_RELAYS", "").split(",") if r.strip()]


def _run(coro_factory):
    """Run an async coroutine to completion in a dedicated thread with its own event loop.

    nostr-sdk's Client is async; this lets the sync publish/discover calls work whether or
    not the caller already has a running loop (e.g. FastAPI's startup hook runs in one).
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro_factory())).result()


class NostrRegistry(RegistryBackend):
    """Real Nostr publish/subscribe over the relays in NOSTR_RELAYS.

    The host signs with the secret key in NOSTR_HOST_NSEC (env only, never committed); its
    Nostr pubkey becomes the listing identity. Clients need no key to discover.
    """

    def __init__(self) -> None:
        self.relays = _relays()
        if not self.relays:
            raise ValueError(
                "REGISTRY=nostr requires NOSTR_RELAYS (comma-separated relay URLs)"
            )

    def host_pubkey(self) -> str | None:
        from nostr_sdk import Keys

        return Keys.parse(os.environ["NOSTR_HOST_NSEC"]).public_key().to_hex()

    def publish(self, listing: HostListing) -> None:
        from nostr_sdk import Client, EventBuilder, Keys, Kind, NostrSigner, RelayUrl, Tag

        keys = Keys.parse(os.environ["NOSTR_HOST_NSEC"])
        ev = listing.to_nostr_event()  # reuse the canonical content + tags
        builder = EventBuilder(Kind(LISTING_KIND), ev["content"]).tags(
            [Tag.parse(t) for t in ev["tags"]]
        )
        target = _pow_target()
        if target > 0:
            builder = builder.pow(target)  # NIP-13: mine a nonce into the signed event id
        signed = builder.sign_with_keys(keys)
        relays = self.relays

        async def _go():
            client = Client(NostrSigner.keys(keys))
            for url in relays:
                await client.add_relay(RelayUrl.parse(url))
            await client.connect()
            await client.send_event(signed)
            await client.shutdown()

        _run(_go)

    def discover(self) -> list[HostListing]:
        from datetime import timedelta

        from nostr_sdk import Client, Filter, Kind, RelayUrl

        relays = self.relays

        async def _go():
            client = Client()  # read-only: no signer needed to subscribe
            for url in relays:
                await client.add_relay(RelayUrl.parse(url))
            await client.connect()
            events = await client.fetch_events(
                Filter().kind(Kind(LISTING_KIND)), timedelta(seconds=5)
            )
            out = events.to_vec()
            await client.shutdown()
            return out

        min_diff = _pow_min()
        by_pubkey: dict[str, HostListing] = {}
        for ev in _run(_go):
            if not ev.verify():  # schnorr signature + event id check
                continue
            tags = [t.as_vec() for t in ev.tags().to_vec()]
            if not any(len(t) >= 2 and t[0] == "n" and t[1] == NOSTR_TAG_VALUE for t in tags):
                continue
            if min_diff > 0 and leading_zero_bits(ev.id().to_hex()) < min_diff:
                continue  # reject listings below the client's minimum PoW difficulty
            try:
                listing = HostListing.from_nostr_event(
                    {
                        "pubkey": ev.author().to_hex(),
                        "content": ev.content(),
                        "created_at": ev.created_at().as_secs(),
                    }
                )
            except Exception:
                continue
            prev = by_pubkey.get(listing.pubkey)
            if prev is None or listing.updated_at >= prev.updated_at:  # latest wins
                by_pubkey[listing.pubkey] = listing
        return list(by_pubkey.values())


_backend: RegistryBackend | None = None


def get_backend() -> RegistryBackend:
    global _backend
    if _backend is None:
        kind = os.getenv("REGISTRY", "local").lower()
        if kind == "local":
            _backend = LocalRegistry()
        elif kind == "nostr":
            _backend = NostrRegistry()
        else:
            raise ValueError(f"unknown REGISTRY backend: {kind}")
    return _backend


def publish(listing: HostListing) -> None:
    get_backend().publish(listing)


def discover() -> list[HostListing]:
    return get_backend().discover()


def host_identity() -> str | None:
    """The host's stable identity pubkey for the selected backend (None for local)."""
    return get_backend().host_pubkey()
