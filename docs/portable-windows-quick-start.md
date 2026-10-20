# Fluke 8845A Lab Windows 便携版

这是已打包的 Windows x64 应用，不是源码包。无需单独安装 Python 或运行 pip。

## 启动桌面程序

1. 将 ZIP 解压到一个可写文件夹，并保留解压出的全部文件和子文件夹。
2. 双击解压目录中的 `Fluke8845A.exe`。
3. 在“仪器”设置中选择 COM 端口并连接。连接真实仪器前，请先核对仪器处于 RS-232 / COMPUTER 模式，并确认串口参数和接线。
4. 没有连接仪器时，可勾选“使用明确标记的 MOCK 模拟仪器”体验界面。Mock 读数是模拟数据，不是仪器测量结果。

请勿只复制或移动 `Fluke8845A.exe`；它需要与 ZIP 中的运行库文件放在一起运行。实验数据会写入界面所选的保存目录。

## 启动 localhost 网页界面

双击 `Fluke8845A-Web.dist\Fluke8845A-Web.exe`。程序会启动只监听本机回环地址的服务，并尝试打开默认浏览器；默认地址为 `http://127.0.0.1:8765/`。请在桌面版和网页界面之间择一连接仪器，同一个 COM 端口不能同时由两个进程打开。

## Fluke 8845A Lab Windows Portable

This ZIP contains the packaged Windows x64 application, not the source project. Python and pip are not required.

1. Extract the entire ZIP to a writable folder and keep all files and subfolders together.
2. Double-click `Fluke8845A.exe` in the extracted folder.
3. Select the COM port in the instrument settings. Before connecting real hardware, verify RS-232 / COMPUTER mode, serial settings, and wiring on the instrument.
4. Without an instrument, select the explicitly labelled Mock instrument to explore the interface. Mock readings are simulated and are not measurements from a Fluke meter.

Do not move or copy only the desktop EXE; it relies on the bundled runtime files. Experiment data is written to the directory selected in the app.

To start the optional localhost interface, double-click `Fluke8845A-Web.dist\Fluke8845A-Web.exe`. It listens on the local loopback address and attempts to open the default browser at `http://127.0.0.1:8765/`. Use only one interface at a time for a given COM port because Windows does not allow both processes to open the same serial port simultaneously.
