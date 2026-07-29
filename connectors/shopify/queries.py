"""GraphQL documents, kept apart from the code that sends them.

Two conventions here, both load-bearing:

**Every mutation selects `userErrors { field message }`.** A mutation without
that selection cannot be checked for business-level rejection, and the transport
will refuse to treat it as successful. Adding a mutation means adding the
selection.

**Every list query selects `pageInfo { hasNextPage endCursor }`.** Shopify caps
page size, so a query without a cursor returns a truncated catalogue that looks
complete. A partial product list feeds a "we have no listing for this SKU"
decision that then creates a duplicate.

Field names are API-version-specific. The version is pinned in `credentials.py`;
when a field moves, the transport's error for layer 2 says so explicitly rather
than reporting a generic failure.
"""

from __future__ import annotations

# --- verification -----------------------------------------------------------
SHOP = """
query Shop {
  shop {
    id
    name
    myshopifyDomain
    primaryDomain { url }
    currencyCode
    ianaTimezone
    plan { displayName partnerDevelopment shopifyPlus }
    billingAddress { countryCodeV2 }
  }
}
"""

# `currentAppInstallation.accessScopes` is how the connector distinguishes "bad
# token" (401) from "token fine, scope missing" (403). Those have different
# fixes and only one of them requires reinstalling the app.
ACCESS_SCOPES = """
query AccessScopes {
  currentAppInstallation {
    accessScopes { handle }
  }
}
"""

LOCATIONS = """
query Locations($first: Int!, $after: String) {
  locations(first: $first, after: $after, includeInactive: false) {
    nodes {
      id
      name
      isActive
      fulfillsOnlineOrders
      address { countryCode provinceCode city }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PUBLICATIONS = """
query Publications($first: Int!) {
  publications(first: $first) {
    nodes { id name }
  }
}
"""

# --- products ---------------------------------------------------------------
PRODUCTS = """
query Products($first: Int!, $after: String, $query: String) {
  products(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {
    nodes {
      id
      title
      handle
      status
      vendor
      productType
      tags
      totalInventory
      onlineStoreUrl
      createdAt
      updatedAt
      publishedAt
      seo { title description }
      featuredMedia { ... on MediaImage { id alt } }
      variants(first: 100) {
        nodes {
          id
          title
          sku
          price
          compareAtPrice
          barcode
          inventoryQuantity
          inventoryItem {
            id
            tracked
            unitCost { amount currencyCode }
            measurement { weight { value unit } }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PRODUCT_BY_ID = """
query Product($id: ID!) {
  product(id: $id) {
    id
    title
    handle
    status
    descriptionHtml
    seo { title description }
    tags
    totalInventory
    variants(first: 100) {
      nodes { id sku price compareAtPrice inventoryQuantity inventoryItem { id } }
    }
  }
}
"""

# Product creation deliberately carries no variants: since 2024-10 `productCreate`
# does not accept them, and variants go through `productVariantsBulkCreate`. The
# two-step is not an inefficiency to optimise away — collapsing it back into one
# call is how this breaks on the next version bump.
PRODUCT_CREATE = """
mutation CreateProduct($product: ProductCreateInput!) {
  productCreate(product: $product) {
    product {
      id
      handle
      status
      title
      variants(first: 1) { nodes { id sku } }
    }
    userErrors { field message }
  }
}
"""

PRODUCT_UPDATE = """
mutation UpdateProduct($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id handle status title updatedAt }
    userErrors { field message }
  }
}
"""

VARIANTS_BULK_CREATE = """
mutation CreateVariants($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkCreate(productId: $productId, variants: $variants,
                            strategy: REMOVE_STANDALONE_VARIANT) {
    productVariants { id sku price inventoryItem { id } }
    userErrors { field message }
  }
}
"""

VARIANTS_BULK_UPDATE = """
mutation UpdateVariants($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id sku price compareAtPrice }
    userErrors { field message }
  }
}
"""

PUBLISHABLE_PUBLISH = """
mutation Publish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    publishable { availablePublicationsCount { count } }
    userErrors { field message }
  }
}
"""

# --- inventory --------------------------------------------------------------
# `inventorySetQuantities` sets an absolute on-hand figure; the delta-based
# `inventoryAdjustQuantities` double-counts if a run is retried. An idempotent
# daily sync must be absolute.
INVENTORY_SET = """
mutation SetInventory($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup {
      createdAt
      reason
      changes { name delta quantityAfterChange }
    }
    userErrors { field message }
  }
}
"""

INVENTORY_LEVELS = """
query InventoryLevels($first: Int!, $after: String) {
  productVariants(first: $first, after: $after) {
    nodes {
      id
      sku
      displayName
      inventoryQuantity
      inventoryItem {
        id
        tracked
        unitCost { amount }
        inventoryLevels(first: 10) {
          nodes {
            location { id name }
            quantities(names: ["available", "on_hand", "committed"]) {
              name
              quantity
            }
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# --- orders -----------------------------------------------------------------
# `customerJourneySummary` is the only first-party attribution available: it is
# what tells us whether a sale came from TikTok. Without it the content engine
# is optimising blind.
ORDERS = """
query Orders($first: Int!, $after: String, $query: String) {
  orders(first: $first, after: $after, query: $query, sortKey: CREATED_AT) {
    nodes {
      id
      name
      createdAt
      processedAt
      displayFinancialStatus
      displayFulfillmentStatus
      cancelledAt
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      currentSubtotalPriceSet { shopMoney { amount } }
      totalDiscountsSet { shopMoney { amount } }
      totalShippingPriceSet { shopMoney { amount } }
      totalTaxSet { shopMoney { amount } }
      refunds { totalRefundedSet { shopMoney { amount } } }
      customer { id numberOfOrders }
      customerJourneySummary {
        momentsCount { count }
        firstVisit { source sourceType referrerUrl landingPage utmParameters { source medium campaign content term } }
        lastVisit { source sourceType referrerUrl landingPage utmParameters { source medium campaign content term } }
      }
      lineItems(first: 50) {
        nodes {
          id
          quantity
          sku
          title
          originalTotalSet { shopMoney { amount } }
          discountedTotalSet { shopMoney { amount } }
          variant { id inventoryItem { unitCost { amount } } }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# --- customers --------------------------------------------------------------
CUSTOMERS = """
query Customers($first: Int!, $after: String, $query: String) {
  customers(first: $first, after: $after, query: $query) {
    nodes {
      id
      createdAt
      numberOfOrders
      amountSpent { amount currencyCode }
      lastOrder { id createdAt }
      tags
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# --- collections ------------------------------------------------------------
COLLECTIONS = """
query Collections($first: Int!, $after: String) {
  collections(first: $first, after: $after) {
    nodes { id title handle productsCount { count } updatedAt }
    pageInfo { hasNextPage endCursor }
  }
}
"""

COLLECTION_CREATE = """
mutation CreateCollection($input: CollectionInput!) {
  collectionCreate(input: $input) {
    collection { id title handle }
    userErrors { field message }
  }
}
"""

COLLECTION_ADD_PRODUCTS = """
mutation AddToCollection($id: ID!, $productIds: [ID!]!) {
  collectionAddProducts(id: $id, productIds: $productIds) {
    collection { id title productsCount { count } }
    userErrors { field message }
  }
}
"""

# --- discounts --------------------------------------------------------------
DISCOUNT_CODE_CREATE = """
mutation CreateDiscount($basicCodeDiscount: DiscountCodeBasicInput!) {
  discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
    codeDiscountNode { id }
    userErrors { field message }
  }
}
"""
