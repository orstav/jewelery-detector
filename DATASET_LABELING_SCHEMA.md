# Dataset Labeling Schema

This dataset separates product identity from media attribution.

An image can belong to multiple products as catalog media without implying those
products are the same item.

## Core Fields

### `correct_product_ids`

The product IDs this image should be attached to.

Examples:

- `R059`
- `E137,N091`

### `media_role`

One of:

- `identity`: clean single-product image that can help define product identity.
- `supporting`: single-product image that belongs to the product but should not
  define identity alone, such as a lifestyle, model, crop, or detail image.
- `shared_supporting`: image intentionally showing multiple products or a set.
- `wrong_product`: current attribution is wrong and should be corrected.
- `exclude`: do not use this image in the clean dataset.
- `needs_followup`: cannot decide quickly.

### `identity_eligible`

Whether this image can be used as evidence for same-product identity.

- `true` only for `media_role = identity`
- `false` for supporting, shared, excluded, and unresolved images

### `supports_multiple_products`

Whether the image is valid media for more than one product.

Typical set/model image:

```text
media_role = shared_supporting
correct_product_ids = E137,N091
identity_eligible = false
supports_multiple_products = true
```

## Clustering Policy

- `identity` images can participate in identity clustering.
- `supporting` images attach to an already-known product but do not create merge
  evidence by themselves.
- `shared_supporting` images attach to multiple products after identity
  clustering and must not merge those products.
- `wrong_product`, `exclude`, and `needs_followup` stay out of automated identity
  clustering until resolved.

## Human Review Priority

Highest-value review items:

1. Images with multiple product IDs.
2. Images crossing product categories.
3. Shared files across product folders.
4. Folder/filename ID mismatches.

For category-crossing model photos, the expected label is usually
`shared_supporting`, not an error.
