# Native Office document trust regressions

Run `node --test electron/nativeOfficeDocumentTrust.test.cjs` from `console`,
or the existing `test:electron` command. Console postinstall installs the vendor
engine dependencies used by the actual PPTX archive and streaming writer.

The suites execute actual native IPC factories, the shared admission helper and
AsyncLocalStorage context. Deep save, generated-page, thumbnail and image cases
also execute the actual PPTX engine and archive parser. Electron presentation,
Provider/network, sidecar and filesystem collaborators are finite synthetic
producers; document admission and ownership checks are never replaced.

The independent source cases were frozen before porting. Port changes are
relative module roots, OS temporary directories, and stricter assertions that
native slides factories automatically register image handlers. Generic AI
stream registration retains the standalone configuration seam.

These locks prove source behavior. They do not prove packaged Electron behavior,
real Provider availability, OS printing or the compatibility of delivered Apps.
