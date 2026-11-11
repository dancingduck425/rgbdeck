# Capturing the Dark Flash V92 screen's USB traffic

RGB Deck can only drive the V92 once we know what the vendor app (DF Space Station) sends to it. This page is
for whoever has the screen: about 20 minutes on the Windows PC it is plugged into. Nothing here changes the
screen or the PC; it only records what goes over the cable to that one device.

## What to send back

1. `v92.pcapng`: the capture made below.
2. A note with the **time of each step** (or just the order, if you leave about 10 seconds between steps).
3. The six images in this folder as you used them (they are the same files, so "used all six" is enough).
4. One theme folder from DF Space Station (a `.mPanelTheme` folder), zipped.
5. The screen's resolution if the box, manual or the app states it.
6. RGB Deck's log: `%APPDATA%\rgbdeck\rgbdeck.log` (paste that path into Explorer's address bar).

Zip it all and send it through anything that takes a file of a few hundred MB (Google Drive, Discord, ...).

## Set up (once)

1. Download the six `.png` files in this folder (open each on GitHub, **Download raw file**).
2. Install [Wireshark](https://www.wireshark.org/download.html). In its installer, **tick USBPcap** when it is
   offered, and restart Windows afterwards.
3. Close RGB Deck (tray icon, Quit). Close DF Space Station too, completely: also from the tray.

## Capture

**Privacy first:** a USB capture can contain everything typed on a USB keyboard. Step 2 limits the capture to the
screen. Even so, do not type passwords while it is running.

1. Start **Wireshark as administrator** (right-click, *Run as administrator*).
2. In the list of interfaces there are one or more named **USBPcap1, USBPcap2, ...**. Click the small **gear**
   icon left of each one until you find the one whose device list contains **HOTSPOTEKUSB HID DEMO**
   (it may only show as *USB Input Device* under a hub; unplugging nothing, look for the entry with
   VID 1800 if IDs are shown). In that dialog:
   - untick **Capture from all devices connected**,
   - tick **only** the screen's entry,
   - press **Start**.
3. Now do these steps, **about 10 seconds apart**, and write down the order:
   1. Start **DF Space Station** and wait until the screen shows its theme (this records how the app
      greets the screen).
   2. Make a theme with **no widgets at all** (no clock, no stats, no text) and the background `1-red.png`.
      Apply it.
   3. Same with `2-green.png`, then `3-blue.png`, `4-white.png`, `5-black.png`, `6-pattern.png`.
      A widget-free theme matters: a ticking clock makes every picture different and much harder to decode.
   4. With `6-pattern.png` still showing: set **brightness** to the lowest value, then the highest, then a
      middle one.
   5. If the app has a **rotation / orientation** setting: change it once, and back.
   6. Apply one of the app's own themes **with** a clock or stats and let it run for 10 seconds.
   7. Close DF Space Station from the tray (this records how it says goodbye, if it does).
4. In Wireshark press the red **Stop** button, then **File → Save As…**, name it `v92.pcapng`.

If Wireshark shows nothing arriving while you switch themes, the wrong USBPcap interface was chosen: stop, pick
the next one in step 2 and repeat.

## What happens with it

The solid-color pictures show how one pixel is encoded (which bytes change between red, green and blue), the
pattern shows the resolution, the order of the pixels and whether the image is compressed, and the brightness
steps show the control commands. If the pictures turn out to be plain or JPEG-compressed, a driver follows; if
they are encrypted or packed in some private way, we will say so instead of guessing.
