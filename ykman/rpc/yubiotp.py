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


from .base import RpcNode, action, child

from yubikit.yubiotp import YubiOtpSession, SLOT


class YubiOtpNode(RpcNode):
    def __init__(self, connection):
        super().__init__()
        self.session = YubiOtpSession(connection)

    def get_data(self):
        state = self.session.get_config_state()
        data = dict(
            is_led_inverted=state.is_led_inverted(),
            slot1_configured=state.is_configured(SLOT.ONE),
            slot2_configured=state.is_configured(SLOT.TWO),
        )
        if self.session.version >= (3, 0, 0):
            data.update(
                slot1_touch_triggered=state.is_touch_triggered(SLOT.ONE),
                slot2_touch_triggered=state.is_touch_triggered(SLOT.TWO),
            )
        return data

    @action
    def swap(self, params, event, signal):
        self.session.swap_slots()
        return dict()

    @child
    def one(self):
        return SlotNode(self.session, SLOT.ONE)

    @child
    def two(self):
        return SlotNode(self.session, SLOT.TWO)


class SlotNode(RpcNode):
    def __init__(self, session, slot):
        super().__init__()
        self.session = session
        self.slot = slot
        self._state = self.session.get_config_state()

    def get_data(self):
        self._state = self.session.get_config_state()
        data = dict(is_configured=self._state.is_configured(self.slot))
        if self.session.version >= (3, 0, 0):
            data.update(is_touch_triggered=self._state.is_touch_triggered(self.slot))
        return data

    @action(condition=lambda self: self._state.is_configured(self.slot))
    def delete(self, params, event, signal):
        self.session.delete_slot(self.slot, params.pop("acc_code", None))

    @action(
        condition=lambda self: self._state.is_configured(self.slot)
        and not self._state.is_touch_triggered(self.slot)
    )
    def calculate(self, params, event, signal):
        challenge = bytes.fromhex(params.pop("challenge"))
        response = self.session.calculate_hmac_sha1(self.slot, challenge, event)
        return dict(response=response.hex())
