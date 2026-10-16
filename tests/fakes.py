from __future__ import annotations

from collections import deque

from fluke8845a.modes import descriptor, mode_from_scpi_function
from fluke8845a.scpi import SerialSettings


class FakeTransport:
    def __init__(self, idn="FLUKE,8845A,12345678,2.0", settings: SerialSettings | None = None, terminals="FRONT"):
        self.settings = settings or SerialSettings()
        self.idn = idn
        self.terminals = terminals
        self.is_open = False
        self.last_command = ""
        self.commands = []
        self.read_timeouts = []
        self.error_queue = deque()
        self.timeouts = set()
        self.echo_commands = False
        self.force_function = None
        self.inject_config_error = False
        self.nplc = "1"
        self.bandwidth = 20
        self.function = "VOLT:DC"
        self.range_value = 10
        self.autorange = "1"
        self.filters = {}
        self.digital_filters = {}
        self.autozero = "1"
        self.input_impedance_auto = "0"
        self.bandwidth_response_override = None
        self.local_error_on_read = False

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def write(self, payload: bytes):
        command = payload.decode("ascii").rstrip("\r\n")
        self.last_command = command
        self.commands.append(command)
        upper_command = command.upper()
        if upper_command.startswith("CONF:"):
            function = command.split(":", 1)[1]
            self.function = function
            self.input_impedance_auto = "0"
            mode = mode_from_scpi_function(function)
            if mode is not None:
                self.range_value = descriptor(mode).ranges_si[0]
                if descriptor(mode).digital_filter:
                    self.digital_filters[descriptor(mode).sense_prefix.upper()] = "1"
        elif upper_command.endswith(":RANG:AUTO ON"):
            self.autorange = "1"
        elif upper_command.endswith(":RANG:AUTO OFF"):
            self.autorange = "0"
        elif ":RANG " in upper_command:
            self.range_value = float(command.rsplit(" ", 1)[1])
        elif ":FILT:STAT " in upper_command:
            prefix = command[:upper_command.index(":FILT:STAT")]
            self.filters[prefix.upper()] = "1" if upper_command.endswith(" ON") else "0"
        elif ":FILT:DIG " in upper_command:
            prefix = command[:upper_command.index(":FILT:DIG")]
            self.digital_filters[prefix.upper()] = "1" if upper_command.endswith(" ON") else "0"
        elif upper_command.startswith("ZERO:AUTO "):
            self.autozero = "1" if upper_command.endswith(" ON") else "0"
        elif upper_command.startswith("INP:IMP:AUTO "):
            self.input_impedance_auto = "1" if upper_command.endswith(" ON") else "0"
        elif ":NPLC " in upper_command:
            self.nplc = command.rsplit(" ", 1)[1]
        elif upper_command.startswith("SENS:DET:BAND "):
            self.bandwidth = command.rsplit(" ", 1)[1]
        if command.startswith("CONF:") and self.inject_config_error:
            self.error_queue.append('101,"Invalid parameter"')

    def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
        self.read_timeouts.append((self.last_command, timeout_s))
        if self.last_command in self.timeouts:
            return b"partial"
        if self.echo_commands:
            return self.last_command.encode("ascii") + terminator
        command = self.last_command.upper()
        if command == "*IDN?":
            response = self.idn
        elif command == "SYST:ERR?":
            response = self.error_queue.popleft() if self.error_queue else '0,"No error"'
        elif command == "SENS:FUNC?":
            response = self.force_function or self.function
        elif command == "ROUT:TERM?":
            response = self.terminals
        elif command.endswith(":RANG:AUTO?"):
            response = self.autorange
        elif command.endswith(":RANG?"):
            response = str(self.range_value)
        elif command.endswith(":NPLC?"):
            response = str(self.nplc)
        elif command.endswith(":FILT:STAT?"):
            prefix = command.removesuffix(":FILT:STAT?")
            response = self.filters.get(prefix, "0")
        elif command.endswith(":FILT:DIG?"):
            prefix = command.removesuffix(":FILT:DIG?")
            response = self.digital_filters.get(prefix, "0")
        elif command == "SENS:DET:BAND?":
            response = (
                self.bandwidth_response_override
                if self.bandwidth_response_override is not None
                else str(self.bandwidth)
            )
        elif command == "ZERO:AUTO?":
            response = self.autozero
        elif command == "INP:IMP:AUTO?":
            response = self.input_impedance_auto
        elif command == "READ?":
            response = '+550,"Command not allowed in local"' if self.local_error_on_read else "+1.2345678901234567E+00"
        elif command.startswith("MEAS:"):
            response = "2.5"
        else:
            response = "0"
        return response.encode("ascii") + terminator

    def discard_input(self):
        pass

