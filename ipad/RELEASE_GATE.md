# Physical iPad release gate

The simulator harness is deliberately incomplete. It proves that the app
still uses the fixed bundle id and that the local Paper store opens, but it
cannot prove Pencil hardware, iPad lifecycle behaviour, provisioning,
or a second device's sync response.

## Executable simulator-safe check

Run the DEBUG build with `-debugReleaseGate` and inspect the console:

```sh
xcrun simctl launch booted art.exscalia.ipad -debugReleaseGate
```

The report labels every physical-device item `HARDWARE-ONLY`, one line for
each item of the pass below, numbered as there
(`ReleaseGateReport.hardwareChecks`; a unit test keeps the two in step); it
is not a release sign-off. The same report is printed by Xcode when the launch argument
is present.

## Sync log

The report runs at launch, before any sync. For the sync items below, read the
sync engine's log: every failed push, pull and page fetch with its HTTP status,
and every state the save state is sent.

```sh
xcrun simctl spawn booted log stream --predicate 'subsystem == "art.exscalia.ipad"' --level info
```

On the iPad, Console.app with the same filter. The newest failure is also kept
in the Paper store as `sync.last_error` and survives a relaunch.

## Required physical iPad pass

Run on the signed iPad Air before shipping and record each item as pass/fail:

1. Write continuously for **30–60 minutes**. Pencil pressure and tilt change
   the expected stroke; palm rejection works.
2. Panning with one or two fingers never draws, and pinch zoom remains
   centred under the fingers.
3. Hovering the Pencil over a page shows a circle the size of the current
   tool, following the tip. (Double-tap and squeeze are item 13.)
4. Lock/background the app during a stroke, then force-quit it. Relaunch and
   verify the stroke is intact and the save state (the glyph at the right end
   of the tab bar) is truthful.
5. Open a notebook, tap Add (the plus in the navigation bar) > Scan Document
   and cancel the scanner (cancelling is enough), write a few strokes, then
   force-quit. Relaunch and verify the ink is intact and the save state is
   truthful. Repeat with Add > Camera.
6. Import and annotate a large (50-page) PDF (library: + New > Import), scroll
   through it, and export it (Share > Export All); verify the original asset
   remains unchanged.
7. Make an edit in airplane mode, reconnect, and verify it reaches the
   server. Force a two-device edit and verify both the original and conflict
   sibling remain.
8. Open the same notebook in two windows side by side (the tab menu's Open in
   New Window, or Split View or Stage Manager). Write on a page in window A and
   verify window B shows the ink within about a second and takes no Pencil
   input on that page until it does. Then write on the same page in window B,
   force-quit, relaunch and verify both windows' ink is on the page.
9. Verify the Personal Team profile installs with bundle id
   `art.exscalia.ipad`, and note the seven-day re-sign date.
10. A finger never inks, whatever the tool. One finger pans. A finger tap on
    ink, text, an image or a shape selects it, and the object menu appears at
    once.
11. Lasso some strokes, drag them, lift, then reopen the notebook: they stay
    moved. This is the owner's bug.
12. A two-finger double-tap undoes and a three-finger double-tap redoes, with
    the pen and with the lasso.
13. Pencil double-tap follows Settings > Apple Pencil: the eraser, the previous
    tool, or the tool menu at the tip for the palette choices. A Pencil Pro
    squeeze opens the tool menu at the tip when it lets go.
14. Draw Shape (the tool menu's writing types): a rough rectangle, ellipse,
    triangle, line and arrow each snap clean, and one undo restores the rough
    stroke.
15. On a whiteboard, write at the lowest zoom, at 56 %, 100 % and 400 %, and
    pan across the board with one finger. The lowest zoom is the larger of 5 %
    and the zoom at which the board fills the screen: about 8 % on a 13-inch
    iPad, 7 % on an 11-inch one, and the board's edge never shows. The ink
    stays sharp. A pinch while the Pencil is down does not zoom. Dragging the
    minimap pans, and a double-tap on it fits the content. It stays smooth past
    5,000 strokes.
16. On dark paper (New Notebook > Color > Dark, or More > Paper Color), black
    ink shows light while writing, on the library thumbnail, and in an exported
    PDF.
17. Rename a folder that holds notebooks. The web viewer shows the new name
    after one sync, and the iPad downloads nothing: the save state settles as
    fast as it does for an empty folder.
18. Open five documents and switch with ⌘1-⌘5 on a hardware keyboard.
    Force-quit and relaunch: the tabs come back. Open in New Window shows the
    same document in a second window, and ink written in one appears in the
    other after a flush.
19. Convert an infinite-page notebook (its item menu or tab menu > Convert to
    Whiteboard). Its ink sits centred on its board and is found by search after
    recognition.

Do not mark these checks passed from simulator output. Free Apple ID builds do
not provide APNs or iCloud; Paper syncs through the configured OMNIA server.
