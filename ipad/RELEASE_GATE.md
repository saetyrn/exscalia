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

The report labels every physical-device item `HARDWARE-ONLY`; it is not a
release sign-off. The same report is printed by Xcode when the launch argument
is present.

## Sync log

The report runs at launch, before any sync. For the sync items below, read the
sync engine's log: every failed push, pull and page fetch with its HTTP status,
and every state the pill is sent.

```sh
xcrun simctl spawn booted log stream --predicate 'subsystem == "art.exscalia.ipad"' --level info
```

On the iPad, Console.app with the same filter. The newest failure is also kept
in the Paper store as `sync.last_error` and survives a relaunch.

## Required physical iPad pass

Run on the signed iPad Air before shipping and record each item as pass/fail:

1. Write continuously for **30–60 minutes**. Pencil pressure and tilt change
   the expected stroke; palm rejection works.
2. Two-finger pan never draws, and pinch zoom remains centred under the
   fingers.
3. Pencil double-tap follows the system setting; squeeze opens tool presets;
   hover shows the width preview.
4. Lock/background the app during a stroke, then force-quit it. Relaunch and
   verify the stroke is intact and the save-state pill is truthful.
5. Open a notebook, tap + > Scan document and cancel the scanner (cancelling
   is enough), write a few strokes, then force-quit. Relaunch and verify the
   ink is intact and the save-state pill is truthful. Repeat with + > Camera.
6. Import and annotate a large (50-page) PDF, scroll through it, and export it;
   verify the original asset remains unchanged.
7. Make an edit in airplane mode, reconnect, and verify it reaches the
   server. Force a two-device edit and verify both the original and conflict
   sibling remain.
8. Open the same notebook in two windows side by side (Split View or Stage
   Manager). Write on a page in window A and verify window B shows the ink
   within about a second and takes no Pencil input on that page until it
   does. Then write on the same page in window B, force-quit, relaunch and
   verify both windows' ink is on the page.
9. Verify the Personal Team profile installs with bundle id
   `art.exscalia.ipad`, and note the seven-day re-sign date.

Do not mark these checks passed from simulator output. Free Apple ID builds do
not provide APNs or iCloud; Paper syncs through the configured OMNIA server.
