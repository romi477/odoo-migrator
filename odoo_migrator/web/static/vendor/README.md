# Vendored front-end libraries

Checked in rather than fetched from a CDN: this is a local tool that must
start with no network, and a pinned file is also the only way to know later
what actually shipped.

| File | Library | Version | Source | sha256 |
|---|---|---|---|---|
| `alpine.min.js` | Alpine.js | 3.14.8 | `https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js` | `b600e363d99d95444db54acbfb2deffec9ae792aa99a09229bcda078e5b55643` |
| `cytoscape.min.js` | Cytoscape.js | 3.30.4 | `https://cdn.jsdelivr.net/npm/cytoscape@3.30.4/dist/cytoscape.min.js` | `1bb5340e549511e111b31e5684872c949ad33d40ea5dba0ad8e7d90c62c7b3b9` |

To update one: download the new file, replace it, update the version and the
hash in this table in the same commit, and say in the commit message why the
bump was needed. A minified blob with no provenance is unreviewable, so the
row is the review.

Verify what is checked in:

```bash
shasum -a 256 odoo_migrator/web/static/vendor/alpine.min.js
shasum -a 256 odoo_migrator/web/static/vendor/cytoscape.min.js
```
