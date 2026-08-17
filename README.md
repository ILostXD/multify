# Mutify

<p align="center">
  <img src="assets/Mutify_BG_Color_Logo.svg" width="130" height="130" alt="Mutify Logo" />
</p>

<p align="center">
  <b>Modern Multi-Service Playlist Converter</b><br />
  <i>Seamlessly bridge local .m3u8 & .m3u playlists to Spotify, YouTube Music, and Tidal.</i>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-3776AB?style=flat&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/flask-3.x-000000?style=flat&logo=flask&logoColor=white" alt="Flask" />
  <img src="https://img.shields.io/badge/react-18-61DAFB?style=flat&logo=react&logoColor=black" alt="React" />
  <img src="https://img.shields.io/badge/license-MIT-green.svg?style=flat" alt="MIT License" />
</p>

---

## Overview

**Mutify** is a high-performance, privacy-first playlist converter designed to import local music library playlists (`.m3u8` / `.m3u`) into your favorite cloud streaming services. With intelligent track metadata matching, studio album prioritization, guest search modes, and a stunning liquid glass interface, Mutify makes migrating and synchronizing playlists effortless.

---

## Screenshots & Interface

### 1. Landing Page & Multi-Service Hub
Select your target streaming service with dedicated glowing hero cards and direct dropzone import.

<p align="center">
  <img src="assets/LandingPage.png" alt="Mutify Landing Page" width="800" />
</p>

---

### 2. Intelligent Matcher & Track Review
Live status badges, album art previews, and dropdown candidate selector with manual override and skip options.

<p align="center">
  <img src="assets/SongItem.png" alt="Track Match Review" width="800" />
</p>

---

### 3. Provider Settings & Credentials
Tabbed credential management for Spotify OAuth, YouTube Music session headers, and Tidal device authorization.

<p align="center">
  <img src="assets/Settings.png" alt="Streaming Provider Settings" width="540" />
</p>

---

## Key Features

- **Multi-Service Destination**: Target **Spotify**, **YouTube Music**, or **Tidal** with full data isolation per service.
- **Intelligent Audio Ranking**: Filters out live bootlegs, karaoke versions, and instrumentals to favor original studio albums and official releases.
- **Instant Guest Search**: Search and test YouTube Music and Tidal catalogs out of the box with zero upfront credentials.
- **Manual Review & Dropdown Override**: Pick alternative match candidates or skip individual tracks before generating playlists.
- **Missing Track Exporter**: Export unmatched or skipped tracks into a clean `.txt` list for reference.
- **Fluid Liquid Glass Interface**: High-contrast dark and light modes, reactive ambient orbs, and a floating action dock.
- **Local & Secure**: All credentials, session tokens, and cache files stay 100% local on your machine.

---

## Supported Input Playlists

Mutify parses standard and extended `.m3u` / `.m3u8` playlist files exported from:
- **Navidrome / Subsonic**
- **iTunes / Apple Music**
- **Plex / Jellyfin**
- **VLC / Foobar2000 / Winamp**
- Local file directories (automatic filename metadata fallback)

---

## Quick Start

### 1. Clone Repository
```bash
git clone https://github.com/your-username/mutify.git
cd mutify
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Run the Application
```bash
python app.py
```

### 4. Open in Browser
Navigate to **`http://127.0.0.1:5099`**.

---

## Streaming Provider Setup

### Spotify
1. Create an application on the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Set the **Redirect URI** to `http://127.0.0.1:5099/callback`.
3. Add your Spotify email under **User Management**.
4. Enter your **Client ID** and **Client Secret** in Mutify Settings and click **Connect Spotify**.

### YouTube Music
- **Guest Search**: Works out of the box with zero setup.
- **Playlist Creation**: Copy your cookie request headers from `music.youtube.com` (via browser DevTools Network tab) and paste them into the YouTube Music settings tab.

### Tidal
1. Open Mutify Settings, select **Tidal**, and click **Link Tidal Account**.
2. Click the authorization link to approve the device login on Tidal.
3. Click **Verify Connection** to store the session.

---

## Project Structure

```
mutify/
├── app.py                      # Flask server, OAuth routes, & API handlers
├── requirements.txt            # Project dependencies
├── assets/                     # Official brand vector SVGs and UI screenshots
├── templates/
│   └── index.html              # React UI & Liquid Glass design system
└── providers/
    ├── __init__.py             # Abstract BaseProvider interface
    ├── registry.py             # Dynamic provider resolution
    ├── spotify_provider.py     # Spotify search & playlist creation
    ├── ytmusic_provider.py     # YouTube Music search & playlist creation
    └── tidal_provider.py       # Tidal device auth & playlist creation
```

---

## AI Assistance & Development

This codebase was designed and developed with **AI pair-programming assistance** using **Google DeepMind Antigravity**, adhering to clean architectural patterns, provider abstraction layers, and modern UI/UX design standards.

---

## License

Distributed under the **MIT License**. See `LICENSE` for more information.
