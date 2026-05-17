# Image Assets

This directory is the canonical home for provide-uterm visual assets used by:

- `README.md` (PyPI/GitHub rendered banner)
- the public site at [`site-uterm-io`](https://github.com/provide-io/site-uterm-io)
- distributed skill docs
- plugin metadata/docs

## Source of truth

- `uterm-banner.png`: original full-size banner (1254 x 1254).

## Generated size ladder

Use these square variants for icons/thumbnails:

- `uterm-logo-128.png`
- `uterm-logo-256.png`
- `uterm-logo-512.png`
- `uterm-logo-1024.png`

## Regeneration workflow

Regenerate all derived sizes from the banner source:

```bash
scripts/generate_image_assets.sh
```

The script is cross-platform:

- macOS: uses `sips`
- Linux: uses ImageMagick (`magick` or `convert`)

If you replace the source banner, rerun the script and commit the updated outputs in this directory.
