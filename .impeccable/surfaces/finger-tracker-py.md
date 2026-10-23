---
version: 1
slug: "finger-tracker-py"
primary_target: "Finger_tracker.py"
related_targets: []
---

# Main window (Finger Mouse)

Scope: the whole main window (Home, Set up, every settings page, Caregiver) plus the Teach signs window. Mode: Operate. Audience: the person using it (reads Home from their seat) and the caregiver (sets up, fixes, hands over). Platform: Qt desktop.

## Direction contract

THESIS: Finger Mouse is a settings app the caregiver already knows how to drive, Windows 11 Settings / macOS System Settings grade, with Grid 3's sizes. It refuses the old dark single window with nine overflowing tabs of sliders.

OWN-WORLD: system light or dark, mica-grey ground, white (or #2b2b2b) grouped cards with 1px strokes, one accent blue for primary actions and selection, success green / amber / red only for state and always with an icon and words. System UI font (Segoe UI Variable / SF). Lucide stroke icons. Toggle switches with On/Off text.

STORY: The person sees one big status and one Start/Stop. The caregiver opens Set up and follows four numbered steps that end in "it works", fine-tunes on the mode's page, optionally locks it, and copies a handover note for the next carer.

FIRST VIEWPORT: Left sidebar 232 px: app name, Home, Set up, then settings pages (Head pointer, Sign language, Hand gestures, Eye gaze, Pointer, Camera, Caregiver, Advanced); lock control at its foot. Home: a status card across the top (72 px state disc, 26 px headline like "Head pointer is on", gesture chip and live detail, how-to line), the 56 px Start/Stop at its right; the camera picture fills below.

FORM: category standard (canon), chosen by the user over the roll; seed key e1699244.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Signature interaction

The caregiver lock: when on, the sidebar collapses to Home and a "Caregiver" control that needs a 3-second hold (or a PIN, if one is set) to open setup again.
