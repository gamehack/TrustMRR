"""Idempotent, mandatory Notion article handoff (Python standard library only).

Notion is the authoritative source for all future channel drafts. A write or
readback failure must fail the production run without a READY marker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

_API = "https://api.notion.com"
_VERSION = "2026-03-11"
_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,50}:[0-9]{4}-[0-9]{2}(?:-[0-9]{2})?$")
_MAX_MARKDOWN = 180_000
_READY_RE = re.compile(
    r"^QJAI_PUBLISH_READY v=1 source_key=(?P<source>[a-z0-9-]+:[0-9]{4}-[0-9]{2}(?:-[0-9]{2})?) "
    r"content_sha256=(?P<sha>[0-9a-f]{64})$"
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("NOTION_PARENT_PAGE_ID must be a valid UUID") from None


def _enabled(name: str) -> bool:
    value = os.getenv(name, "false").strip().lower()
    if value not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError(f"{name} must be true or false")
    return value in ("true", "1", "yes")


def autosync_enabled() -> bool:
    return _enabled("NOTION_AUTOSYNC_AFTER_GENERATION")


def _request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    """Allow only known Notion API paths; fail closed on redirects/uncertainty."""
    if not path.startswith("/v1/") or "//" in path or ".." in path:
        raise ValueError("Invalid Notion API path")
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = Request(
        _API + path,
        data=raw,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Notion-Version": _VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "QJAI-Notion-Archive/1.0",
        },
    )
    try:
        with build_opener(_NoRedirect()).open(req, timeout=20) as resp:
            if not 200 <= resp.status < 300:
                raise RuntimeError("Notion returned an unexpected response")
            data = resp.read(500_001)
    except HTTPError as exc:
        # Never log request bodies, auth headers or Notion's returned page text.
        raise RuntimeError(f"Notion API returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        # A network error after POST is ambiguous; next run checks for the child.
        raise RuntimeError("Notion API connection failed; reconcile before retrying") from None
    if len(data) > 500_000:
        raise RuntimeError("Notion API response exceeds expected limit")
    try:
        parsed = json.loads(data)
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError("Notion API returned invalid JSON") from None
    if not isinstance(parsed, dict):
        raise RuntimeError("Notion API returned invalid object")
    return parsed


def _find_existing(parent_id: str, unique_prefix: str, token: str) -> dict | None:
    """Search every child page, including pagination; user-renamed headlines survive."""
    cursor = None
    matches = []
    seen = set()
    while True:
        suffix = "?page_size=100"
        if cursor:
            suffix += "&start_cursor=" + quote(cursor, safe="")
        result = _request("GET", f"/v1/blocks/{parent_id}/children{suffix}", token)
        blocks = result.get("results")
        if not isinstance(blocks, list) or not isinstance(result.get("has_more"), bool):
            raise RuntimeError("Notion child-page response was incomplete")
        for block in blocks:
            if not isinstance(block, dict):
                raise RuntimeError("Notion child-page result was invalid")
            if block.get("type") != "child_page":
                continue
            title = block.get("child_page", {}).get("title", "")
            if isinstance(title, str) and title.startswith(unique_prefix):
                matches.append({"id": block.get("id"), "title": title})
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
        if not isinstance(cursor, str) or cursor in seen:
            raise RuntimeError("Notion pagination was inconsistent")
        seen.add(cursor)
    if len(matches) > 1:
        raise RuntimeError("More than one Notion article matches the stable period prefix")
    return matches[0] if matches else None


def _validated_markdown(title: str, source_key: str, markdown: str) -> str:
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("Invalid Notion article title")
    if not isinstance(source_key, str) or not _KEY_RE.fullmatch(source_key):
        raise ValueError("Invalid Notion source key")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("Notion article body is empty")
    if len(markdown.encode("utf-8")) > _MAX_MARKDOWN:
        raise ValueError("Notion article body is too large")
    return markdown


def _canonical(markdown: str) -> str:
    return markdown.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def _comparison_form(markdown: str) -> str:
    """Compare Markdown v2 semantics across Notion's code-tag/escape serialization."""
    value = _canonical(markdown)
    value = re.sub(r"<code>(.*?)</code>", r"`\1`", value, flags=re.DOTALL)
    # Notion escapes literal Markdown punctuation in readback; those escapes
    # do not change the rendered content and must not invalidate the digest.
    return re.sub(r"\\([\\`*_{}\[\]()#+.!|<>~-])", r"\1", value)


def _read_markdown(page_id: str, token: str) -> str:
    result = _request(
        "GET", f"/v1/pages/{_uuid(page_id)}/markdown?markdown_version=v2", token
    )
    if result.get("object") != "page_markdown" or not isinstance(result.get("markdown"), str):
        raise RuntimeError("Notion Markdown readback was invalid")
    if result.get("truncated") is not False:
        raise RuntimeError("Notion Markdown readback was truncated")
    if result.get("unknown_block_ids") not in (None, []):
        raise RuntimeError("Notion Markdown readback contains unknown blocks")
    if result.get("warnings") not in (None, []):
        raise RuntimeError("Notion Markdown readback returned warnings")
    return _canonical(result["markdown"])


def _verify_created_page(page_id: str, parent_id: str, title: str, token: str) -> None:
    page = _request("GET", f"/v1/pages/{_uuid(page_id)}", token)
    parent = page.get("parent") or {}
    if parent.get("type") != "page_id" or _uuid(parent.get("page_id", "")) != parent_id:
        raise RuntimeError("Created Notion page is outside the configured parent")
    titles = []
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            titles.append("".join(str(item.get("plain_text", "")) for item in prop.get("title", [])))
    if titles != [title]:
        raise RuntimeError("Created Notion page title did not round-trip")


def _append_ready(page_id: str, source_key: str, digest: str, token: str) -> str:
    marker = f"QJAI_PUBLISH_READY v=1 source_key={source_key} content_sha256={digest}"
    _request("PATCH", f"/v1/blocks/{_uuid(page_id)}/children", token, {
        "children": [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{
                "type": "text", "text": {"content": marker},
            }]},
        }],
    })
    return marker


def sync_article(*, source_key: str, title: str, markdown: str,
                 unique_prefix: str) -> dict[str, str]:
    """Create one dated child page, or confirm its existence without overwriting.

    Notion Markdown v2 readback canonicalizes inline code and escaped punctuation;
    keep generated metadata prose plain to preserve exact content round-trips.

    No broad search and no upsert: an ambiguous create may only be retried after
    re-reading all children. The parent must be a dedicated article-only page.
    """
    if not _enabled("NOTION_ENABLE_SYNC"):
        raise PermissionError("NOTION_ENABLE_SYNC must explicitly be true")
    token = os.getenv("NOTION_API_KEY", "").strip()
    if len(token) < 20:
        raise ValueError("NOTION_API_KEY is not configured")
    parent_id = _uuid(os.getenv("NOTION_PARENT_PAGE_ID", ""))
    _validated_markdown(title, source_key, markdown)
    if not unique_prefix or not title.startswith(unique_prefix):
        raise ValueError("Notion title must begin with its stable period prefix")
    if len(unique_prefix) > 80:
        raise ValueError("Notion period prefix is too long")

    found = _find_existing(parent_id, unique_prefix, token)
    if found is not None:
        # A partial earlier POST, or a manually edited historical page, is NOT
        # a delivered article. Never report success without verifying the
        # existing child's immutable READY handshake and content digest.
        existing = _read_markdown(found["id"], token)
        lines = existing.rstrip("\n").split("\n")
        match = _READY_RE.fullmatch(lines[-1].strip().replace(r"\\_", "_")) if lines else None
        if match is None or match.group("source") != source_key:
            raise RuntimeError("Existing Notion period page lacks a valid READY marker; reconcile manually")
        signed_body = _canonical("\n".join(lines[:-1]))
        if hashlib.sha256(signed_body.encode("utf-8")).hexdigest() != match.group("sha"):
            raise RuntimeError("Existing Notion READY content has been modified; reconcile manually")
        return {
            "result": "duplicate_ready",
            "source_key": source_key,
            "page_id": found["id"],
            "page_url": "https://www.notion.so/" + found["id"].replace("-", ""),
        }

    original_digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    # Remove the existing H1 to avoid a duplicate article title in the body.
    content = re.sub(r"^#\s+[^\n]+\n+", "", markdown, count=1)
    header = (
        "> 潜界 AI 文章归档。来源唯一键：`" + source_key + "`。"
        " 原文 SHA-256：`" + original_digest + "`。\n\n"
    )
    document = _canonical(header + content)
    if len(document.encode("utf-8")) > _MAX_MARKDOWN:
        raise ValueError("Notion archived Markdown is too large")
    result = _request("POST", "/v1/pages", token, {
        "parent": {"type": "page_id", "page_id": parent_id},
        "properties": {"title": {"type": "title", "title": [{"type": "text", "text": {"content": title}}]}},
        "markdown_version": "v2",
        "markdown": document,
    })
    page_id = result.get("id")
    page_url = result.get("url")
    if not isinstance(page_id, str) or not isinstance(page_url, str):
        raise RuntimeError("Notion creation acknowledgement is incomplete")
    _verify_created_page(page_id, parent_id, title, token)
    readback = _read_markdown(page_id, token)
    if _comparison_form(readback) != _comparison_form(document):
        raise RuntimeError("Notion Markdown readback differs from the archived article")
    verified_digest = hashlib.sha256(readback.encode("utf-8")).hexdigest()
    marker = _append_ready(page_id, source_key, verified_digest, token)
    final = _read_markdown(page_id, token)
    lines = final.rstrip().split("\n")
    final_marker = lines[-1].strip().replace(r"\\_", "_") if lines else ""
    if not lines or not _READY_RE.fullmatch(final_marker) or final_marker != marker:
        raise RuntimeError("Notion READY marker did not round-trip")
    verified_body = _canonical("\n".join(lines[:-1]))
    if verified_body != readback or hashlib.sha256(verified_body.encode("utf-8")).hexdigest() != verified_digest:
        raise RuntimeError("Notion content changed while appending READY")
    return {"result": "created_ready", "source_key": source_key,
            "page_id": page_id, "page_url": page_url}


def check_parent_access() -> dict:
    """Verify the Notion token can read the configured archive parent; no writes."""
    token = os.getenv("NOTION_API_KEY", "").strip()
    if len(token) < 20:
        raise ValueError("NOTION_API_KEY is not configured")
    parent_id = _uuid(os.getenv("NOTION_PARENT_PAGE_ID", ""))
    result = _request("GET", f"/v1/blocks/{parent_id}/children?page_size=1", token)
    if not isinstance(result.get("results"), list) or not isinstance(result.get("has_more"), bool):
        raise RuntimeError("Notion parent read response was incomplete")
    return {"readable": True, "sampled_children": len(result["results"])}


def read_existing_ready(source_key: str, unique_prefix: str) -> str | None:
    """Read only one exact report period from the canonical Notion parent."""
    token = os.getenv("NOTION_API_KEY", "").strip()
    if len(token) < 20:
        raise ValueError("NOTION_API_KEY is not configured")
    parent_id = _uuid(os.getenv("NOTION_PARENT_PAGE_ID", ""))
    found = _find_existing(parent_id, unique_prefix, token)
    if found is None:
        return None
    raw = _read_markdown(found["id"], token)
    lines = raw.rstrip("\n").split("\n")
    match = _READY_RE.fullmatch(lines[-1]) if lines else None
    if match is None or match.group("source") != source_key:
        raise RuntimeError("Existing report period lacks correct READY; manual reconciliation required")
    signed = _canonical("\n".join(lines[:-1]))
    if hashlib.sha256(signed.encode("utf-8")).hexdigest() != match.group("sha"):
        raise RuntimeError("Historical Notion READY digest mismatch")
    return signed
