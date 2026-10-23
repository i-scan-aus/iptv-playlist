
name: Update IPTV playlist

on:
  workflow_dispatch:
  schedule:
    - cron: '17 3 * * *'
  push:
    branches:
      - main

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Generate IPTV playlist
        run: |
          python build_playlist.py

      - name: Prepare website
        run: |
          mkdir -p _site
          cp playlist.m3u _site/playlist.m3u
          if [ -f index.html ]; then
            cp index.html _site/index.html
          else
            echo '<h1>IPTV Playlist</h1><p><a href="playlist.m3u">Download M3U Playlist</a></p>' > _site/index.html
          fi

      - name: Setup GitHub Pages
        uses: actions/configure-pages@v5

      - name: Upload Pages artifact
        uses: actions/upload-pages-artifact@v3
        with:
          path: _site

      - name: Deploy to GitHub Pages
        uses: actions/deploy-pages@v4
