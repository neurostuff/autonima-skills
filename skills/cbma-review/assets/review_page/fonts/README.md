# Roboto

The review page's typeface, as in Neurosynth Compose. Bundled because the page's content security
policy loads nothing from other hosts, so Google Fonts cannot be used.

- Source: Roboto, Copyright 2015 Google Inc., Apache License 2.0 (`LICENSE`); upstream
  https://github.com/google/roboto (the Debian `fonts-roboto-unhinted` build).
- Regular, Medium, Bold and Italic, subset to Latin-1 and general punctuation and converted to
  WOFF2 with `python3 -m fontTools.subset ... --flavor=woff2`. Characters outside the subset
  (such as ⚠ ✓ ☀) come from the system's fonts.
