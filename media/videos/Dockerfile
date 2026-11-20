# The videos' render environment: the one place they're rendered, so a render comes out
# the same on every machine, CI included. Text is drawn with the bundled fonts, but each
# platform's Chromium rasterizes glyphs its own way (CoreText on macOS, FreeType on
# Linux), so renders made outside this image differ in their antialiasing.
# Playwright's image, at the playwright-core version package.json pins: Chromium's
# headless shell is in it, at /ms-playwright. Pinned by digest; Dependabot bumps it.
FROM mcr.microsoft.com/playwright:v1.65.0-noble@sha256:9beb546099e11f52d62e505537914870e01a67e0e6b00ae41da4775324e73dbe

# FFmpeg with libx264, from Ubuntu's release pocket, which never changes.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg=7:6.1.1-3ubuntu5 \
  && rm -rf /var/lib/apt/lists/*

# pnpm, at the version the repository's package.json names.
RUN npm install --global --ignore-scripts pnpm@12.18.0 && npm cache clean --force
