# v1.0.0 — source release

This first public release contains the Python source, tests, documentation, application artwork, and simulated measurement examples.

## Included

- Six measurement modes: DCV, ACV, DCI, ACI, 2-wire resistance, and 4-wire resistance
- RS-232/SCPI control with the project's own serial driver
- Continuous CSV recording, event markers, live plotting, and statistics
- Mock mode and example data explicitly marked as simulated

## Verification scope

The project notes record 198 passing tests and six-mode frozen-app Mock checks from the development build. These checks were not rerun for this release preparation. The exact current source snapshot has not completed hardware validation across all measurement modes or accuracy testing.

## Packaging

This is a source-only release. It does not attach a Windows executable because the exact Qt/PySide binary component inventory, corresponding source delivery, and replace/relink instructions are not yet ready. See [Qt licensing notes](Qt_许可与第三方声明.md).

## Privacy and reference materials

The archive contains simulated examples only. Instrument logs, raw hardware measurements, workstation identifiers, and Fluke manual PDFs are not part of this release.
