"""Hermes-side helpers: pre-send checks, delivery failures, signature capture.

These are compositions of bridge methods that already exist upstream. Nothing here
adds an add-on method, so a fork can carry them without touching the JS half.

House rules from `._common` apply: typed parameters, a short first sentence in the
docstring, a dict back, `logging` rather than `print`.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from email.utils import getaddresses
from typing import Any

from ..errors import UsageError
from ..server import Registrar
from ._common import call, clamp

_ADDRESS = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_QUOTED = re.compile(r"^\s*>")


async def _tolerant(method: str, variants: list[dict[str, Any]], timeout: float = 45.0) -> Any:
    """Call a bridge method, trying camelCase first and snake_case second.

    The JS handlers and the Python layer have drifted on parameter casing before;
    a fork that composes methods should not break when that happens again.
    """
    last: Exception | None = None
    for params in variants:
        try:
            return await call(method, params, timeout=timeout)
        except UsageError as exc:  # a rejected parameter shape, not a dead bridge
            last = exc
            continue
    if last is not None:
        raise last
    raise UsageError(f"{method} was called with no parameters.")


def _items(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        for key in ("items", "messages", "folders", "accounts", "identities"):
            value = result.get(key)
            if isinstance(value, list):
                return value
    if isinstance(result, list):
        return result
    return []


def _split_recipients(raw: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for name, address in getaddresses([raw or ""]):
        address = (address or "").strip()
        if not address:
            continue
        out.append({"name": (name or "").strip(), "address": address})
    return out


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def _parse_date(value: Any) -> _dt.datetime | None:
    """A bridge date string as an aware datetime, or None when it is not one."""
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.UTC)


def _has_use(folder: dict[str, Any], use: str) -> bool:
    """Whether a folder's specialUse mentions `use`, in either shape it arrives in."""
    uses = folder.get("specialUse")
    if isinstance(uses, str):
        return use in uses.lower()
    if isinstance(uses, list):
        return any(use in str(item).lower() for item in uses)
    return False


async def _folders() -> list[dict[str, Any]]:
    """The whole folder tree, flat."""
    try:
        result = await _tolerant(
            "folders.query",
            [{"depth": 4}, {"depth": 4, "tree": False}],
            timeout=60.0,
        )
    except Exception:  # noqa: BLE001 - callers treat an empty tree as "not loaded yet"
        return []
    return _items(result)


async def _default_identity() -> dict[str, Any] | None:
    for method, params in (
        ("identities.list", {}),
        ("accounts.identities", {}),
        ("accounts.list", {}),
    ):
        try:
            found = _items(await call(method, params, timeout=30.0))
        except Exception:  # noqa: BLE001 - identity lookup must never block a check
            continue
        for item in found:
            identities = item.get("identities")
            if isinstance(identities, list) and identities:
                first = identities[0]
                if isinstance(first, dict):
                    return first
            if item.get("email") or item.get("address"):
                return item
    return None


def register(reg: Registrar) -> None:
    @reg.read_tool(title="Check a message before it is sent")
    async def send_preflight(
        to: str,
        cc: str = "",
        subject: str = "",
        internal_domains: str = "",
    ) -> dict[str, Any]:
        """Check recipients and headers before sending. Sends nothing.

        Reports which recipients are outside the organisation, whether a display
        name would appear where a bare address is required, and which identity the
        message would go out as. Domains come from `internal_domains`
        (comma-separated) or TBMCP_INTERNAL_DOMAINS, else the account's own domain.
        """
        recipients = _split_recipients(to)
        copies = _split_recipients(cc)
        if not recipients:
            raise UsageError("to had no usable address — pass 'Name <user@example.com>'.")

        identity = await _default_identity()
        own = ""
        if identity:
            own = str(identity.get("email") or identity.get("address") or "")

        domains = [d.strip().lower() for d in (internal_domains or os.environ.get("TBMCP_INTERNAL_DOMAINS", "")).split(",") if d.strip()]
        if not domains and own:
            domains = [_domain(own)]

        external, unnamed, malformed = [], [], []
        for person in recipients + copies:
            address = person["address"]
            if "@" not in address or " " in address:
                malformed.append(address)
            if domains and _domain(address) not in domains:
                external.append(address)
            if not person["name"] and not domains:
                unnamed.append(address)

        warnings: list[str] = []
        if external:
            warnings.append(
                f"{len(external)} recipient(s) outside {', '.join(domains) or 'the organisation'}: "
                "some mail servers reject a From header with a display name for these."
            )
        if malformed:
            warnings.append(f"{len(malformed)} address(es) look malformed: {', '.join(malformed[:5])}")
        if not subject.strip():
            warnings.append("subject is empty.")
        if len(recipients) > 20:
            warnings.append(f"{len(recipients)} recipients in to — consider bcc for large lists.")

        return {
            "to": recipients,
            "cc": copies,
            "domains": domains,
            "external": external,
            "internal_count": len(recipients + copies) - len(external),
            "subject": subject,
            "identity": identity,
            "warnings": warnings,
            "note": "Nothing was sent. Sending still needs mail_send or mail_draft_send.",
        }

    @reg.read_tool(title="Find recent delivery failures")
    async def send_bounces(lookback_hours: int = 72, limit: int = 15) -> dict[str, Any]:
        """List recent bounce and non-delivery notices. Sends nothing.

        Scans the newest mail in every Inbox for postmaster and MAILER-DAEMON
        senders, then pulls the failed addresses out of the bodies. Use it after
        sending to an unfamiliar domain, where rejection often arrives minutes later.
        """
        hours = clamp(lookback_hours, default=72, minimum=1, maximum=720, field="lookback_hours")
        size = clamp(limit, default=15, minimum=1, maximum=50, field="limit")
        cutoff = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=hours)

        inboxes = [f for f in await _folders() if _has_use(f, "inbox")]
        reports: list[dict[str, Any]] = []
        for folder in inboxes:
            try:
                listed = _items(
                    await _tolerant(
                        "messages.list",
                        [{"folderId": folder["id"], "limit": 200}, {"folder_id": folder["id"], "limit": 200}],
                    )
                )
            except Exception:  # noqa: BLE001 - one folder failing must not lose the rest
                continue
            for message in listed:
                if len(reports) >= size:
                    break
                author = str(message.get("author") or "").lower()
                if not any(mark in author for mark in ("postmaster", "mailer-daemon", "mail-daemon")):
                    continue
                when = _parse_date(message.get("date"))
                if when and when < cutoff:
                    continue
                failed: list[str] = []
                try:
                    body = await _tolerant(
                        "messages.read",
                        [{"messageId": message.get("id")}, {"message_id": message.get("id")}],
                        timeout=60.0,
                    )
                    text = ""
                    if isinstance(body, dict):
                        text = str(body.get("text") or body.get("body") or "")
                    failed = sorted({a for a in _ADDRESS.findall(text) if a.lower() != own_lower()})
                except Exception:  # noqa: BLE001
                    pass
                reports.append(
                    {
                        "id": message.get("id"),
                        "date": message.get("date"),
                        "author": message.get("author"),
                        "subject": message.get("subject"),
                        "failed_addresses": failed[:10],
                    }
                )
        return {
            "lookback_hours": hours,
            "scan": f"{len(inboxes)} inbox(es), newest 200 each",
            "count": len(reports),
            "bounces": reports,
            "note": "Empty means nothing looked like a delivery failure in that window.",
        }

    @reg.read_tool(title="Find the signature used in sent mail")
    async def signature_candidates(sample: int = 5) -> dict[str, Any]:
        """Extract signature blocks from recent Sent messages. Sends nothing.

        Reads the last `sample` messages in a Sent folder and returns the trailing
        blocks, most frequent first, so a signature can be copied from what was
        actually sent rather than invented.
        """
        count = clamp(sample, default=5, minimum=1, maximum=20, field="sample")
        sent = [f for f in await _folders() if _has_use(f, "sent")]
        if not sent:
            raise UsageError("No Sent folder was found; the folder tree may not be loaded yet.")

        blocks: dict[str, int] = {}
        sampled = 0
        for folder in sent[:2]:
            messages = _items(
                await _tolerant(
                    "messages.list",
                    [{"folderId": folder["id"], "limit": count}, {"folder_id": folder["id"], "limit": count}],
                )
            )
            for message in messages[:count]:
                try:
                    body = await _tolerant(
                        "messages.read",
                        [{"messageId": message.get("id")}, {"message_id": message.get("id")}],
                        timeout=60.0,
                    )
                except Exception:  # noqa: BLE001
                    continue
                sampled += 1
                text = ""
                if isinstance(body, dict):
                    text = str(body.get("text") or body.get("body") or "")
                for block in _candidate_blocks(text):
                    key = "\n".join(block).strip()
                    if key:
                        blocks[key] = blocks.get(key, 0) + 1

        ranked = sorted(blocks.items(), key=lambda kv: (-kv[1], -len(kv[0])))
        return {
            "folders": [f.get("name") for f in sent[:2]],
            "sampled": sampled,
            "candidates": [{"count": n, "lines": text.splitlines()} for text, n in ranked[:3]],
            "note": "Candidates are evidence, not a template. Confirm before reusing.",
        }


def own_lower() -> str:
    """The signed-in address in lower case, or an empty string when unknown."""
    value = os.environ.get("TBMCP_OWN_ADDRESS", "")
    return value.strip().lower()


def _candidate_blocks(text: str) -> list[list[str]]:
    """Trailing paragraphs that look like a signature."""
    lines = [line.rstrip() for line in text.splitlines()]
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    picked: list[list[str]] = []
    for block in blocks[-3:]:
        if len(block) > 8:
            continue
        if any(_QUOTED.match(line) for line in block):
            continue
        picked.append(block)
    return picked
