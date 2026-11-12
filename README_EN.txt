PS5 DIRECTO 2.1.8 — USER GUIDE (English)
========================================

QUICK START (5 MINUTES)
=======================
You need: a PS5 that is already jailbroken (PS5 Directo doesn't do that)
and a Windows 10/11 PC on the same network.

1. Download PS5Directo-X.Y.Z-Windows.zip from the GitHub Releases
   page (nothing else to install) and extract it, e.g. into Documents.
2. Double-click "PS5 Directo.vbs". If Windows asks whether to open a file
   downloaded from the internet, click "Open".
3. On the PS5, load your jailbreak and ShadowMountPlus as usual.
4. The wizard asks for the PS5's IP: on the PS5, Settings > Network >
   View Connection Status > "IPv4 Address".
5. It checks the FTP server and BFpilot. If one is red, click "Load" next to
   it. (With Y2JB Autoloader that doesn't work: load them from Payload
   Manager on the PS5 and click "Check again".)
6. Paste a direct download link (or drag a file onto the window) and click
   "Add to queue". When it says "Done", it's in the PS5's menu.

Something wrong? Settings > "Full check" tells you what and how to fix it.

Note: PS5 Directo is a file manager: it only moves, converts and organises
the files you choose. It doesn't include or host any content, BIOS, keys or
firmware, doesn't connect to Sony's servers and has no payload of its own.
The programs in "Discover" are downloaded from their authors' official
GitHub pages and belong to their authors. Use it with content you have the
right to use (your own backups and homebrew), at your own responsibility.

(The full guide follows; you don't need it to get started.)


Paste links (or drag files from your PC) and the tool does the rest:

  1. Downloads the files and sends them to the PS5 over FTP while they
     download (nothing is stored on your PC's disk).
  2. Joins split parts (.001, .002...) into a single file.
  3. Asks BFpilot to extract the RAR/7z/ZIP ON the console, trying the
     passwords it knows.
  4. Finds the app folder (the one with sce_sys) and puts it in
     /data/homebrew, where ShadowMountPlus picks it up automatically.
  5. Deletes the archives from the PS5.

While one transfer is extracting on the PS5, the next one in the queue is
already being sent.

TABS
  - Transfers: the queue.
  - Console files: browse the PS5 like a file explorer. You see the size
    and date of every file (folders with "calculate"), and you can create
    folders, rename, copy or move (Copy/Move, then "Paste here" in the
    destination folder), delete, download to the PC (to Downloads\PS5
    Directo), set 777 permissions, and upload files or folders as-is into
    the current folder.
    Right-click a file or the empty space for the menu; shortcuts: Del,
    F2, Ctrl+C / Ctrl+X / Ctrl+V, Ctrl+A and Backspace. "Move to…" and
    "Copy to…" open a folder picker with recent destinations. If something
    with the same name already exists when pasting, you choose replace/merge,
    keep both or skip. "Extract…" unpacks a .rar/.7z/.zip that is already on
    the console. Drives (with free space) and quick places are on the left.
    You can drag files from the PC onto the open folder.
    Without BFpilot almost everything works over FTP (copying goes through
    the PC, slower); extracting and showing free space do need BFpilot.
  - Space: a pie chart of what takes up space on the console, with each
    app's name and size. It flags duplicate content (for example a folder and
    its version converted to an image), half-sent transfers and leftovers,
    and lets you delete them from there (it asks first). Needs BFpilot.
    Anything installed from the PS5 itself is shown but deleted on the console.
  - Activity: history of finished and failed transfers.

PC FOLDERS
  With "Choose folder…" (or by dragging the folder onto the icon) a whole
  folder is sent as-is. If it has sce_sys it goes to /data/homebrew as
  TITLEID-app. If part of it is already on the console (for example, an
  upload from another program that got cut off), it skips the files that
  are already identical and resumes the ones left half-way.

ALTERNATIVE: EXTRACT ON THE PC (7-Zip)
  In "Where to extract" choose "On the PC (7-Zip)" (or make it the default
  in Settings). Then:
  - Links are downloaded to the PC first (temporary folder in Settings;
    you need room for the RAR, not for the extracted content). Your own
    local files are used as they are and are never deleted.
  - 7-Zip extracts on the fly and the app sends each loose file to the
    PS5. Nothing extracted is written to the PC.
  - Every file is checked against its CRC before it's accepted.
  - On the PS5 it only takes the final size (no RAR alongside), and the
    exact size is known before starting.
  - Trade-off: everything travels uncompressed over Wi-Fi (can be 3-4
    times more than the RAR), so sending takes longer.
  - Pause/Retry continues from the file where it stopped.
  Requires 7-Zip (free, 7-zip.org). BFpilot is not needed.


WHAT YOU NEED
-------------
This guide doesn't cover how to jailbreak: it assumes your PS5 is already
jailbroken and that you know how to load payloads (.elf).

ON THE PC
- Windows 10 or 11.
- Nothing else: the installer includes everything (Python too).
- 7-Zip (only for "Extract on the PC"): https://www.7-zip.org/

ON THE PS5 (loaded as payloads, for example with Payload Manager:
https://github.com/itsPLK/ps5-payload-manager )
- An FTP server. The reference one is ftpsrv (port 2121):
  https://github.com/ps5-payload-dev/ftpsrv
  Many payload menus already include it. If yours uses another port,
  change it in Settings.
- BFpilot (port 5905): extracts on the console, moves and deletes files.
  Get bfpilot.elf from: https://github.com/ItsBlurf/BFpilot/releases
  If it isn't loaded, the program waits until you load it. Not needed in
  "Extract on the PC" mode.
- ShadowMountPlus: makes the apps in /data/homebrew appear on the PS5
  home screen: https://github.com/drakmor/ShadowMountPlus

To check that everything works: Settings > "Full check".


ABOUT BFPILOT AND PERMISSIONS (important even if you use it on its own)
-----------------------------------------------------------------------
Up to at least version 0.4.4, when BFpilot extracts a RAR, 7z or ZIP it
leaves the files without the execute permission, and the PS5 refuses to
open the app (error CE-107750-0). It also happens when you extract from
BFpilot's own web page, without PS5 Directo.
- PS5 Directo fixes it automatically after every extraction.
- For content already on your console: Settings > "Fix permissions".
- By hand: in FileZilla, right-click the app folder > File permissions >
  777 > "Recurse into subdirectories".


FIRST TIME
----------
Open PS5Directo-X.Y.Z-Setup.exe and click "Install". No admin rights
needed. It adds:
  - A "PS5 Directo" icon on the desktop and in the Start menu.
  - "Send to > PS5 Directo" in the right-click menu of your files.
If Windows says "Windows protected your PC": "More info" > "Run anyway"
(the installer isn't signed; it's the usual warning for small free
programs).
With the .zip instead of the installer: extract it anywhere and
double-click "Crear accesos directos.bat" ("create shortcuts").
From then on, open PS5 Directo from the desktop icon: it runs in its own
window, with no tabs and no black console window.

(If something fails to start, use "Iniciar PS5 Directo.bat" ("start PS5
Directo"): same thing but with the black window, where errors are shown.
Log when running without the window: %USERPROFILE%\.ps5directo\ps5directo.log)

The language follows Windows. Switch it anytime with the "English" /
"Español" button at the top.

If you close the window with transfers running, they keep going in the
background. Open PS5 Directo again to see them. The "Quit" button closes
it completely (anything unfinished continues later with "Retry").


HOW TO USE IT
-------------
1. Extract this folder anywhere you like.
2. Open PS5 Directo from the desktop icon. A short welcome guide explains
   each part (you can see it again with the "User guide" button).
3. The first time, Settings opens: enter your PS5's IP (on the PS5:
   Settings > Network > View Connection Status), click "Save" and then
   "Test connection". Both dots at the top should turn green.
4. Paste the direct links (all parts together, in any order) and click
   "Add to queue".
   - If you know the password, type it. Otherwise it tries the ones in
     Settings and whatever is in brackets in the file name, like [WEB.COM].
   - A path to a file already on your PC also works.

5. FILES ON YOUR PC WITHOUT COPYING PATHS:
   - Drag one or more files onto the desktop icon.
   - Or right-click the files > Send to > PS5 Directo.
   If PS5 Directo is already open, they're added to its queue.
   - Or drop them on the app window: PS5 Directo finds them on your PC by
     name and size and puts their paths in the link box.


PAUSE, LIMIT AND SPACE
----------------------
- "Pause" stops sending; "Resume" continues where it left off.
- Settings > Speed limit (MB/s): to keep using the internet while it
  sends. Applies instantly. 0 = no limit.
- Settings > Always keep free (GB, 25 by default): before starting it
  checks that what's left to send fits, and while sending or extracting it
  watches the free space. If it drops below that margin, it stops
  everything before the PS5 fills up. Free up space and click Retry.
- Some file hosts limit connections: with them use just 2 per file, and
  fewer if the server starts returning errors.


UPDATES AND EXTENSION
---------------------
- "Update" button (top): checks GitHub for a new version and installs it.
  When there is one, the button turns blue and shows the version. You can
  also install from a downloaded .zip. A copy of the previous version is
  kept in the .backup folder.
- "Extension" button (top): opens the folder of the Chrome/Edge extension
  and its instructions. With it, when you click Download on a website, the
  link goes straight to PS5 Directo, no copy and paste:
    1. Open chrome://extensions (or edge://extensions).
    2. Turn on "Developer mode" (top right).
    3. "Load unpacked" and choose PS5 Directo's "extension" folder.
  Pass the captcha and click Download as usual: if it's a .rar/.7z/.zip/.pkg
  and PS5 Directo is open, the final link (after redirects) appears in the
  link box and the browser downloads nothing. When all parts are in, click
  "Add to queue". If PS5 Directo is closed, the download goes ahead
  normally. Turn it on and off from its icon.


SPEED TEST
----------
On a paused or finished transfer, "Speed test" measures (about 30 s):
download only, PC -> PS5 upload only, and both at once, and tells you
what's to blame when it's slow. There's also a PC -> PS5 only test in
Settings.


MAINTENANCE
-----------
- Settings > Full check: tests FTP, writing, BFpilot, formats, 7-Zip and
  free space, and tells you what to do if something fails.
- Settings > PS5 cleanup: finds leftovers in /data/ps5directo, shows how
  much they take and deletes them (it never touches anything outside that
  folder).
- Warns if the same Title ID already exists in /data/homebrew or on a
  USB/external drive.
- Destination: suggests /mnt/usbX/homebrew when drives are connected.
- Windows notification when a transfer finishes or fails (accept the
  permission the first time).


IF SOMETHING GOES WRONG
-----------------------
- The network or FTP drops: it retries by itself and continues where it
  left off.
- You close the program midway: when you open it again, click "Retry".
  It continues from what was already on the console.
- Wrong password: click "Retry" and type it. Nothing is downloaded again.
- "Already exists": it never overwrites what you have. Delete or rename it
  and click "Retry".
- Work files live in /data/ps5directo (outside the folders ShadowMountPlus
  scans).


LINKS
-----
Direct download links work, and so do ftp:// links to a NAS or another
server (ftp://user:password@nas/path/file.rar). Pixeldrain, Dropbox and some Mediafire and
Google Drive links are converted automatically. Pages that require a
captcha or a login don't work directly: copy the final download link, or
use the browser extension.

.pkg files go to /data/pkg, to install them with ps5upload.
Images (.exfat, .ffpkg, .ffpfsc) go to /data/homebrew.

Settings and queue are stored in  %USERPROFILE%\.ps5directo


LIBRARY
-------
The Library tab shows what's in /data/homebrew (and on USB drives) with its
cover, name, Title ID, version, folder or image (.ffpkg, .ffpfsc, .exfat)
and the firmware it needs. Set your PS5's firmware in Settings (e.g. 13.60)
and the ones that won't launch are flagged in red. It also flags duplicates,
DLC/updates and incomplete folders. Works with FTP alone; with BFpilot it
also shows how much each folder takes. Firmware comes from param.json and,
for backported titles (fakelib folder), from what the executables require.
Every finished transfer is checked too.

IMAGES AND LZ4 (PS5 DUMP FORGE)
-------------------------------
"How to leave it on the PS5" defaults to "No conversion": left as it comes
(archives are extracted; an .ffpkg, .exfat or .ffpfsc image or LZ4 content
is sent untouched). You can also pick:
  - Plain folder: always ends up as a folder; images, .fpkg and LZ4 are
    unpacked on the PC.
  - .ffpkg image: recommended by ShadowMountPlus. Full speed.
  - .exfat or .ffpfs image: ShadowMountPlus mounts them too.
  - Compressed .ffpfsc image: 30-60% smaller, but read more slowly
    (150-250 MB/s); some titles may stutter.
  - .fpkg package: installs like a store app. Firmware 11.60 or lower only
    (kstuff, fpkg-enable and ppr-patch). Left in /data/pkg. For .fpkg and
    .ffpfsc you can pick the compression.
  - LZ4 (folder or inside an .ffpkg): AMPR titles only. Experimental: some
    titles crash when saving. Non-AMPR content is sent as a folder or plain
    .ffpkg.
It's prepared on the PC with PS5 Dump Forge (github.com/quer3q/ps5-dump-forge,
downloaded automatically the first time) and sent as a single file. Needs
room on the PC for the image. .ffpkg/.ffpfsc images are not FPKG packages:
they aren't installed, ShadowMountPlus mounts them. DLC and updates are
sent as folders.

BEFORE SENDING
--------------
- If it's already on the console (same Title ID), the transfer
  stops and asks: send anyway, replace the copy there, or cancel.
- Paste the .sfv / .md5 / .sha1 with the parts (or keep it next to them on
  the PC) and every part is checked while it's sent.
- When there's no room, the error has a "Make room" button that suggests
  what to delete in the Space tab. Nothing is deleted without confirmation.

PAYLOADS, DIAGNOSTICS, PHONE AND SCHEDULE
-----------------------------------------
- Click the FTP / BFpilot dots (or Settings > Load payloads) to send ftpsrv
  or BFpilot to the PS5's ELF loader (port 9021, etaHEN or elfldr). The Y2JB
  Autoloader loader only accepts the console itself: use Payload Manager.
- "Why is it slow?" checks Wi-Fi vs cable, band and signal, 100 Mb/s links,
  VPNs, different networks and the ping to the PS5.
- The phone button shows a QR code to open PS5 Directo from your phone on
  the same network.
- "Schedule" (queue): start at a set time and sleep or shut down the PC when
  done. The PC doesn't go to sleep while transfers are running.

WHAT TO DOWNLOAD
----------------
- PS5Directo-X.Y.Z-Setup.exe: the installer. This is the one to get.
  Everything included (Python too): open it, click "Install", done.
- PS5Directo-X.Y.Z-Windows.zip: the same without installing, if you'd
  rather extract it to a folder. "Update" uses it too.
To remove it: Windows Settings > Apps > PS5 Directo > Uninstall. Your
settings are kept in case you install it again.
- PS5Directo-X.Y.Z-macOS.dmg: for Mac (Apple Silicon and Intel, macOS 11
  or later). IN TESTING: not tested on a real Mac yet. Open it and drag
  PS5 Directo into Applications. The first time macOS blocks it because
  it isn't signed by Apple: on macOS 15 or later, System Settings >
  Privacy & Security > "Open Anyway"; on older ones, right-click the app
  > Open. If something fails, "Create problem report" and attach it at
  github.com/jstrdev/ps5directo/issues.
- PS5Directo-X.Y.Z-Linux.tar.gz: for Linux (x86-64, recent distros such
  as Ubuntu 22.04 or later). Extract it anywhere and open it once with
  ./ps5directo.sh in a terminal (or right-click > Run as a Program).
  That first time it adds itself to the applications menu, so from then
  on you open it from there. Python and 7-Zip are included.
  For the window without tabs it uses Chrome, Chromium, Edge or Brave if
  you have one (otherwise your browser). Torrents need aria2:
  sudo apt install aria2. Linux has no "Send to" and no Windows firewall
  prompt.

WHAT'S NEW
----------
2.1.8 - Catch links on the phone, like the PC extension: "Share" a page
    (a post, a forum) with PS5 Directo and it takes the download links it
    has; new "🔍 Links from a page" button in Send (also on the PC);
    "Catch what you copy" in the phone app: copy a link in any app and
    when you come back to PS5 Directo it's in the box (needs the new
    phone app).
  - Pull down to refresh on touch screens.
  - Every card shows "Updated...", Discover ones too: yellow if it's from
    the last two weeks, grey if older.
  - Discover Home rows show the recently updated first.
  - PS5 Game Updater: if you don't have it, one click installs it from
    Library; if you have it and there's a new version, it tells you and
    updates it with one click (if it's in the RecompStore catalogue).
  - The header no longer splits into two rows on laptop screens.

2.1.7 - Power button at the top, next to the bell: Turn on, Rest mode,
    Restart and Turn off in one tap, with a dot that shows whether the
    console is on (green) or in rest mode (yellow).
  - No longer offers as an "update" a version with a lower number than
    yours (like EglantinePS5 1.000.000 after 1.219.029), unless its author
    published it after you installed yours.
  - Scene news: "Recently updated" also uses each project's real latest
    GitHub release date, so new arrivals no longer sink to the bottom.
  - Discover cards show when their latest version is from (in yellow if
    it's from this week).
  - PatchDL's "See in Discover" takes you straight to its card.
  - Fan control tools no longer show in the news.
  - On the phone, the sort and "Check again" share one row.

2.1.6 - "Most popular" no longer stays frozen: the news robot counts the
    GitHub downloads of the whole catalogue every day, and the most
    downloaded in the last week come first.
  - Every Discover section (Emulators, Apps...) can be sorted: recently
    updated (default), recently added, most popular, recommended order or
    A to Z. The RecompStore items in each section follow the same order.
  - When you open a section, a bubble tells you if something new arrived
    or got a new version since your last visit.
  - "Added by you" with a guide: PS5 Directo makes a quick guide from its
    README (install and usage steps, and what it needs), translated, with a
    link to read it all. Their descriptions are translated too.
  - Titles that close: besides the bell, a visible notice shows the most
    likely cause and a button to check and fix it. If the cause is an old
    ampr-emu-ng, it fixes it right away and tells you. It tells apart
    closing on startup from closing later (e.g. when loading a save), and
    also warns when a title closes by itself within seconds twice in a row
    even if ShadowMountPlus logs nothing.

2.1.5 - Turn the console on from rest mode: "☀ Turn on" button in System
    (also from the phone). The PS5 only accepts that signal from a device
    paired with it, so it has to be paired once with chiaki-ng (it only
    gives permission, you don't have to play over Remote Play). PS5 Directo
    picks up that pairing from chiaki-ng on the PC by itself and leaves it
    on the console so the phone or another PC can turn it on too. If it
    isn't paired yet, "Set up turning on" opens the step-by-step guide.
  - On the PS5, in Power Saving > Features Available in Rest Mode, "Stay
    Connected to the Internet" and "Enable Turning On PS5 from Network"
    must be on. It can't be turned on when fully powered off.
  - System shows whether the console is on or in rest mode.

2.1.4 - If something puts an old ampr-emu-ng back in a title that was up to
    date (almost always ShadowMountPlus updating the fakelib when mounting),
    the bell tells you to turn that option off, and it swaps it again.
  - Scene news: new projects not yet in Discover have "Add to Discover";
    from "Added by you" you install them and choose their menu.
  - "Recently updated" now really sorts by date, and "See all" from
    "Newest arrivals" opens the list sorted by arrival.
  - The proposals robot no longer proposes content stores.

2.1.3 - Automatic AMPR: zstd or LZ4 packed titles always carry the latest
    ampr-emu-ng. When converting on the PC, if PS5 Dump Forge adds an older
    one, PS5 Directo swaps it before sending; and every 6 hours it puts the
    latest in the console's titles that have an old one (the previous one is
    kept as .bak) and tells you in the bell. It can be turned off in
    Settings.
  - "Check startup" warns when a title has an old ampr-emu-ng, which is
    usually what makes it close on startup or when loading a save.

2.1.2 - Console power: System (and ⋯ › "Console power") has buttons to put
    the console in rest mode, restart it or turn it off, also from the
    phone. Prospero Manager does it with the console's own functions; if it
    isn't open, PS5 Directo opens it. Restarting or turning off loses the
    jailbreak until you run it again.
  - AMPR in your packed titles: zstd or LZ4 titles from PS5 Dump Forge carry
    their own ampr-emu-ng in their fakelib folder, in the version Forge had
    when converting. In System › "AMPR in your packed titles", PS5 Directo
    shows which ones have an old one and puts the latest with one click
    (checking the download and keeping the previous one as .bak).

2.1.1 - "Added by you" is shared: the list is also kept on the console
    (/data/ps5directo/anadidos.json), so what you add, move to another
    section or remove on the PC shows the same on the phone without a PC
    (and the other way round) or on another PC. The latest change wins.
  - Porpoise (GameCube and Wii) renamed its GitHub project: PS5 Directo now
    uses the new one.

2.1.0 - "Added by you" has a search box: type a name (for example "rpcs3"
    or "jellyfin") and PS5 Directo searches GitHub for matching PS5
    projects, with their stars and last update, and you add them with one
    click. Pasting the link still works.
  - What you add can also show in its Discover section (Emulators, Apps and
    media, Tools…): PS5 Directo picks the right one or you choose it, and
    you can change it any time from its card.
  - On the phone, tapping a button no longer shows the browser's blue box:
    it sinks slightly, like on the PC. And cards no longer stay "lifted"
    after you tap them.
  - "⋯ › Check for updates" always stands out in the menu, and glows when
    there's a new version.

2.0.9 - Fixed: the animated line under the selected tab (and other button
    decorations) got cut off when switching tabs. The 2.0.8 "working" spinner
    no longer shows on tabs or selectors.

2.0.8 - PS3 in Discover › Emulators: RPCS3 for PS5 (PPSA99300), the PS3
    emulator as a native PS5 title. Its licenses don't allow sharing it built,
    so the card explains how to build it on a Linux PC, where your PS3
    firmware and your copies go, and mentions the other port, KB-RPCS3.
  - Everything in English: about a hundred messages that still showed up in
    Spanish (errors, notices, tips) are now translated.
  - Buttons respond when pressed: they sink slightly, and when an action takes
    a moment the button shows it's working and can't be pressed twice by
    mistake.
  - New version notice without restarting: PS5 Directo checks every hour, so
    the "Update" notice shows up even if you leave it open.
  - What's new after updating is easier to read: each change with its title in
    bold, the latest version open and older ones folded, and long lists show
    the first 5 with "See more".
  - Scene news finds more projects: also those without GitHub topics and
    source-only ones that are already well known.

2.0.7 - The PC and the phone see the same updates: which version of each
    emulator or app you installed is also written on the console
    (/data/ps5directo/versiones.json), and "You have updates" only lists what
    is known to be old.
  - New in Discover › Emulators: PS5 Hookshot, the Switch 2 remake of Zelda:
    Ocarina of Time as a native PS5 title. It's built on your PC from your own
    copy; the card explains the steps.
  - "Added by you" no longer fails: it installs apps as .zip, .7z or .rar
    (with their PPSA… or CUSA… folder) and .elf or .bin payloads; anything
    else is saved to follow, with its guide and a bell notice for each new
    version.
  - The Discover banner refreshes itself: featured items move behind the
    latest scene news after a week and leave after two, and what's trending in
    the scene shows up there for a week ("🔥 Trending").
  - Discover can get new cards without updating PS5 Directo.
  - Android app connected to the PC: ⋯ › "📱 Use without a PC (on this phone)"
    to switch without reinstalling.
  - Scene news in the big banner no longer shows leftovers like "&nbsp;" or
    "---", and it's translated for Spanish users.
  - Android without a PC: if the PS5 is connected to the phone's own hotspot,
    "Find my PS5" finds it there too, so it downloads over mobile data and
    sends through the hotspot.

2.0.6 - Android without a PC: "Find my PS5" could miss the console; it now
    searches the phone's Wi-Fi network, and the first time it says clearly
    that it doesn't know your PS5 yet.
  - Android without a PC: the links box no longer mentions PC paths or
    torrents (without a PC only download links work).
  - Android without a PC: when a new version comes out, the app says so at the
    top with "What's new" and "Download the app"; it installs over the old one
    and keeps your settings and queue.
  - On the PC: a one-time notice that the Android app now works without a PC,
    with the link to get it.

2.0.5 - Android without a PC (beta): the first time you choose "With PS5
    Directo on my PC" (the QR, as before) or "Without a PC": the phone itself
    downloads and sends to the PS5, and the console extracts with BFpilot and
    converts with Alembic. Library, console files, Discover, payloads and the
    firmware guard work the same.
  - Android without a PC: a fixed notification with "Close" stays while it
    works; what you download from the console goes to the phone's Downloads ›
    PS5 Directo and backups to Documents › PS5 Directo.
  - Android app: if you already had it, uninstall it once before installing
    this one. It's now signed with a key that isn't in the code, so nobody can
    make a fake app that installs over the real one.
  - New in Discover › "📌 Added by you": paste the GitHub link of any app or
    payload that isn't in Discover and it installs, opens and tells you about
    new versions like the rest.
  - Use away from home: "Use on your phone" has a "🌍 Use away from home"
    section that explains how with Tailscale (free), with a second QR that
    works from anywhere without opening router ports.
  - Phone: if the phone link no longer works, a clear notice explains how to
    link it again ("Scan again" in the Android app) instead of repeating the
    welcome with empty menus.
  - The "What does PS5 Directo do?" tour no longer overflows the screen with
    large fonts.
  - New in Discover › Improve your titles: CheatRunner, performance patches
    while you play (60 fps with fixed timing, higher resolution, 120 Hz);
    installed by hand following its guide.

2.0.4 - Updating big emulators (RetroArch) no longer gets stuck on "Putting it
    in place", and fixing permissions on thousands of files is several times
    faster.
  - Discover's Home shows the scene news in a big auto-rotating banner, with
    featured picks editable on GitHub.
  - The banner fills itself with the latest from the scene: new versions of
    everything in Discover and new PS5 projects on GitHub, newest first,
    generated every 3 hours by an automatic task in the PS5 Directo repo
    (escena.json).
  - From the RecompStore catalog the banner only shows just-arrived items and
    updates from the last 3 days; the full news list can be sorted (recently
    updated, newest arrivals, most popular, A to Z).
  - New banner look: thumbnails on the right with a progress bar, arrows
    together at the bottom and a small line with titles seen running.
  - New: PS5 Lichii (Switch 2, PS5 and PS5 Pro; best on Pro).
  - Convert on the PS5 (experimental): in "Format on the PS5" choose where
    it's converted: "On your PC" (latest PS5 Dump Forge) or "On the PS5"
    (Alembic), which downloads and sends at the same time and the console
    converts it at the end.
  - Network shares (SMB: Windows, Samba and most NAS): paste
    \\NAS\Folder\title or smb://NAS/Folder/title and it goes straight to the
    PS5.
  - New in Discover: Prospero Manager (all-in-one manager running on the
    console, port 7070).
  - When a new PS5 Directo version is out, "⋯" shows a blue dot and "Update to
    vX" glows.
  - New users see the feature cards right after connecting the PS5, with the
    step-by-step guide at the end; if you skip them, a "See what PS5 Directo
    can do" bubble next to PC mode reminds you until you watch them or close
    it.
  - Payloads: "Install on the console" fetches the new version from GitHub
    into Payload Manager.
  - A 12-step tour of what PS5 Directo does (formats, PC or PS5 conversion,
    your way / PC mode, file manager, Discover with recomps and more),
    available any time from ⋯ › "What does PS5 Directo do?".
  - Discover clears the update notice after updating, and has a ✕ to skip a
    version.
  - Payload updates are also checked on GitHub, with a small notice and
    "What's new".
  - Dump Forge notes are translated.
  - Convert what's already on your PC without sending it ("Only to my PC" +
    "Convert").
  - Extraction goes straight to the USB/external drive when that's the
    destination.
  - M.2 / extended storage titles show their real path.
  - A warning when ShadowMountPlus isn't on the console.
  - Mac: the app no longer modifies itself and the guide explains the "is
    damaged" message.


2.0.3 - Titles installed on the M.2 SSD / extended storage now show their real
  path. "Reinstall from GitHub" in Update. Emulators with several builds (e.g.
  EglantinePS5): pick which one to download, add another GitHub build or install
  your own .zip. New-arrivals notice in Discover. Library export (CSV or web
  page) and a type row that always shows every category. A notice on the PS5
  when a transfer ends (with zftpd). New in Discover: DualSense Overlay, Retro
  Papa and the native DOOM port. "What's new" before updating any emulator,
  app or payload: the release notes of every version you're missing,
  translated, with the original one click away. App updates: a strip under
  the header with "What's new" and "Update and restart", a clearer update
  window, a progress bar and downloads that resume if cut. Discover › Remote
  Play › "Play on PC" opens chiaki-ng straight on your PS5 (PS5 Directo
  streams nothing itself). Your PC collection warns when a folder couldn't be
  read. Downloads from hosts like Akirabox, 1fichier or Rapidgator are no longer stuck at 2 connections: they add more while the server copes, like a download manager. New in Discover: Alembic (on-console format converter, experimental). New versions of PS5 Directo download in the background ("Restart now" when ready). The What's new window keeps Close and OK always visible. "See them" in the scene arrivals notice now takes you to "Newest arrivals". A one-time notice reminds you that titles can be converted to LZ4 or zstd to take up much less space. Library: zstd-packed titles show as "zstd" (they said LZ4). Fixed: Library's 🔔 Updates button opened the wrong window. Sliding
  "glass" tabs, dialogs that ease in, no white focus frame, dark scrollbars.
2.0.1 - New icons across the app: line icons in the same style as the format
  cards instead of emojis. Discover shows each project's real artwork (its
  official icon and image, or RecompStore's), cached on your PC. Library: a
  type row (All · Titles · Emulators · Media · Tools), a single "Discover and
  add more to your library" shortcut, uniform cards with the same buttons in
  the same place, official art when the console has no cover, and dismissable
  tips. PC on the PS5 (was "Linux and Steam"): step-by-step guides in your
  language for Linux (Ubuntu), Bazzite Deck and MihawkOS that check your
  firmware and running payloads, download the image to your PC with one click
  and send the loader. Step-by-step Remote Play setup (chiaki-ng + PIN). Scene news syncs the whole PS5 catalog with full-size
  art. Errors in plain words, single links checked before
  queueing, dates in the app's language. Fixed: Activity and Backup on phones,
  half-translated messages, a lone tile in System.
2.0.0 - Firmware guard (System): checks every 10 minutes whether the console
  has downloaded a system update and warns you (also on Telegram or Discord);
  remove it with one click or automatically, and optionally block the update
  servers on the console. Payloads up to date (System): compares what you have
  in Payload Manager with its catalog, warns about new versions and updates them
  with one click. Tidy up names (Library): clear names for your images and .pkg
  files ("Title-PPSA01234-EU") from the title inside them; folders and patches
  are never touched, the ShadowMountPlus limit is respected, and it can be
  undone. YouTube without updates (for Y2JB users), with a
  backup and "Undo". Scene news (Library): the latest from the RecompStore
  catalog, read only. Beyond the console (Recommended): Linux (Ubuntu) and
  Bazzite Deck (Steam) guides with the loader, and MihawkOS (your Windows
  programs and Steam on the PS5 itself, experimental). If PS5 Directo detects a bug of
  its own, it offers a pre-filled GitHub report. If something closes on start
  on the console, it reads ShadowMountPlus's log, checks it and tells you the
  likely cause. Title page: "About this title" (Wikipedia summary, trailer, reviews) and
  an animated cover on hover. New "✨ Discover" tab: Scene news up top and 30+
  emulators, apps and tools by category, with covers; Library gets Recommended
  and What's new shortcuts plus an "Emulators" / "Apps and tools" filter. A new
  interactive tour of everything 2.0 does, the first time you open it. Phone:
  the ⋯ menu no longer goes off screen and the install-app notice stays until
  you press "No, thanks". Smoother tab transitions and a livelier background.
2.0.0 - iPhone and iPad: "Open on phone" › "Got an iPhone or iPad?" explains
  how to keep it as a Home Screen icon (already connected) and how to create a
  shortcut for "Share › Send to PS5 Directo" from any app. Mac: finds
  Homebrew tools (aria2, FFmpeg, rclone), the console check works, the picker
  comes to the front, clicking the icon again brings the window back, shortcuts
  show ⌘. Linux: https downloads work even if the distribution keeps its
  certificates elsewhere. Loading animation with triangle, circle, cross and
  square. Tools: SMPlusGui and ps5-tailscale (experimental). ❤ Credits in the
  ⋯ menu.
2.0.0 - PC mode (switch at the top): everything you add, also from the
        extension and the phone, stays on your PC: download only,
        download and extract, or download, extract and convert. Nothing
        goes to the console.
      - When downloading to the PC, several parts at a time (Settings ›
        "Parts at once"), with the same total connections.
      - "Catch the download links you copy" (under the link box): file
        links you copy in any website or program land in the box.
      - When done, "Open the folder" (on your PC) or "See on the console"
        buttons, and finished items are more compact in the queue.
      - PS5 Dump Forge: a notice when a new version is out, with what's
        new; you update when you want (or automatically, in Settings).
      - zstd format (PS5 Dump Forge 0.5), as a folder or in an .ffpkg
        image, and compression level for LZ4 and zstd. "Launch check"
        knows zstd and warns about ShadowMountPlus's fakelib update.
      - Library: Permissions, Saves and Delete are in each card's "⋯"
        menu.
      - Queue: ▲ ▼ to change the order and "⚡ Send now" (jumps ahead; what
        was being sent pauses and carries on by itself when it's done).
      - Backup: also the saves of the PS1 (PSSwanStation), PS2 (Crux2,
        PS5SX2) and Switch (ProsperoEden) emulators, restored separately.
        The daily backup waits until you close what's open on the console.
      - Payloads: "PS5 Dump Forge" can be started on the console (converts
        and installs .pkg from its web page, port 8095).
      - Library › ⭐ Recommended: emulators (PSSwanStation, Crux2,
        ProsperoEden) installed by themselves from their author's GitHub,
        and tools loaded as a payload (PatchDL to update without
        installing anything, PS5 Dump Forge), with what each does and
        what you need.
      - Library: "📂 Folder" button on each card (and 📂 on your PC's).
      - If 7-Zip fails extracting on the PC, it says why and what to do,
        and if it's 7-Zip's fault it tries without it (Python or PS5 Dump
        Forge).
      - Watched folder: whatever finishes downloading there with another
        program lands in the link box by itself.
      - Console trash (Space › Trash): "Delete" in the Library and in Console
        files, and the old copy when replacing, are moved aside and can be
        recovered for a few days (deleted by themselves after 7 days).
      - Checksums (.sfv, .md5) are also checked on resumed parts, by
        reading the whole file again.
      - Time left for the whole queue, next to "Queue".
      - "Only to my PC" › Download and extract: "Choose what to extract"
        (see what's inside and extract only part of it).
      - Speed test › "Tune connections with this link": tries 2, 4, 8 and
        16 connections and keeps the best.
      - Mac: the app is now native on Apple Silicon (no Rosetta) and
        macOS asks for the Local Network permission as PS5 Directo.
      - Extension 1.3.0: in PC mode, what you send with right-click goes
        to your PC; Alt + click on a link lets the browser download it (no
        capture); rules (don't capture under X MB, never or always on a
        site); picks up the password written next to the link; in the
        popup, "Send now", time left for the queue and the PC mode switch;
        the "done" notification has "Open the folder".
      - Android app: "Share" a link from any app sends it to PS5 Directo,
        and "Wake up my PC" (Wake-on-LAN) if the PC is asleep.
      - Library: "🩺 Check everything" (checks it all at once), details of
        each title when you click its cover (where it is, use, backups,
        notes), "Archive on PC" (copies, checks and removes it from the
        console) and the "Not opened in 2 months" filter.
      - Recommended: Remote Play from the PC (payload to get the pairing
        PIN, and chiaki-ng).
      - Recommended: PS5 Hardware Overlay (FPS, temperatures, clocks,
        RAM, fan and power draw on top of the picture, like MangoHud) and
        zftpd.
      - New System tab: the console's CPU temperature (with a graph),
        memory, disk and uptime (with zftpd), and access to the overlay.
      - "The PS5 downloads it (zftpd)" destination: the console downloads
        the link without going through the PC (http, https, ftp and
        magnet); .zip files are extracted to the destination.
      - ☁️ My storage (rclone): Google Drive, OneDrive, Dropbox, MEGA,
        pCloud, Box, WebDAV and SFTP. Browse, tick and download to your PC
        or send it to the console like any link.
      - Library › 🔔 Updates: the version you have of each title against
        the latest known by prosperopatches.com (PS5) and orbispatches.com
        (PS4), with the one that fits your firmware. Only the version
        number is checked; to install, PatchDL.
      - Emulators in Recommended: a notice and "Update" button when a new
        version is out (copied over, keeping settings, saves, BIOS and
        content), "Add your BIOS", "Send content" to its folder, view and edit
        its settings and view its logs, and a quick guide for each one.
      - Captures: a checkbox so new screenshots and videos download to
        the PC by themselves, one folder per title, when the console is on.
      - Archive on PC: a list of what you archived with its SHA-256
        checksum and a "Check" button to know if it's still intact (On your
        PC › 🗄 Archived).
      - When resuming a download, if the file on the server has changed
        (different ETag, date or size), it starts over instead of mixing
        two versions.
      - Phone: a list of the devices that have connected; "Change code"
        locks out everyone who had it.
      - Recommended, many more emulators (each from its author's page; none
        includes BIOS, keys, firmware or content): PS5SX2 and PSXS5
        (PlayStation), PSP5 (PSP), Porpoise (GameCube and Wii), Mupen64Plus
        (N64), Snes9x (SNES), EglantinePS5 (Switch 2, experimental), PS5-ECAM
        (Wii U, 3DS, DS and Switch), PSFlycast (Dreamcast), Genesis Plus GX
        (Sega), FBNeo (arcade), Beetle PCE (PC Engine), PS5X360 (Xbox 360),
        XPSemu (Xbox), RetroArch (many systems) and Ruffle (Flash), grouped
        by system, with search and an "Experimental" tag where it applies.
      - 🧭 First steps: the first time you open an emulator it guides you
        step by step (installing it, your BIOS or keys, your content and
        opening it), ticking what's already done.
      - Media in Recommended: VLC (with "Send videos and music"), Jelly5
        and Emby5; and new tools: PShare, Chiral-FTP, ps5-exporter and PKG
        Manager.
      - Backup: also the saves of PSXS5, Porpoise, Mupen64Plus, Snes9x,
        Genesis Plus GX, FBNeo, Beetle PCE, PSFlycast, RetroArch and Ruffle.
      - System with ps5-exporter: fan, power, FPS, clocks, the console's
        hours of use, "Now on the console" (also at the top, next to the
        firmware) and which titles heat it up most.
      - Captures: new ones are posted by themselves to your Telegram group
        or channel (your bot) or Discord (a webhook), with a different
        destination per title, history and "Retry"; and "📤 Send" for the
        ones you tick.
      - "Install on the console" for .pkg files (Library and Console files)
        and "▶ Open on the console" for installed titles, with zftpd.
      - Conversions: "⏸ Pause here" (frozen, using no processor or disk)
        and "🐢 Use less of the PC" (low priority; also in Settings).
      - Interface: a "⋯" menu at the top (language, guide, "Go to…",
        updates, what's new, quit), "Go to…" with Ctrl+K, finished items in
        one line and in their own section, long hints folded, each title's
        details with its background art, "What's new" for every version you
        skipped, and less animation if the system asks for it.

1.27.0 - Android app (PS5Directo-X-Android.apk on GitHub › Releases): opens
        PS5 Directo from your PC in its own window, with an icon in your
        apps. The first time, scan the "Phone" QR from the app.
      - Extension 1.2.2 with guided install: "🧩 Extension" button at the
        top opens the extensions page, copies the path and shows when it
        connects.
      - Fixed: sending to the console from "On your PC" or "All" with "Only
        to my PC" selected said "That's already on your PC".
      - Fixed: Captures › "Download to PC" saves to Downloads › PS5 Directo
        › Capturas, with its progress in the same tab.

1.26.0 - "🔎 Find my PS5": finds the console on your network (in Settings,
        next to the IP, and in the "I can't find the PS5" notice).
      - Library › All: each title once, whether it's on the console, on
        your PC or both, with its copies, updates and DLC together, and
        "Send to the console" or "Download to PC" depending on what's
        missing.
      - Library › On your PC: "Send everything missing" queues everything on
        your PC that isn't on the console.
      - "Opened … ago" and time used on each Library card (from the
        ShadowMountPlus log, since PS5 Directo started watching), and a
        "Recently opened" sort.
      - "Saves" button on each title: copies only its saves to the PC
        (restored from Backup).
      - Low-space notice for the console (under 20 GB or 10 %), with "Make
        room".
      - At the bottom of the page: "Report a problem", the guide and GitHub,
        always visible.

1.25.2 - The browser extension updates itself: PS5 Directo keeps it in a
        fixed folder (in your user folder, .ps5directo\extension) that
        doesn't change when you update or move it, and the extension
        reloads itself when there's a new version. Load it from that
        folder once (Settings → "Open the extension folder"). If the browser
        has it loaded from another folder, the extension and PS5 Directo
        say so.

1.25.1 - "Where does it go?" and "What should I do on your PC?" are now
        cards with icons, like the format ones. Choosing "convert" under
        "Only to my PC" opens the formats to pick one.

1.25.0 - Bug report with a form (Settings → "Create bug report", or "Save
        report" in a transfer's diagnosis): pick what you were doing, say
        what went wrong and the steps, and it creates the .zip with that
        inside. Then "Open issue on GitHub" opens it pre-filled (just drag
        the .zip in), or "Copy text" for the forum or Telegram.
      - If a transfer fails because the console was lost (rest mode,
        restart, payload crashed), it resumes by itself when the console
        answers again, up to 5 times. The card says so.
      - If you paste or drop a payload (.elf), it warns that copying it
        doesn't start it and offers to start it on the console with
        Payload Manager.

1.24.0 - Browser extension (version 1.2): right-click a link or selected text
        → "Send to PS5 Directo", to the link box or straight to the queue (to
        the PS5 or only to your PC). The icon shows the queue progress and
        notifies you when something finishes or fails, and its popup has a
        mini queue with pause, resume and cancel plus the FTP and BFpilot
        dots. If you already had it: click ↻ on it in chrome://extensions.
      - Phone: in Captures, "📲 Save to phone" saves screenshots and clips
        to the phone (WEBM clips are converted to MP4 on the PC first), to
        share them from the gallery. Tabs at the bottom, easier to tap, and
        captures two per row. It can be added to the home screen as an app
        and opens without scanning the QR again.

1.23.3 - A big notice at the top when FTP or BFpilot is missing, instead of
        just red dots: it says what's wrong and what to do. It tells apart
        a console that's off or on another IP, one that's on but without
        the jailbreak (after a restart), Payload Manager not open, or a
        payload to install or restart, with a button to fix it.
      - If Payload Manager is opened later (for example after restarting
        the console and jailbreaking again), PS5 Directo starts ftpsrv and
        BFpilot by itself without having to reopen it.

1.23.2 - The "Several links at once" button is gone: what it did is now in
        the usual box. Paste the links one per line or inside any text and,
        when you click "Add to queue", it checks them: if a link doesn't
        work or a part is missing, it tells you and you choose to add only
        the good ones, everything anyway or cancel. If all is fine it's
        added as always, with no extra steps.
      - Launch check: flags in red when a title is in LZ4 but one of its
        executables doesn't use AMPR (happens with collections): with LZ4
        it closes when opened. It also reads the crashes ShadowMountPlus
        logs (SIGBUS, "general protection fault") and explains the cause.

1.23.1 - New "Where does it go?" switch in Transfers: "To the PS5" (as
        always) or "Only to my PC". With "Only to my PC" you choose what to
        do: just download the files, download and extract (joins the parts
        and uses the password), or download, extract and convert to the
        format you pick (.exfat, .ffpkg, LZ4…). Nothing is sent to the
        console.
      - "Review links" is now called "Several links at once", which is
        clearer: paste many links at once and it splits them into packages.

1.23.0 - "📦 Review links" (in Transfers, next to "Add to queue"): paste a
        list or any text with links (a message, a copied page…). It pulls
        out every link, checks they're online and groups them into
        packages, all the parts of the same thing together. It flags dead
        links and missing parts, and you choose which packages to queue.
      - New "Download to PC only (not sent)" option in "Where to extract":
        it downloads everything, joins the parts and extracts it into a PC
        folder (Downloads > PS5 Directo > Biblioteca by default, can be
        changed), without touching the console. When it's done it deletes
        the downloaded parts and it shows up in Library > On your PC.

1.22.1 - Mac: the app asks for macOS "Local Network" permission. Without
        it, it can't talk to the PS5 even when it's on; now, if macOS
        blocks it, it says so and explains where to turn it on (System
        Settings > Privacy & Security > Local Network).

1.22.0 - Captures: "Convert to MP4" button. Tick the videos and they're
        saved as MP4 (H.264) in Downloads > PS5 Directo > Capturas, ready
        for Telegram, WhatsApp or any website. If they were recorded in
        HDR, they get normal colours so they don't look washed out. Uses
        FFmpeg: on Windows it's installed the first time only (33 MB, it
        asks first); on Linux and Mac it uses the system one.
      - Space: new "Clean up leftovers" section. Finds half-finished
        sends and downloads, cut-off extractions and temporary folders on
        the console and on the PC, with the size of each. Anything a
        queued transfer still uses is locked and doubtful folders come
        unticked. Nothing is deleted without confirmation.

1.21.2 - Library "Download to PC": the bar at the top now shows the speed
        and the time left.
      - The cover of the title being downloaded turns dim and fills with
        colour from bottom to top, 0 to 100 %, with the percentage.
      - Each Library cover has a small chip with its size.

1.21.1 - Library: "Download to PC" button on each title (folder or image),
        with its progress right there. It goes to the PS5 Directo folder
        on the PC (by default Downloads > PS5 Directo > Biblioteca, you
        can change it in "On your PC") and shows up in "On your PC" by
        itself, ready to send again.
      - Automatic payloads with Payload Manager: if it's open on the
        console, ftpsrv and BFpilot start by themselves when PS5 Directo
        opens if they're missing (can be turned off). The "Console status"
        window (click the FTP or BFpilot dots at the top) says what's
        missing, why and what you can't use, and fixes it with one click:
        Start, Install and start (if Payload Manager doesn't have them) or
        Restart (if one hung or was loaded twice). It can also add them to
        Payload Manager's autoload so they load when the console starts.
        It never loads the same one twice.
      - Asking for LZ4 from an image (.exfat...) whose title doesn't use
        AMPR said "as a plain folder" although the image is sent as is.
        Now it says so, and the queue step shows "No conversion".
      - Mac: if the app didn't open, it said nothing. It now removes the
        "downloaded from the internet" flag that can block the Python it
        includes, checks that it starts and, if something fails, shows a
        message with the error and what to do.
      - The program always opens on "Transfers" (it used to go back to
        the last tab you used, which could be confusing).
      - With no connection to the console, the tabs say clearly what's
        going on and what to check, instead of a Windows error such as
        "WinError 10054".
      - Captures: tips so your clips are recorded as MP4 (they play right
        inside Telegram, WhatsApp and websites) and so HDR recordings don't
        look washed out when shared (turn off HDR in the PS5's capture
        settings). They can be dismissed with "Got it".

1.21.0 - Mac version IN TESTING (PS5Directo-1.21.0-macOS.dmg), for Apple
        Silicon and Intel, with Python and 7-Zip included. Not tested on
        a real Mac yet: any report on GitHub is welcome.
      - The problem report now includes the program's technical log
        (without the session access code).
      - Fixed: if you closed the program on the Library tab, on reopening
        it showed "Transfers" selected with an empty Library below.
      - Linux version (PS5Directo-1.21.0-Linux.tar.gz), with Python and
        7-Zip included. It adds itself to the applications menu and
        updates itself like on Windows.
      - Downloads to the PC go to your real Downloads folder (on Linux it
        may have another name).

1.20.4 - Its own taskbar icon: the window no longer hides inside the Edge
        or Chrome icon, and pinning it pins PS5 Directo.
      - If it's already open and you open it again, it brings the existing
        window to the front instead of opening another one.
      - If you pinned it before this version, unpin the old icon and pin
        the new one (right-click the taskbar icon > Pin).
      - Captures: screenshots taken with HDR on (.jxr) now show up,
        previewed as JPG (downloading keeps the original). Each one shows
        the app and the date, videos show the app's cover, and the
        temporary copies the console makes when sharing are hidden.
      - Fixed: in "Console files", folder size, deleting, copying, fixing
        permissions and "Replace" on an existing folder failed since 1.20.3.

1.20.3 - Windows installer (PS5Directo-1.20.3-Setup.exe): double-click,
        "Install", done, everything included. The .zip is still there if
        you'd rather not install.
      - Library: "Check" on each app, for when it won't open or closes on
        launch. It checks the folder (executable, param.json, permissions,
        firmware, backports and LZ4 packing), shows logs (.log) and captures
        from the last minutes, explains the error code shown on the TV and
        saves a .zip report. Read-only: it changes nothing.
      - Welcome window on first launch: sums up what PS5 Directo is and does.
      - Library: also what's installed on the console (PS4 and PS5), with
        cover, version and firmware, and a filter for it.
      - Library, "On your PC": pick PC or NAS folders, see what's already on
        the console and what isn't, and send it with one click.
      - Library: "See updates" opens each title's public official update list
        in your browser (Prospero Patches). Warns that updates needing
        firmware 14.00 or higher can't be installed. The program downloads
        nothing.
      - Library: if PS5 Game Updater is on the console, a button opens it; if
        its folder is misplaced, it detects and fixes it.
      - Library: images and folders without param.json show name, cover and
        firmware.
      - New "Backup" tab: saves saved data, ShadowMountPlus and payload
        settings and the app database to the PC, manually or daily. Saved data
        can be restored to the same console (current data is backed up first).
      - New "Captures" tab: the console's screenshots and videos with
        thumbnails, to view and download to the PC.
      - "Diagnosis" on failed transfers: what happened, codes and how to fix
        it, plus a .zip report (no passwords) to send to whoever helps you. In
        Settings, "Create problem report".
      - Torrents: paste a magnet or a .torrent. It's downloaded to the PC
        first with aria2 (fetched automatically) and then sent, extracted or
        converted. No seeding afterwards; deleted from the PC.
      - PS5 Dump Forge updates itself (at most daily), checking first the new
        version works; otherwise it keeps the previous one.
      - New PS5 Directo versions are announced by themselves in a window with
        what's new.
      - Warns if the speed to the PS5 suddenly drops (usually the console
        idling: press a controller button).
      - "Format on the PS5" starts folded and says how long it takes: no
        conversion downloads and sends at once; converting takes 3 steps.
        Cards sorted by most used and "Which one?" by need.
      - Your PS5 firmware next to the title (saves automatically) and the
        .fpkg card tells you if it works.
      - In the queue, "Convert to …" names the format, and it warns if
        something couldn't be converted.
      - Fixed: an archive with a normal version plus a backport wasn't
        converted; now it picks the right one.
      - Fixed: converted files got mixed into an older folder with the same
        name.
      - Fixed: many Library folders showed no name.
1.20.1 - If it can't connect and the PC and PS5 are on different networks,
        it says so and explains how to fix it.
      - Clearer "Use on your phone" and "Browser extension" buttons, also
        offered at the end of the first-steps wizard.
      - "Download again" in the queue and in Activity: starts over without
        pasting the link again. If the target already exists when placing,
        it asks (keep both, replace or cancel) instead of failing.
      - Fixed: archives with the normal app plus a "backport" folder no
        longer fail with "Already exists"; half-sent leftovers are
        replaced.
      - Updated "User guide": explains the format converter, every tab,
        phone access and the browser extension.
      - The "Which one?" window adds estimated speeds where there was no
        data, with clearer notes on LZ4 and .ffpfsc.
1.20.0 - First-steps wizard (IP, connection check and loading what's
        missing, firmware) and a shorter, simpler guide.
      - New Library tab: what's on the console with cover, version, format
        and required firmware, flagging what won't launch on yours.
      - .ffpkg / .ffpfsc images and LZ4 with PS5 Dump Forge (optional):
        prepared on the PC and sent as a single file. Picked with cards
        under the destination, each explained, with the room needed on the
        PC; the first time it warns that converting takes PC space until
        it's done. A download that's already an image is sent as is or
        converted to the chosen format, and placed even without BFpilot.
      - New formats: plain folder, .exfat, .ffpfs and .fpkg (with a
        compression choice). Without 7-Zip, PS5 Dump Forge extracts it.
      - Formats are grouped (space savers first) and "Which one?" compares
        the pros and cons of each.
      - Time left (and rough finish time) when sending, extracting and
        converting. On the phone, a big alert with vibration and a beep
        when something finishes or fails (with the page open).
      - Library: LZ4-packed folders are tagged LZ4 (plus an LZ4 filter).
      - Fixed: resuming a transfer asked "Already on the console" about
        its own half-sent copy.
      - Fixed: the phone page was blank in some Android Chrome versions.
      - Open on phone: the page is sent compressed and the dialog shows
        "What arrives from the phone" to tell whether it gets there.
      - Firmware warning (set yours in Settings), backport-aware.
      - Asks before sending something that's already on the console.
      - .sfv / .md5 / .sha1 checks while sending.
      - "Make room": suggests what to delete when space runs out.
      - Load ftpsrv and BFpilot on the PS5 from the app (port 9021).
      - "Why is it slow?" network diagnostics.
      - Open from your phone with a QR code.
      - Schedule the queue, sleep or shut down when done, no sleep while
        sending.
      - Portable version with Python included.
      - Fixed: with FTP only (no BFpilot), ftpsrv returned the console's
        root when listing a folder; it affected the file browser and
        folder uploads.
      - Phone access: a button to allow it in Windows Firewall if the page
        stays blank.
1.19.2 - Fixed "504 Option not supported" in Console files when BFpilot is
        not loaded: the file browser now lists folders with ftpsrv alone
        (and with FTP servers that have no MLSD).
1.19.1 - Fixed: the queue stopped updating by itself (since 1.18.3), so
        "Remove" seemed to do nothing and then said "no existe". The file
        browser now relists the open folder when an upload to it finishes.
        Fixed "Download to PC" for apps: ftpsrv decrypted eboot.bin/.prx/
        .sprx on the fly, so downloads came out incomplete. Files are now
        transferred exactly as stored (this also makes size comparisons
        when resuming folders correct). Thanks to RichyMX for finding it.
1.19  - Renewed file browser: right-click menu, keyboard shortcuts,
        clickable path, drives and places on the left, "Move to…" / "Copy
        to…" with recent destinations, a choice when pasting over existing
        items (replace, merge, keep both or skip), "Extract…" for archives
        already on the console, and dragging files from the PC onto the
        open folder.
      - The file browser no longer needs BFpilot for most things: rename,
        move, delete, new folder, permissions, sizes and copy work over FTP.
      - Notification bell at the top: finished or failed transfers, file
        operations, console disconnections and new versions.
1.18.4 - Fixed: leaving the window minimized or in the background for a while
        made the program close itself, showing "Failed to fetch" on return.
        Now it only closes when you actually close the window.
1.18.3 - Fixed "Failed to fetch" and the queue appearing and disappearing: on
        Windows two PS5 Directo could end up listening on the same port (e.g.
        when opened twice in a row). Now only one can be open. Brief
        connection hiccups are retried automatically.
1.18.2 - Creates a "PS5 Directo" shortcut on the desktop (and in "Send to")
        with the program's icon. If you delete it, it won't come back; if it
        pointed to an old copy, it's fixed. The taskbar shows the new icon.
        If an old version was still open (e.g. an old copy in another folder),
        it's closed automatically instead of being shown.
1.18.1 - When deleting something big you see how much has been freed and how
        long it has taken, and the browser's free space updates meanwhile.
1.18  - "Space" tab: a pie chart and list of everything that takes up space
        on the console, by app name and size. It flags duplicate content (for
        example a folder and its converted version), half-sent transfers and
        leftovers, and deletes them from there. USB/external drives too.
      - Faster downloads: each connection is reused for many chunks in a row,
        like download managers do.
      - The extension passes that host's cookies, browser and source page to
        PS5 Directo: same speed and access as in the browser (logged-in or
        premium) and fewer 403 errors. Cookies never leave your PC and are
        never saved to disk. After updating, reload the extension in
        chrome://extensions and accept the new permission.
      - Folders: checks that what's left to send fits on the PS5, and the
        speed no longer shows false jumps when resuming a half-sent file.
      - The PS5 IP is cleaned up if typed with spaces, ftp://, a port or
        commas (it used to fail with "getaddrinfo failed").
      - After updating, a window shows what's new.
      - Better layout on narrow windows and small fixes.
1.17  ftp:// links (a NAS or other server) with resume. .tar/.tar.gz/.tar.bz2/
      .tar.xz extracted on the PC (no 7-Zip needed). Folders upload over several
      connections at once. Own steps for folder uploads.
1.16  "Console files" tab (file browser: sizes, copy, move, rename, delete,
      download to PC, permissions, upload here). Send PC folders with
      resume. "Choose files…/Choose folder…" buttons. "Activity" tab.
1.15  English interface (auto-detected; English/Español button). The
      browser extension also follows the browser's language.
1.14  Welcome guide on first launch ("Guide" button to see it again).
      Updates are checked on GitHub automatically.
1.13  New icon and background.
1.12  "Update" and "Extension" buttons on the main screen. Asks for the
      PS5's IP on first launch.
1.11  IMPORTANT FIX: content extracted by BFpilot didn't launch
      (CE-107750-0) because of missing execute permissions. PS5 Directo now
      sets 777 when placing it. New Settings > "Fix permissions".
1.10  New option: extract on the PC with 7-Zip and send the content already
      extracted, verified file by file.


DISCLAIMER
----------
PS5 Directo is an independent program, not affiliated with Sony or with
the authors of BFpilot, ftpsrv or ShadowMountPlus. It's a file-transfer
tool: it includes no third-party content, no Sony keys or files, and no
links to download sites. Use it only with content you have the right to
use (your own backups and homebrew). Everyone is responsible for what they
transfer.
