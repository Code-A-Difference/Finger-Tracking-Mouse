# Product

<!-- impeccable:product-schema 1 -->

## Platform

desktop (Qt / PySide6 window app for Windows, macOS and Linux; not a web page)

## Users

Two people use Finger Mouse, and they are rarely the same person:

- **The person using it** controls the computer without a physical mouse, with their head, a hand, their eyes, or by fingerspelling. Some have nervous system conditions (tremor, limited or tiring movement, low stamina, difficulty with fine motor control). They do not set the app up themselves. Every click they make costs effort, so the everyday screen must be hard to break and easy to read from a seated distance.
- **The caregiver who sets it up and supports it**: family at home, school staff and educational assistants, and care-home or support workers (who change shifts, so many different, often non-technical people touch the same setup). They need to get it working quickly, fix it when it stops working, and hand over to the next person without reading a manual.

Marketing currently goes to schools, but the app is kept for the general public.

## Product Purpose

Let someone who finds a physical mouse hard to use control the real pointer with a webcam, offline, on their own computer. Success: a caregiver gets the person working in a few minutes, the person can use it for a whole session without a caregiver stepping in, and when something goes wrong anyone nearby can see what is wrong and fix it.

## Positioning

Free, open source, runs entirely on the computer (no video leaves it, no internet needed), and offers several ways to steer in one app: head pointer, sign language typing, hand gestures and eye gaze. Made by Code A Difference, a student group.

## Operating Context

- The person sits in front of a laptop or desktop webcam, often in a wheelchair or supported seating, sometimes at a school desk. The caregiver stands beside or behind them, often leaning in to use the trackpad.
- Setup is a moment: choose the camera, choose the way of steering, check it works, adjust a few things. Then the window mostly sits minimised or to the side while the person uses other apps.
- Problems happen mid-session: the camera is taken by another app, lighting changes, the person moved, the pointer drifts.

## Capabilities and Constraints

- Tracking modes: **head pointer** (nose steers, wink or long blink to click, mouth for drag/scroll/click, optional smile action, Snap) and **sign language typing** (ASL fingerspelling, taught per person) are the priority modes. Hand gestures and eye gaze (beta, needs calibration) remain.
- Everything applies immediately and saves to the user folder. Escape stops tracking. Moving the real mouse pauses hand control. A halo follows the pointer.
- Teaching signs and eye calibration need tracking already running in that mode.
- An **optional caregiver lock** (off by default) is wanted so the person using it can't change the setup by accident.
- Must keep: the self-test, the tray icon, update notices, all existing settings and their saved format.

## Brand Commitments

- Name: Finger Mouse. Publisher: Code A Difference. Plain, friendly, short wording; no em dashes in copy.
- Look: the category standard, chosen on purpose (2026-10-23): a modern system-settings app. It should sit naturally beside Windows 11 Settings, macOS System Settings and Grid 3; their craft level is the bar. Follow the system's light or dark mode.

## Evidence on Hand

- README and CHANGES.md describe the features. No testimonials, user counts or clinical claims exist; none may be invented.

## Product Principles

1. Two audiences, two depths: an everyday surface for the person using it, a setup surface for the caregiver.
2. Never strand anyone: whatever state it is in, the screen says what is happening and the one thing to do next.
3. Big, few, forgiving: large targets, few choices at a time, nothing irreversible from one stray click.
4. Setup is a path, not a pile of tabs: a caregiver follows steps that end in "it works".
5. Calm by default: no flashing, no surprise motion, no alarm colours unless something truly needs attention.

## Accessibility & Inclusion

- Large targets (at least 44 px, primary actions much larger), high contrast, readable from a seated distance, full keyboard access with a strong visible focus.
- Respect reduced motion. Never rely on colour alone for state.
- Wording at a plain reading level for caregivers of any technical background.
