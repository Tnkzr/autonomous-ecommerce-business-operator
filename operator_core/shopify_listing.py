"""Turn a screened candidate into a Shopify draft product.

The missing step in `research → score → select → list on Shopify`. `listings.py`
writes marketplace copy — an SEO title, bullets, backend keywords, image briefs.
Shopify needs a different shape: HTML body, a handle that becomes the URL, SEO
meta fields with their own length limits, tags, and a variant carrying price and
cost. This module is the translation, and it is a separate file because the two
sides change for different reasons — a Shopify field rename should not touch
copywriting logic.

Four things it gets right that a naive mapping does not.

**The handle is generated once and pinned.** The handle is the product URL. Every
video published points at it, so changing it later costs every view those videos
earned — Shopify does not redirect a changed handle by default. `[shopify]
pin_handles` says so; this module derives the handle from the title at creation
and never recomputes it.

**SEO meta fields have hard limits that are not the same as the copy limits.**
A meta title over ~60 characters and a description over ~160 get truncated by
search engines mid-word. The marketplace title is optimised for a different
algorithm entirely, so it is trimmed at a word boundary rather than reused.

**Body HTML is escaped, then structured.** Product copy is generated text and
goes into an HTML field. Passing it through unescaped turns an ampersand in a
product name into broken markup and an angle bracket into an injection.

**Cost per item is set when known, and its absence is reported.** Shopify
computes margin from `unitCost`. Leaving it unset means every margin figure the
admin shows is wrong, and `storefront.sync_line_items` cannot compute profit
either — so a missing cost is surfaced here, at the point where it could still
be filled in, rather than discovered a month later in a report.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

from .config import Policy
from .listings import ListingDraft
from .models import ProductCandidate

# Search engines truncate around these lengths. Not Shopify limits — Shopify
# accepts longer and then the result is cut mid-word on the results page.
SEO_TITLE_MAX = 60
SEO_DESCRIPTION_MAX = 160

# Shopify's own ceilings.
HANDLE_MAX = 255
TAG_MAX = 255
MAX_TAGS = 250


@dataclass
class ShopifyProductPlan:
    """Everything needed to create one draft product, plus what is missing."""

    sku: str
    product_input: dict[str, Any]
    variant_input: dict[str, Any]
    handle: str
    collections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku, "handle": self.handle,
            "product": self.product_input, "variant": self.variant_input,
            "collections": self.collections,
            "warnings": self.warnings, "blockers": self.blockers,
            "ready": self.ready,
        }


def handleize(text: str) -> str:
    """Shopify's handle rules. Also the product URL — see the module docstring."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return slug[:HANDLE_MAX] or "product"


def trim_at_word(text: str, limit: int) -> str:
    """Trim to a limit without cutting a word in half."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,.;:-—")


def build_body_html(draft: ListingDraft, candidate: ProductCandidate) -> str:
    """Structured HTML from generated copy, escaped.

    Everything interpolated here is generated text that will be rendered as
    markup, so it is escaped first. An unescaped ampersand in a product name is
    broken markup; an unescaped angle bracket is worse.
    """
    def esc(value: Any) -> str:
        return html.escape(str(value or ""), quote=False)

    parts: list[str] = []
    if draft.description:
        for paragraph in [p for p in draft.description.split("\n\n") if p.strip()]:
            parts.append(f"<p>{esc(paragraph.strip())}</p>")

    if draft.bullets:
        parts.append("<ul>")
        parts.extend(f"<li>{esc(bullet)}</li>" for bullet in draft.bullets)
        parts.append("</ul>")

    if draft.faq:
        parts.append("<h3>Questions</h3>")
        for entry in draft.faq:
            question = esc(entry.get("q") or entry.get("question", ""))
            answer = esc(entry.get("a") or entry.get("answer", ""))
            if question and answer:
                parts.append(f"<p><strong>{question}</strong><br>{answer}</p>")

    if not parts:
        parts.append(f"<p>{esc(candidate.title)}</p>")
    return "\n".join(parts)


def build_tags(candidate: ProductCandidate, draft: ListingDraft) -> list[str]:
    """Tags from the product's own keywords and category.

    Tags drive collection membership and storefront filtering, so they are the
    product's real attributes rather than marketing words. Deduplicated
    case-insensitively because Shopify treats `Storage` and `storage` as two
    tags and the storefront then shows both.
    """
    raw = list(candidate.keywords) + [candidate.category]
    if candidate.brand:
        raw.append(candidate.brand)
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw:
        cleaned = " ".join(str(item or "").split())[:TAG_MAX]
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(cleaned)
    return tags[:MAX_TAGS]


def build_product_plan(policy: Policy, candidate: ProductCandidate,
                       draft: ListingDraft, *, unit_cost: float | None = None,
                       compare_at_price: float | None = None,
                       inventory_quantity: int | None = None
                       ) -> ShopifyProductPlan:
    """Map a candidate and its listing copy into Shopify's shape.

    Always produces a DRAFT. The status is not a parameter: creation is only
    safe to do autonomously *because* a draft is invisible and reversible, and a
    status this function could be handed is a status it will eventually be
    handed wrongly.
    """
    cfg = policy.raw.get("shopify", {})
    warnings: list[str] = []
    blockers: list[str] = []

    handle = handleize(draft.title or candidate.title)
    seo_title = trim_at_word(draft.title or candidate.title, SEO_TITLE_MAX)
    seo_description = trim_at_word(
        draft.description or " ".join(draft.bullets), SEO_DESCRIPTION_MAX)

    if not seo_description:
        warnings.append(
            "No SEO description could be built from the listing copy. Search "
            "engines will show an arbitrary page fragment instead.")

    product_input: dict[str, Any] = {
        "title": draft.title or candidate.title,
        "handle": handle,
        "descriptionHtml": build_body_html(draft, candidate),
        # Not a parameter. See the docstring.
        "status": "DRAFT",
        "productType": candidate.category,
        "tags": build_tags(candidate, draft),
        "seo": {"title": seo_title, "description": seo_description},
    }
    if candidate.brand:
        product_input["vendor"] = candidate.brand
    else:
        warnings.append(
            "No brand set, so Shopify's vendor field is left empty. It is "
            "used for filtering and appears in some themes.")

    price = float(candidate.target_price)
    if price <= 0:
        blockers.append(
            f"Target price is {price}. Refusing to build a product that would "
            "be listed at or below zero.")

    variant_input: dict[str, Any] = {
        "price": f"{price:.2f}",
        "inventoryItem": {"tracked": True},
        "optionValues": [{"optionName": "Title", "name": "Default Title"}],
    }
    variant_input["inventoryItem"]["sku"] = candidate.sku

    resolved_cost = unit_cost
    if resolved_cost is None and candidate.supplier is not None:
        resolved_cost = getattr(candidate.supplier, "landed_unit_cost", None)
    if resolved_cost is not None and resolved_cost > 0:
        variant_input["inventoryItem"]["cost"] = f"{float(resolved_cost):.2f}"
    else:
        warnings.append(
            "No unit cost available, so Shopify's cost per item is left unset. "
            "Every margin figure in the Shopify admin will be wrong, and "
            "per-product profit cannot be computed from orders.")

    if compare_at_price is not None:
        if compare_at_price <= price:
            # A compare-at at or below the price is a fake discount. It is a
            # consumer-protection problem in several jurisdictions, not a
            # formatting error.
            blockers.append(
                f"compare_at_price {compare_at_price:.2f} is not above the "
                f"price {price:.2f}. A struck-through price that was never "
                "charged is a misleading-pricing claim, not a promotion.")
        else:
            variant_input["compareAtPrice"] = f"{compare_at_price:.2f}"

    if inventory_quantity is not None and inventory_quantity < 0:
        blockers.append(
            f"Inventory quantity {inventory_quantity} is negative.")

    max_discount = float(cfg.get("max_discount_pct", 100.0))
    if compare_at_price and compare_at_price > price:
        implied = (compare_at_price - price) / compare_at_price * 100
        if implied > max_discount:
            warnings.append(
                f"The compare-at price implies a {implied:.0f}% discount, over "
                f"the {max_discount:.0f}% ceiling in [shopify]. A permanent "
                "deep discount trains buyers to wait and erodes the anchor.")

    for issue in draft.warnings:
        warnings.append(f"listing: {issue}")

    collections = [candidate.category] if candidate.category else []

    return ShopifyProductPlan(
        sku=candidate.sku, product_input=product_input,
        variant_input=variant_input, handle=handle, collections=collections,
        warnings=warnings, blockers=blockers)


def publish_plan(connector: Any, plan: ShopifyProductPlan) -> dict[str, Any]:
    """Create the draft product and its variant.

    Two calls, not one: since API version 2024-10 `productCreate` does not
    accept variants, and collapsing them back into one call is how this breaks
    on the next version bump. Creating a draft needs no approval — only the
    later move to ACTIVE does.
    """
    if not plan.ready:
        raise ValueError(
            f"Refusing to create {plan.sku}: " + "; ".join(plan.blockers))

    created = connector.create_draft_product(
        title=plan.product_input["title"],
        description_html=plan.product_input["descriptionHtml"],
        vendor=plan.product_input.get("vendor", ""),
        product_type=plan.product_input.get("productType", ""),
        tags=plan.product_input.get("tags", []),
        seo_title=(plan.product_input.get("seo") or {}).get("title", ""),
        seo_description=(plan.product_input.get("seo") or {}).get("description", ""),
        handle=plan.handle,
    )
    product_id = created.payload["product_id"]
    variants = connector.add_variants(product_id, [plan.variant_input])
    return {
        "product_id": product_id,
        "handle": created.payload["handle"],
        "status": created.payload["status"],
        "variants": variants.payload,
        "warnings": created.warnings + plan.warnings,
    }
