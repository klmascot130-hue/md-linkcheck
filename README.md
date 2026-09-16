# md-linkcheck

A fast, zero-dependency Markdown link checker. Point it at a file or a
directory and it reports every broken link — HTTP(S) URLs *and* local
file references — with file name and line number. Perfect for CI.

No third-party packages: Python 3.9+ standard library only.

## Install

```bash
pip install .
```

Or run it straight from the source tree without installing:

```bash
PYTHONPATH=src python -m md_linkcheck.cli README.md
```

## Usage

```bash
# Check one file
md-linkcheck README.md

# Check a whole docs folder recursively
md-linkcheck docs/

# Machine-readable output
md-linkcheck README.md --format json

# Skip URLs matching a pattern (repeatable)
md-linkcheck docs/ --exclude 'localhost' --exclude 'example\.com'

# Only external (http/https) links
md-linkcheck README.md --only-external

# Tune performance
md-linkcheck docs/ --workers 20 --timeout 5 --retries 2
```

Example output:

```
docs/guide.md:12: BROKEN https://old-site.example/page (HTTP 404)
README.md:44: BROKEN images/missing.png (file not found: images/missing.png)

3 link(s) checked, 2 broken.
```

Exit codes: `0` = all links OK, `1` = broken links found, `2` = error.

## What it checks

- Inline links `[text](url)`, images `![alt](url)`, autolinks
  `<https://…>`, bare URLs, and reference-style links `[text][ref]`.
- Local relative links (e.g. `docs/setup.md`, `images/logo.png`) are
  verified against the filesystem.
- `mailto:`, `tel:`, `#anchors`, and links inside fenced/inline code
  blocks are skipped.
- HTTP checks use `HEAD` with a `GET` fallback (some servers reject
  HEAD), follow redirects, and retry on transient network errors.

## CI example

```yaml
# .github/workflows/linkcheck.yml
name: Link check
on: [push, pull_request]
jobs:
  links:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install .
      - run: md-linkcheck README.md docs/
```

## Tests

```bash
python -m unittest discover -s tests
```

## License

MIT — see [LICENSE](LICENSE).
