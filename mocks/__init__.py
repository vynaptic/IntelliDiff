"""Black-box mock service pairs with seeded discrepancies.

Each pair is two aiohttp apps: variant ``a`` (the legacy service) and variant ``b`` (its
replacement), serving one deterministic dataset in two different wire shapes. DeltaSleuth sees
them only as endpoints plus declared behaviour (``<pair>/targets.yaml``); nothing here depends on
DeltaSleuth, exactly as a real target would not.

* ``python -m mocks.run <pair> --variant a|b --port N`` serves one variant.
* ``python -m mocks.fixtures <pair> --out DIR`` writes layout-1 file samples for both variants.
* ``<pair>/expected.json`` is the ground truth the end-to-end tests assert against.
"""
