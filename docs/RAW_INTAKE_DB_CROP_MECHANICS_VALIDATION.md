# Detector DB crop mechanics validation

Date: 2026-07-05
Branch: `raw-intake-embedding-consensus`

## Scope

Read-only validation of the **actual crop evidence currently available in the detector DB**. This does not judge theoretical crop code quality; it checks whether the evaluated catalog matcher is actually using persisted crop profiles/crop embeddings.

## Inventory

```text
Active embeddings: 1197
Active images: 1197
Active products: 154
Active non-full embeddings: 0
Active non-cached-full crop sources: 58
Image profile rows: 0
Image profile JSON rows: 0
Product image rows: 1139
```

| view_type | crop_source | active | count |
|---|---|---:|---:|
| `full_image` | `cached_full_image` | True | 1139 |
| `full_image` | `full` | True | 58 |

## Hard-negative sample

```text
Hidden evaluated: False
Total products: 154
Dev products: 139
Hidden products: 15
Sampled hard negatives: 24
Sample query crop IDs: {'catalog_E091_765c5d9bb0c1:full_image': 1, 'catalog_E091_c3f55524548b:full_image': 1, 'catalog_E097_e377daa37ada:full_image': 1, 'catalog_E098_d1f19b57fb10:full_image': 1, 'catalog_E101_4d97b1fa0ba2:full_image': 1, 'catalog_E101_6f6eaf7132ff:full_image': 1, 'catalog_E101_81e01f13677d:full_image': 1, 'catalog_E101_8acd1e44837e:full_image': 1, 'catalog_E102_02ef922f4724:full_image': 1, 'catalog_E102_59dadc0e81e8:full_image': 1, 'catalog_E102_852f0649db61:full_image': 1, 'catalog_E102_85484b9ca770:full_image': 1, 'catalog_E103_142e15d6d612:full_image': 1, 'catalog_E105_06cf6f39297b:full_image': 1, 'catalog_E105_3991ee7e155e:full_image': 1, 'catalog_E105_6f3a1e6dd223:full_image': 1, 'catalog_E106_1b5331251864:full_image': 1, 'catalog_E106_c6c1bb494740:full_image': 1, 'catalog_E108_4ecb41898903:full_image': 1, 'catalog_E108_d48ca03b0e9c:full_image': 1, 'catalog_E108_e908a2c5b7c8:full_image': 1, 'catalog_E109_090124d15a01:full_image': 1, 'catalog_E109_66f5074bd192:full_image': 1, 'catalog_E110_9d3422bc2826:full_image': 1}
```

Review sheet:

```text
/home/server/.hermes/profiles/hermes-hal-9000/workspace/openclaw-hal-import/workspace/apps/jewelery-detector/workbench/raw-intake-embedding-consensus/crop-mechanics-validation/index.html
```

## Conclusion

The current detector DB benchmark is **not validating actual jewelry crop embeddings**. It contains full-image embedding rows only:

```text
view_type = full_image
crop_source = cached_full_image
```

There are also no persisted `image_profiles` rows, so the profile-driven crop mechanics (`vlm_context`, `owlv2_padded`, `owlv2_context`) are not available to validate from current DB state.

Therefore, the validated problem is not "bad crops". The validated problem is:

```text
current evaluated matching evidence = full-image SigLIP only
actual crop mechanics/crop embeddings = not present in DB benchmark
```

## Next required experiment

Generate a bounded offline set of profile-driven crop views for dev catalog images, embed those crop views with SigLIP, and compare:

1. current full-image embeddings;
2. `vlm_context` crop embeddings;
3. `owlv2_padded` crop embeddings;
4. `owlv2_context` crop embeddings;
5. multi-view product aggregation.

Only then can we say whether crop improvement helps or not.
