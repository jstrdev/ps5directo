# PS5 Directo

**English** · [Español](#español)

A Windows file-transfer tool for jailbroken PS5 consoles. It sends files to the console over FTP **while they download**, without storing them on your PC. It then extracts them, either on the console with BFpilot or on the PC with 7-Zip, and places apps in `/data/homebrew` so ShadowMountPlus shows them in the menu.

![PS5 Directo: paste a link, it's sent, extracted and shows up in the library](docs/demo_en.gif)

<sub>Demo with a sample homebrew app served from a NAS on the local network.</sub>

- ✅ Streams downloads straight to the console: nothing is saved on the PC
- ✅ Works from download links, ftp:// links (e.g. a NAS) or files on your PC
- ✅ Joins split parts (.part1, .part2… / .001, .002…) and extracts RAR, 7z, ZIP and tar (.gz/.bz2/.xz)
- ✅ Extracts on the console (BFpilot) or on the PC (7-Zip, every file CRC-checked)
- ✅ Fixes the file permissions that stop BFpilot-extracted content from launching (CE-107750-0)
- ✅ Pause, resume and retry where it left off, even after a dropped connection
- ✅ Built-in console file browser (works over plain FTP, BFpilot optional): right-click menu, shortcuts, move/copy to…, extract archives on the console, drag & drop uploads, download to PC
- ✅ Notification bell: finished or failed transfers, disconnections and new versions
- ✅ Storage view: a pie chart of what fills the console, by app name and size, flagging duplicates and leftovers you can delete
- ✅ Send whole PC folders over several connections, skipping what's already there and resuming what got cut off
- ✅ Watches free space so the console never fills up
- ✅ Speed limit and built-in speed test
- ✅ Download-manager style downloads: several reused connections per file, auto-backoff when a host complains
- ✅ Optional browser extension: links arrive by themselves with your browser session (same speed and access as the browser)
- ✅ Drag and drop, or "Send to" from Explorer
- ✅ Welcome guide, one-click updates, English and Spanish
- ✅ **Library tab**: covers, versions and the firmware each app needs, flagging what won't launch on your console (backport-aware), duplicates and incomplete folders
- ✅ **Images and LZ4** with [PS5 Dump Forge](https://github.com/quer3q/ps5-dump-forge) (optional): send an app as a single `.ffpkg` / `.ffpfsc` image or LZ4-packed, prepared on the PC
- ✅ Asks before sending something that's already on the console (send anyway, replace or cancel)
- ✅ Checks every part against its `.sfv` / `.md5` / `.sha1` while sending
- ✅ "Make room": suggests what to delete when space runs out
- ✅ Loads ftpsrv and BFpilot on the PS5 from the app (ELF loader, port 9021)
- ✅ "Why is it slow?" network diagnostics (Wi-Fi band and signal, 100 Mb/s cables, VPNs, ping)
- ✅ Open it from your phone with a QR code; schedule the queue and sleep or shut down the PC when it's done

## Requirements

- **PC:** Windows 10/11. Get the `-portable-windows.zip` (Python included, nothing to install), or the plain `.zip` with [Python 3.9+](https://www.python.org/downloads/) installed (tick "Add python.exe to PATH"). For extracting on the PC, also install [7-Zip](https://www.7-zip.org/).
- **Jailbroken PS5:**
  - An FTP server, for example [ftpsrv](https://github.com/ps5-payload-dev/ftpsrv) on port 2121.
  - [BFpilot](https://github.com/ItsBlurf/BFpilot) on port 5905.
  - [ShadowMountPlus](https://github.com/drakmor/ShadowMountPlus).
  - Payloads can be loaded with [ps5-payload-manager](https://github.com/itsPLK/ps5-payload-manager).

## Getting started

1. Download the `-portable-windows.zip` (or the plain `.zip` if you have Python) from the latest [release](../../releases) and extract it anywhere.
2. Run `Crear accesos directos.bat` ("create shortcuts"). It adds a desktop icon and a "Send to → PS5 Directo" entry.
3. Open **PS5 Directo** from the desktop. The language follows Windows; switch it with the **English / Español** button at the top.
4. Follow the welcome guide, enter your PS5's IP in Settings, then paste links or drop files.

> **About BFpilot and permissions:** up to at least v0.4.4, BFpilot's archive extractor leaves files without the execute bit, so the PS5 refuses to launch them (CE-107750-0). This also happens when you extract from BFpilot's own web UI. PS5 Directo fixes it automatically after every extraction. For content you already have, use **Settings → Fix permissions**.

The full guide is in [README_EN.txt](README_EN.txt) (Spanish: [LEEME.txt](LEEME.txt)).

## Publishing a release

Bump `VERSION` in `app.py`, add the notes to `LEEME.txt` / `README_EN.txt`, then push a tag (`git tag v1.20.0 && git push --tags`). The GitHub Action builds both zips (plain and portable with Python) and creates the release.

## Disclaimer

Independent project, not affiliated with Sony or with the authors of the tools mentioned. It's a file-transfer tool: it includes no third-party content, no Sony keys or files, and no links to download sites. Use it only with content you have the right to use (your own backups and homebrew).

- It does not jailbreak the console or bypass any protection: it only copies files to a console you have already modified yourself.
- No piracy: don't use it to copy or run software you don't own or aren't licensed to use. Requests for help getting such content are not answered.
- You are responsible for how you use it. Modifying a console can void its warranty, break its terms of service or lose data: back up what matters.
- Provided "as is", without warranty of any kind. "PlayStation" and "PS5" are trademarks of Sony Interactive Entertainment, used only to describe compatibility.

---

## Español

![PS5 Directo: pegas un enlace, se envía, se descomprime y aparece en la biblioteca](docs/demo_es.gif)

Herramienta de transferencia de archivos para Windows: envía archivos a una PS5 con jailbreak por FTP **mientras se descargan**, sin pasar por el disco del PC. Después los descomprime (en la consola con BFpilot, o en el PC con 7-Zip) y coloca las aplicaciones en `/data/homebrew` para que ShadowMountPlus las muestre en el menú.

**Novedades 1.20:** pestaña Biblioteca con portadas y aviso de firmware, imágenes `.ffpkg`/`.ffpfsc` y LZ4 con [PS5 Dump Forge](https://github.com/quer3q/ps5-dump-forge), pregunta si ya está en la consola, comprobación con `.sfv`/`.md5`, "Hacer sitio", cargar ftpsrv/BFpilot desde el programa, diagnóstico de red, acceso desde el móvil con QR y programar la cola.

**Requisitos:** Windows 10/11. Descarga el `-portable-windows.zip` (trae Python, no hay que instalar nada) o el `.zip` normal con [Python 3.9+](https://www.python.org/downloads/). Para descomprimir en el PC, [7-Zip](https://www.7-zip.org/). En la PS5: servidor FTP ([ftpsrv](https://github.com/ps5-payload-dev/ftpsrv)), [BFpilot](https://github.com/ItsBlurf/BFpilot) y [ShadowMountPlus](https://github.com/drakmor/ShadowMountPlus).

**Uso:** descarga el `.zip` de la última [release](../../releases), ejecuta `Crear accesos directos.bat` y abre **PS5 Directo** desde el escritorio. La guía completa está en [LEEME.txt](LEEME.txt).

**Aviso:** proyecto independiente, sin relación con Sony ni con los autores de las herramientas citadas. No incluye contenido de terceros, claves ni archivos de Sony, ni enlaces a sitios de descargas. Úsalo solo con contenido que tengas derecho a usar (copias de seguridad propias y homebrew). No hace el jailbreak ni salta ninguna protección: solo copia archivos a una consola que ya has modificado tú. No se da ayuda para conseguir contenido sin licencia. Se ofrece tal cual, sin garantías, y lo que hagas con él es responsabilidad tuya.
