PS5 DIRECTO 1.20.0 — USER GUIDE (English)
=========================================

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

Note: PS5 Directo only copies files. Use it with content you have the
right to use (your own backups and homebrew).

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

WHAT'S NEW
----------
1.20.4 - Its own taskbar icon: the window no longer hides inside the Edge
        or Chrome icon, and pinning it pins PS5 Directo.
      - If it's already open and you open it again, it brings the existing
        window to the front instead of opening another one.
      - If you pinned it before this version, unpin the old icon and pin
        the new one (right-click the taskbar icon > Pin).

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
