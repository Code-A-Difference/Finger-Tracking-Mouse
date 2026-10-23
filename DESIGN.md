---
name: Finger Mouse
description: A webcam mouse for people who find a physical mouse hard to use, set up like the system's own Settings app.
colors:
  ground: "#f3f3f3"
  surface: "#ffffff"
  surface-raised: "#f9f9f9"
  stroke: "#e1e1e1"
  stroke-strong: "#c9c9c9"
  text: "#1b1b1b"
  text-secondary: "#5c5c5c"
  accent: "#005fb8"
  accent-hover: "#1a6fc4"
  on-accent: "#ffffff"
  select: "#e9e9e9"
  focus: "#1b1b1b"
  ok: "#0f7b0f"
  ok-bg: "#dff6dd"
  warn: "#8a5300"
  warn-bg: "#fff4ce"
  bad: "#c42b1c"
  bad-bg: "#fde7e9"
  idle: "#5c5c5c"
  idle-bg: "#ebebeb"
  preview: "#1b1b1b"
  preview-text: "#d6d6d6"
  dark-ground: "#202020"
  dark-surface: "#2b2b2b"
  dark-surface-raised: "#323232"
  dark-stroke: "#3a3a3a"
  dark-stroke-strong: "#5a5a5a"
  dark-text: "#ffffff"
  dark-text-secondary: "#c8c8c8"
  dark-accent: "#60cdff"
  dark-accent-hover: "#4cc2ff"
  dark-on-accent: "#000000"
  dark-select: "#2d2d2d"
  dark-focus: "#ffffff"
  dark-ok: "#6ccb5f"
  dark-ok-bg: "#1f3a1c"
  dark-warn: "#fce100"
  dark-warn-bg: "#433519"
  dark-bad: "#ff99a4"
  dark-bad-bg: "#442726"
  dark-idle: "#c8c8c8"
  dark-idle-bg: "#383838"
  dark-preview: "#141414"
  dark-preview-text: "#c8c8c8"
typography:
  display:
    fontFamily: "Segoe UI Variable Text, Segoe UI, .AppleSystemUIFont, SF Pro Text, Cantarell, Noto Sans, sans-serif"
    fontSize: "28px"
    fontWeight: 600
  headline:
    fontFamily: "Segoe UI Variable Text, Segoe UI, .AppleSystemUIFont, SF Pro Text, Cantarell, Noto Sans, sans-serif"
    fontSize: "26px"
    fontWeight: 600
  title:
    fontFamily: "Segoe UI Variable Text, Segoe UI, .AppleSystemUIFont, SF Pro Text, Cantarell, Noto Sans, sans-serif"
    fontSize: "17px"
    fontWeight: 600
  title-sm:
    fontFamily: "Segoe UI Variable Text, Segoe UI, .AppleSystemUIFont, SF Pro Text, Cantarell, Noto Sans, sans-serif"
    fontSize: "15px"
    fontWeight: 600
  body-lg:
    fontFamily: "Segoe UI Variable Text, Segoe UI, .AppleSystemUIFont, SF Pro Text, Cantarell, Noto Sans, sans-serif"
    fontSize: "16px"
    fontWeight: 400
  body:
    fontFamily: "Segoe UI Variable Text, Segoe UI, .AppleSystemUIFont, SF Pro Text, Cantarell, Noto Sans, sans-serif"
    fontSize: "15px"
    fontWeight: 400
  control:
    fontFamily: "Segoe UI Variable Text, Segoe UI, .AppleSystemUIFont, SF Pro Text, Cantarell, Noto Sans, sans-serif"
    fontSize: "14px"
    fontWeight: 400
  label:
    fontFamily: "Segoe UI Variable Text, Segoe UI, .AppleSystemUIFont, SF Pro Text, Cantarell, Noto Sans, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  label-mono:
    fontFamily: "Consolas, Menlo, monospace"
    fontSize: "12px"
    fontWeight: 400
rounded:
  item: "4px"
  control: "6px"
  card: "8px"
  pill: "11px"
spacing:
  hair: "2px"
  xs: "4px"
  sm: "8px"
  md: "10px"
  row-y: "12px"
  row-x: "18px"
  card: "20px"
  page-top: "28px"
  page-x: "36px"
components:
  button:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "8px 16px"
    height: "44px"
  button-hover:
    backgroundColor: "{colors.surface-raised}"
  button-pressed:
    backgroundColor: "{colors.select}"
  button-accent:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.control}"
    padding: "8px 16px"
  button-accent-hover:
    backgroundColor: "{colors.accent-hover}"
  big-toggle-start:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.card}"
    padding: "8px 18px"
    height: "58px"
    width: "132px"
  big-toggle-stop:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.card}"
    padding: "8px 18px"
    height: "58px"
  input:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "7px 10px"
    height: "38px"
  group-card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.card}"
    padding: "12px 18px"
  mode-tile:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.card}"
    padding: "14px 16px 14px 14px"
    height: "84px"
  mode-tile-selected:
    backgroundColor: "{colors.select}"
  chip-ok:
    backgroundColor: "{colors.ok-bg}"
    textColor: "{colors.ok}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
  chip-warn:
    backgroundColor: "{colors.warn-bg}"
    textColor: "{colors.warn}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
  chip-bad:
    backgroundColor: "{colors.bad-bg}"
    textColor: "{colors.bad}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
  chip-idle:
    backgroundColor: "{colors.idle-bg}"
    textColor: "{colors.idle}"
    rounded: "{rounded.pill}"
    padding: "2px 10px"
  nav-item:
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    height: "44px"
  nav-item-selected:
    backgroundColor: "{colors.select}"
  step-number:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    size: "28px"
  step-number-done:
    backgroundColor: "{colors.ok}"
  hold-button:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    height: "44px"
  state-disc:
    size: "72px"
---

# Design System: Finger Mouse

## Overview

**Creative North Star: "The Settings App You Already Know"**

Finger Mouse looks like the operating system's own Settings app, on purpose. A caregiver who has ever changed a setting on Windows 11 or macOS should know how to drive it before reading a word: sidebar of pages on the left, a page title and one-line intro, titled groups of rows on white cards, controls on the right of each row. The category standard is the identity; the craft bar is Windows 11 Settings, macOS System Settings and Grid 3, and the sizes lean toward Grid 3 because the person using it reads Home from a seat and every click costs them effort.

The system follows the OS light or dark mode and has two complete palettes with matching roles. Surfaces are flat: a mica-grey ground, cards that are a step lighter (or darker) with a 1px stroke, and no shadows. One accent blue does all the pointing (primary action, current selection, slider fill, step numbers, switches that are on). Green, amber and red appear only to report state, and always alongside an icon and words.

Density is calm and generous: a 15px base text, 44px nav rows and buttons, 84px mode tiles, a 58px Start/Stop, a single content column capped at 880px. Motion is absent apart from the hold-to-open fill; nothing flashes or slides.

**Key Characteristics:**
- System light/dark, two full palettes with identical role names.
- Flat tonal layering with 1px strokes; no shadows anywhere.
- One accent blue for action and selection; status hues only for state.
- System UI face at 15px base, semibold for every heading.
- Lucide stroke icons at 2px stroke, tinted to the text colour.
- Large, forgiving targets; a strong ink-coloured focus ring.

## Colors

A neutral system-grey world with one blue voice and a small, strictly semantic status set. Every token exists in light and dark (`dark-` prefix) with the same role.

### Primary
- **System Blue** (`accent`; `dark-accent` in dark mode, a bright sky blue): Start, the accent dialog button, the selected mode tile border, the sidebar's current-page pill, slider fill and handle ring, progress chunks, step numbers, switches that are on. Text on it uses `on-accent` (white in light, black in dark).
- **System Blue Hover** (`accent-hover`): hover on accent buttons only.

### Neutral
- **Mica Ground** (`ground`): the window, sidebar, and page background; also the disabled-button fill.
- **Card White** (`surface`; dark `#2b2b2b`): grouped settings cards, the Home status card, buttons, tiles, menus, tooltips.
- **Raised Wash** (`surface-raised`): row and tile hover, input fields, the summary box.
- **Hairline** (`stroke`): card borders, row dividers inside a group, progress track.
- **Firm Line** (`stroke-strong`): button, input and tile borders, slider groove, scrollbar handle.
- **Ink** (`text`) and **Muted Ink** (`text-secondary`): primary and secondary text; muted ink carries hints, intros, values, footnotes, the off-state switch.
- **Selection Grey** (`select`): current sidebar page, selected list and menu items, pressed buttons, selected tile fill, the hold-button fill.
- **Focus Ink** (`focus`): equal to `text`; every focus ring.
- **Camera Black** (`preview`, `preview-text`): the camera picture well, dark in both modes.

### Status (semantic only)
- **Working Green** (`ok` / `ok-bg`): tracking states, finished setup steps.
- **Needs-a-Look Amber** (`warn` / `warn-bg`): no hand, no face, paused, not calibrated.
- **Problem Red** (`bad` / `bad-bg`): stopped by an error, error text.
- **Resting Grey** (`idle` / `idle-bg`): stopped on purpose, starting.

### Named Rules
**The One Blue Rule.** Blue means "act here" or "this one is chosen". It never decorates, never fills a card, never colours body text.

**The Words-First State Rule.** A status hue never appears alone. Every state shows a tinted disc with its own icon, a headline in words, and a chip with its name; colour only backs them up. If the hue were removed, the state must still read.

## Typography

**Display Font:** the system UI face: Segoe UI Variable Text on Windows, the Apple system face on macOS, Cantarell or Noto Sans on Linux.
**Body Font:** the same face.
**Label/Mono Font:** Consolas / Menlo, only for the optional diagnostics line.

**Character:** One native face at every level, so the app reads as part of the OS. Hierarchy comes from size and a single semibold weight, not from a second family.

### Hierarchy
- **Display** (600, 28px): page titles ("Set up", "Head pointer").
- **Headline** (600, 26px): the Home state line ("Head pointer is on", "Not running").
- **Title** (600, 17px): app name in the sidebar, mode tile names; the Start/Stop label is 18px 600.
- **Title Small** (600, 15–16px): group titles above cards (15px), setup step titles (16px), the current sidebar page.
- **Body Large** (400, 16px): the how-to line on Home.
- **Body** (400, 15px): the application base; row titles, status detail, nav items, radio labels.
- **Control** (400, 14px): text inside buttons, inputs, lists; row values.
- **Label** (400 or 600, 13px): row hints, intros' footnotes, chips (600), the "In use" nav badge (12px).

### Named Rules
**The Native Face Rule.** Use the system UI face and nothing else for interface text. The 15px base is deliberately a step above Qt's default so it reads from a seat; do not shrink it.

**The One Weight Rule.** Headings are 600; body is 400. The single exception is the oversized live sign letter on the Sign language page (88px, 700).

## Layout

A fixed sidebar (256px; the direction contract said 232px, the build settled on 256) on the ground colour, then a page stack. Settings and Set up pages scroll inside a single centred column capped at 880px, with page margins of 28px top and 36px left, right and bottom. Home does not scroll: 32px side margins, a status card across the top, the camera well filling the rest, a footnote below.

Rhythm: 8px between cards in a column, plus 10px before each group title; 6px extra between setup steps. Rows inside a group card pad 12px vertically and 18px horizontally, with 18px between the text block and the controls (10px when a row stacks its controls underneath). Setup step cards pad 20px / 16px top / 18px bottom and indent their body 38px to align with the title past the step number. Mode tiles sit in a grid with 10px gaps. Sidebar items have 2px between them; the "Settings" group label gets 14px above.

The window opens at 1080 × 780 and holds down to 900 × 640 without changing structure; text wraps rather than truncates.

**The One Column Rule.** Settings never sit side by side in columns. One column, title and hint on the left of each row, the control on the right.

## Elevation & Depth

Flat. There are no shadows in the system. Depth is tonal: ground, then a card a step away from the ground, then a raised wash for hover and inputs, separated by 1px strokes. The camera well is the one deep surface, near-black in both modes.

**The Stroke-Not-Shadow Rule.** A surface is separated by a 1px `stroke` (cards) or `stroke-strong` (controls), never by a shadow or glow.

## Shapes

Gently rounded, small radii that track the OS: 4px for list and menu items, 6px for buttons, inputs, nav items and the hold button, 8px for cards, tiles, the camera well and the big Start/Stop. Pills are fully round: chips (11px), the switch track (11px), step numbers and the state disc are circles. Borders are 1px; the selected mode tile and the focus ring step up to 2px.

## Components

### Buttons
Quiet, OS-native, with one loud exception.
- **Shape:** gently rounded (6px), 1px `stroke-strong` border.
- **Default:** `surface` fill, `text` ink, 14px, padding 8px 16px; an optional 16px Lucide icon on the left in the text colour.
- **Hover / Pressed:** fill steps to `surface-raised`, then `select`.
- **Accent:** `accent` fill, `on-accent` text, 600; used for the dialog OK and Start.
- **Disabled:** `ground` fill, `stroke` border, muted text; the icon greys with it.
- **Big Start/Stop:** 8px radius, 18px 600 label, at least 40px content height (about 58px tall) and 132px wide. Stopped, it is the accent button with a filled play icon; running, it becomes a default button with a filled stop square. It sits at the right of the Home status card and in setup step 3.

### Chips
- **Style:** pill (11px), 13px 600, padding 2px 10px, fill from the tone's `*-bg` and text from the tone's ink.
- **State:** one chip per state, always beside the status detail text; its name is the state's word ("Tracking", "No face", "Stopped").

### Cards / Containers
- **Corner Style:** 8px.
- **Background:** `surface`.
- **Shadow Strategy:** none (see Elevation & Depth).
- **Border:** 1px `stroke`; rows inside are divided by 1px `stroke` lines running edge to edge.
- **Internal Padding:** rows 12px / 18px; the Home status card 20px / 22px; setup steps 16–18px / 20px.

### Inputs / Fields
- **Style:** `surface-raised` fill, 1px `stroke-strong`, 6px radius, padding 7px 10px, 14px text. Combo boxes are at least 220px wide with a 16px Lucide chevron.
- **Sliders:** 4px groove in `stroke-strong`, `accent` fill to the left, a 20px handle drawn as a `surface` dot inside a 5px `accent` ring; 240px wide with the value (muted, 14px) to its left.
- **Switches:** 44 × 22px track with the word "On" or "Off" to its left. Off is an outlined track in muted ink with a 10px knob; on is a solid `accent` track with a 12px `on-accent` knob.
- **Focus:** a 2px `focus` (ink) border; custom-painted controls draw a 2px ring 4px outside themselves.

### Navigation
- **Sidebar items:** 44px tall, 20px Lucide icon at 14px in, 15px label. Hover and current fill with `select` at 6px radius; the current page also gets a 3 × 16px `accent` pill at the left edge and a semibold label. A short muted note ("In use", 12px) can sit at the right.
- **Grouping:** Home and Set up first, then a muted "Settings" label, then the eight settings pages.
- **Caregiver lock:** when locked, the sidebar keeps only Home, and a hold button sits at the foot.

### Mode Tile (signature)
A whole card that is a radio button, Grid 3 sized: at least 84px tall, `surface` fill, 1px `stroke-strong`, 8px radius, a radio, a 32px Lucide mode icon, a 17px title and a 13px hint. Hover washes to `surface-raised`; the chosen tile gets a 2px `accent` border and `select` fill. Click anywhere on it to choose.

### State Disc (signature)
A 72px circle in the state's `*-bg` with a 36px Lucide icon in the state's ink at 2.2px stroke, top-left of the Home status card. Each state has its own icon (power, alert, loader, scan, pause, or the mode's icon).

### Hold Button (signature)
The caregiver unlock: at least 44px tall, `surface` fill, `stroke-strong` border, 6px radius, a lock icon and "Caregiver: hold to open". Holding for 3 seconds (pointer or Space/Enter) fills it left to right with `select` while the label counts down "Keep holding… 2". Releasing early empties it. This fill is the only motion in the system.

### Setup Step
A group card with a 28px circular number in `accent` (turning `ok` when done), a 16px semibold title, and a right-aligned state in muted 14px ("Not checked yet") that turns `ok` and semibold when done ("7 found").

## Do's and Don'ts

### Do:
- **Do** take every colour from the active palette (`colors()`), so light and dark stay in step; add any new token to both palettes with the same name.
- **Do** pair every state with an icon and words, then tint it with its `*-bg` / ink pair.
- **Do** build settings as a titled group card of rows: title and hint left, control right, 1px dividers between.
- **Do** keep primary actions large (the 58px Start/Stop, 84px tiles) and keep every target at least 44px tall, as the default buttons and sidebar rows are.
- **Do** use Lucide icons at 2px stroke, tinted to `text`, with a greyed copy for disabled.
- **Do** keep the 2px ink focus ring on every focusable control, including custom-painted ones.

### Don't:
- **Don't** use the accent blue for decoration, card fills, body text or status.
- **Don't** show green, amber or red without an icon and a word.
- **Don't** add shadows, glows or gradients; separate with 1px strokes and tonal steps.
- **Don't** introduce a second typeface or a web display face; the system UI face is the identity.
- **Don't** lay settings out in side-by-side columns or bring back a tab strip.
- **Don't** add motion in the window beyond the hold-button fill; nothing flashes, pulses or slides.
- **Don't** add uppercase eyebrows or kicker labels above titles; the page title and one-line intro carry the page.
