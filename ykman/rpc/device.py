# Copyright (c) 2021 Yubico AB
# All rights reserved.
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


from .base import RpcNode, child, action, NoSuchNodeException
from .oath import OathNode
from .fido import Ctap2Node
from .yubiotp import YubiOtpNode
from .management import ManagementNode
from .. import __version__ as ykman_version
from ..device import (
    scan_devices,
    list_all_devices,
    get_name,
    read_info,
    connect_to_device,
)
from ..diagnostics import get_diagnostics
from yubikit.core import TRANSPORT
from yubikit.core.smartcard import SmartCardConnection
from yubikit.core.otp import OtpConnection
from yubikit.core.fido import FidoConnection
from yubikit.management import CAPABILITY

from ..pcsc import list_devices, YK_READER_NAME
from smartcard.Exceptions import SmartcardException
from dataclasses import asdict

import os
import logging

logger = logging.getLogger(__name__)


class RootNode(RpcNode):
    def __init__(self):
        super().__init__()
        self._devices = DevicesNode()
        self._readers = ReadersNode()

    def __call__(self, *args):
        result = super().__call__(*args)
        if result is None:
            result = {}
        return result

    def get_data(self):
        return dict(version=ykman_version)

    @child
    def usb(self):
        return self._devices

    @child
    def nfc(self):
        return self._readers

    @action
    def diagnose(self, *ignored):
        return dict(diagnostics=get_diagnostics())


class ReadersNode(RpcNode):
    def __init__(self):
        super().__init__()
        self._state = set()
        self._readers = {}
        self._reader_mapping = {}

    def list_children(self):
        devices = [
            d for d in list_devices("") if YK_READER_NAME not in d.reader.name.lower()
        ]
        state = {d.reader.name for d in devices}
        if self._state != state:
            self._readers = {}
            self._reader_mapping = {}
            for device in devices:
                dev_id = os.urandom(4).hex()
                self._reader_mapping[dev_id] = device
                self._readers[dev_id] = dict(name=device.reader.name)
            self._state = state
        return self._readers

    def create_child(self, name):
        return ReaderDeviceNode(self._reader_mapping[name], None)


class _ScanDevices:
    def __init__(self):
        self._state = 0
        self._caching = False

    def __call__(self):
        if not self._caching or not self._state:
            self._state = scan_devices()[1]
        return self._state

    def __enter__(self):
        self._caching = True
        self._state = 0

    def __exit__(self, exc_type, exc, exc_tb):
        self._caching = False


class DevicesNode(RpcNode):
    def __init__(self):
        super().__init__()
        self._get_state = _ScanDevices()
        self._list_state = 0
        self._devices = {}
        self._device_mapping = {}

    def __call__(self, *args, **kwargs):
        with self._get_state:
            return super().__call__(*args, **kwargs)

    @action(closes_child=False)
    def scan(self, *ignored):
        return dict(state=self._get_state())

    def get_data(self):
        return dict(state=self._get_state())

    def list_children(self):
        state = self._get_state()
        if state != self._list_state:
            self._devices = {}
            self._device_mapping = {}
            for dev, info in list_all_devices():
                dev_id = str(info.serial) if info.serial else os.urandom(4).hex()
                while dev_id in self._device_mapping:
                    dev_id = os.urandom(4).hex()
                self._device_mapping[dev_id] = (dev, info)
                name = get_name(info, dev.pid.get_type() if dev.pid else None)
                self._devices[dev_id] = dict(pid=dev.pid, name=name, serial=info.serial)
            self._list_state = state

        return self._devices

    def create_child(self, name):
        return UsbDeviceNode(*self._device_mapping[name])


class AbstractDeviceNode(RpcNode):
    def __init__(self, device, info):
        super().__init__()
        self._device = device
        self._info = info

    def __call__(self, *args, **kwargs):
        try:
            return super().__call__(*args, **kwargs)
        except (SmartcardException, OSError) as e:
            logger.error("Device error", exc_info=e)
            self._child = None
            name = self._child_name
            self._child_name = None
            raise NoSuchNodeException(name)

    def get_data(self):
        for conn_type in (SmartCardConnection, OtpConnection, FidoConnection):
            if self._device.supports_connection(conn_type):
                with self._device.open_connection(conn_type) as conn:
                    pid = self._device.pid
                    self._info = read_info(pid, conn)
                    name = get_name(self._info, pid.get_type() if pid else None)
                    return dict(
                        pid=pid,
                        name=name,
                        transport=self._device.transport,
                        info=asdict(self._info),
                    )
        raise ValueError("No supported connections")


class UsbDeviceNode(AbstractDeviceNode):
    def __init__(self, device, info):
        super().__init__(device, info)
        self._interfaces = device.pid.get_interfaces()

    def _supports_connection(self, conn_type):
        return self._interfaces.supports_connection(conn_type)

    def _create_connection(self, conn_type):
        if self._device.supports_connection(conn_type):
            connection = self._device.open_connection(conn_type)
        elif self._info and self._info.serial:
            connection = connect_to_device(self._info.serial, [conn_type])[0]
        else:
            pids = scan_devices()[0]
            if (
                sum(
                    n
                    for pid, n in pids.items()
                    if pid.get_interfaces().supports_connection(conn_type)
                )
                != 1
            ):
                raise ValueError("Unable to uniquely identify device")
            connection = connect_to_device(connection_types=[conn_type])[0]
        return ConnectionNode(self._device.transport, connection, self._info)

    @child(condition=lambda self: self._supports_connection(SmartCardConnection))
    def ccid(self):
        return self._create_connection(SmartCardConnection)

    @child(condition=lambda self: self._supports_connection(OtpConnection))
    def otp(self):
        return self._create_connection(OtpConnection)

    @child(condition=lambda self: self._supports_connection(FidoConnection))
    def fido(self):
        return self._create_connection(FidoConnection)


class ReaderDeviceNode(AbstractDeviceNode):
    def get_data(self):
        try:
            return super().get_data() | dict(present=True)
        except Exception:
            return dict(present=False)

    @child
    def ccid(self):
        connection = self._device.open_connection(SmartCardConnection)
        info = read_info(None, connection)
        return ConnectionNode(self._device.transport, connection, info)

    @child
    def fido(self):
        with self._device.open_connection(SmartCardConnection) as conn:
            info = read_info(None, conn)
        connection = self._device.open_connection(FidoConnection)
        return ConnectionNode(self._device.transport, connection, info)


class ConnectionNode(RpcNode):
    def __init__(self, transport, connection, info):
        super().__init__()
        self._transport = transport
        self._connection = connection
        self._info = info or read_info(None, self._connection)

    @property
    def capabilities(self):
        return self._info.config.enabled_capabilities[self._transport]

    def close(self):
        super().close()
        self._connection.close()

    def get_data(self):
        if (
            isinstance(self._connection, SmartCardConnection)
            or self._transport == TRANSPORT.USB
        ):
            self._info = read_info(None, self._connection)
        return dict(version=self._info.version, serial=self._info.serial)

    @child(
        condition=lambda self: self._transport == TRANSPORT.USB
        or isinstance(self._connection, SmartCardConnection)
    )
    def management(self):
        return ManagementNode(self._connection)

    @child(
        condition=lambda self: isinstance(self._connection, SmartCardConnection)
        and CAPABILITY.OATH in self.capabilities
    )
    def oath(self):
        return OathNode(self._connection)

    @child(
        condition=lambda self: isinstance(self._connection, FidoConnection)
        and CAPABILITY.FIDO2 in self.capabilities
    )
    def ctap2(self):
        return Ctap2Node(self._connection)

    @child(
        condition=lambda self: CAPABILITY.OTP in self.capabilities
        and (
            isinstance(self._connection, OtpConnection)
            or (  # SmartCardConnection can be used over NFC, or on 5.3 and later.
                isinstance(self._connection, SmartCardConnection)
                and (
                    self._transport == TRANSPORT.NFC or self._info.version >= (5, 3, 0)
                )
            )
        )
    )
    def yubiotp(self):
        return YubiOtpNode(self._connection)
