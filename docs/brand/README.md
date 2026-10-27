# Argus Council brand

The mark is an eye whose iris is a ring of eight seats around an amber pupil. Argus Panoptes was
the hundred-eyed watcher; here the council deliberates inside the iris and the pupil is the human
who oversees it. The wordmark is drawn as monoline strokes, so no font is needed to render it.

All files are generated from one set of geometry by `tools/build_brand.py`. Edit the script, not
the SVGs, and run it from the repository root:

```bash
python3 tools/build_brand.py
```

## Files

| File | Use |
| --- | --- |
| `argus-mark.svg` | The mark on light backgrounds |
| `argus-mark-dark.svg` | The mark on dark backgrounds |
| `argus-mark-mono.svg` | Single colour (`currentColor`), for print, embossing and stamps |
| `argus-lockup-light.svg` | Mark and wordmark, horizontal, on light backgrounds |
| `argus-lockup-dark.svg` | Mark and wordmark, horizontal, on dark backgrounds |
| `argus-lockup-mono.svg` | Lockup in one colour |
| `favicon.svg`, `favicon.ico` | The app icon: the mark in white on an indigo tile (ICO holds 16, 32 and 48 px) |
| `png/` | Raster exports: favicons (16, 32, 48), `apple-touch-icon.png` (180), `icon-192.png`, `icon-512.png`, the mark at 512 px, and `social-preview.png` |
| `social-preview.svg`, `png/social-preview.png` | 1280 x 640 GitHub social preview |

Copies live in `docs/assets/` (the docs site header logo and favicon) and `frontend/public/`
(the web app's favicon, which the app shell and sign-in page also use as the in-app logo).

The PNG files are rasterised from the SVGs in a browser (draw the SVG on a `<canvas>` at the
target size and save it with `toBlob`); `build_brand.py` then packs `png/favicon-{16,32,48}.png`
into `favicon.ico`. Re-export the PNGs whenever the geometry changes.

## Colours

| Name | Hex | Role |
| --- | --- | --- |
| Argus indigo | `#4338CA` | Lids and seats on light backgrounds, the icon tile, the web app's light accent |
| Iris | `#8B93F8` | Lids and seats on dark backgrounds, the web app's dark accent |
| Watch amber | `#F5A524` | The pupil on light backgrounds and on the icon tile |
| Watch amber (dark) | `#FBBF24` | The pupil on dark backgrounds |
| Ink | `#1B1D3A` | Wordmark on light backgrounds, monochrome variants |
| Night | `#0F1024` | Dark surfaces such as the social preview |

Amber is used only for the pupil. It is the one warm point in the identity and stands for the
person in the loop, so do not use it for decoration elsewhere.

## Clear space and size

- Keep clear space around the mark equal to the pupil's diameter (about one sixth of the mark's
  width) on every side. For the lockup, the same space applies around the whole lockup.
- Smallest sizes: the mark at 24 px wide, the lockup at 120 px wide. Below 24 px use the favicon
  tile, which is drawn for 16 px.
- Do not recolour the seats separately from the lids, rotate the mark, add effects (shadows,
  glows, gradients), set the wordmark in a typeface, or place the light variant on a dark
  background.
