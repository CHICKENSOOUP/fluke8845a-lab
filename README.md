# Fluke 8845A Lab

Windows desktop application for controlling a Fluke 8845A over RS-232, recording measurements, and viewing live plots.

> This is an independent community project and is not affiliated with or endorsed by Fluke.

## Features

- DC voltage, AC voltage, DC current, AC current, 2-wire resistance, and 4-wire resistance
- Serial-port configuration and SCPI communication through the project's own driver
- Continuous CSV logging, timestamps, event markers, statistics, and live plots
- Mock mode for exercising all six measurement workflows without connecting an instrument

Qt/PySide6 supplies the graphical interface; pyserial and the project's SCPI driver handle serial communication. The application does not use Qt SerialPort.

## Source setup

Requires Python 3.12 or later on Windows. From PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e ".[dev,build]"
python -m fluke8845a
```

To run the test suite:

```powershell
python -m pytest
```

Build a local standalone package with `build_windows.ps1`. The current GitHub source release does not include a prebuilt Windows executable. A binary release will be published with its exact third-party license inventory, Qt/PySide source offer, and Qt replacement instructions.

## Hardware and verification status

Use the instrument's RS-232 / COMPUTER mode and verify the serial settings on the instrument before connecting. Current test and hardware verification limits are summarized in [docs/hardware-validation.md](docs/hardware-validation.md). Mock results are simulated and are not instrument measurements. Follow the safety and wiring instructions in [docs/使用说明.md](docs/使用说明.md).

## Licensing

Project-authored code and artwork are intended to be licensed under MIT; see [LICENSE](LICENSE). PySide6/Qt and the other runtime dependencies retain their own licenses. See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) and [docs/Qt_许可与第三方声明.md](docs/Qt_许可与第三方声明.md).

This source release does not bundle Qt libraries. Do not treat the project MIT license as a license for third-party components.

## References

- [SCPI commands and technical basis](docs/SCPI与技术依据.md)
- [Usage guide](docs/使用说明.md)
- [Known limitations](docs/已知限制.md)
- [v1.0.0 release notes](docs/release-notes-v1.0.0.md)
- [Qt and third-party licensing notes](docs/Qt_许可与第三方声明.md)
