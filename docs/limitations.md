# Limitations

Honest list of current limitations.

## PDF rasterization

- Uploaded PDFs are rasterized with **pypdfium2** (Google PDFium bindings,
  BSD-3-Clause / Apache-2.0 licensed) at 300 DPI. PDFium is a different
  engine from some other PDF libraries: extremely exotic PDFs (embedded
  forms, rare color spaces) may render with small pixel-level differences.

## Fonts (Japanese PDF output)

- EZ Mark does **not** bundle any font file. For Japanese mark-sheet text the
  app searches installed system fonts at runtime (Noto Sans CJK, Yu Gothic,
  Meiryo, MS Gothic on Windows; Noto Sans CJK under
  `/usr/share/fonts` on Linux) and registers whichever is found.
- If no such system font is available, it falls back to ReportLab's built-in
  Adobe CID fonts (`HeiseiKakuGo-W5` / `HeiseiMin-W3`), which are listed
  *without embedding*: some PDF viewers substitute missing Japanese base
  fonts with square (tofu) glyphs, and the printed file may need
  viewer-specific font availability.
- Consequently, output appearance depends on the fonts installed on the
  machine where sheets are generated. See `THIRD_PARTY_NOTICES.md`.
