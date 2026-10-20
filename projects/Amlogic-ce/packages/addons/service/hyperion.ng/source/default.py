# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2018-present Team CoreELEC (https://coreelec.org)

import os
import select
import socket
import sys
import time

import xbmc
import xbmcaddon

sys.path.insert(0, os.path.join(xbmcaddon.Addon().getAddonInfo('path'), 'resources', 'lib'))

import flatbuffers
from hyperionnet import Image, RawImage, Register, Request
from hyperionnet.Command import Command
from hyperionnet.ImageType import ImageType
from hyperionnet.Reply import Reply

HYPERIOND_ADDRESS = ('127.0.0.1', 19400)
ORIGIN = 'Kodi'
# the lowest priority the flatbuffer server accepts
PRIORITY = 199
# endless, hyperiond clears our priority when the connection closes
IMAGE_DURATION = -1
FEED_OFF = 0
FEED_VIDEO = 1
FEED_ALL = 2
CAPTURE_WIDTH = 128
CAPTURE_HEIGHT = 72
# about 20 images per second
FRAME_INTERVAL = 0.05
MIN_WAIT = 0.001
FRAME_TIMEOUT_MS = 200
# hyperiond drops a client that sends nothing for its timeout, 1 s at the least, and would keep
# the last image until then
IMAGE_TIMEOUT = 1
RECONNECT_INTERVAL = 1
# a SoC without a GUI capture path never delivers a frame; a refused request fails at once, but
# a transient vdin1 failure can too, so only a run of them backs off
EMPTY_LIMIT = 50
FAILED_LIMIT = 20
FAILED_REQUEST_MS = FRAME_TIMEOUT_MS / 2
BACKOFF_INTERVAL = 300


def log(message, level=xbmc.LOGINFO):
    xbmc.log('service.hyperion.ng: {}'.format(message), level)


class Feeder(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.player = xbmc.Player()
        self.screensaver = xbmc.getCondVisibility('System.ScreenSaverActive')
        self.feed = xbmcaddon.Addon().getSettingInt('KODI_FEED')
        self.video_only = False
        self.retry_at = 0
        self.empty = 0
        self.failed = 0
        self.capture = None
        self.builder = flatbuffers.Builder(CAPTURE_WIDTH * CAPTURE_HEIGHT * 4)
        self.sock = None
        self.received = bytearray()
        self.registered = False
        self.error = None
        self.fed_at = 0
        self.connect_at = 0
        self.connect_failed = False

    def onSettingsChanged(self):
        self.feed = xbmcaddon.Addon().getSettingInt('KODI_FEED')

    def onScreensaverActivated(self):
        self.screensaver = True

    def onScreensaverDeactivated(self):
        self.screensaver = False

    def target(self):
        # what to capture, None while nothing may be fed
        if self.feed == FEED_OFF or self.screensaver:
            return None
        if self.player.isPlayingVideo():
            return FEED_VIDEO
        if self.feed == FEED_ALL and not self.video_only and time.monotonic() >= self.retry_at:
            return FEED_ALL
        return None

    def grab(self, target):
        if self.capture is None:
            self.capture = xbmc.RenderCapture()
        if target == FEED_VIDEO:
            aspect = self.capture.getAspectRatio()
            height = round(CAPTURE_WIDTH / aspect) if aspect > 0 else CAPTURE_HEIGHT
            self.capture.capture(CAPTURE_WIDTH, height)
        else:
            try:
                self.capture.capture(CAPTURE_WIDTH, CAPTURE_HEIGHT, composite=True)
            except TypeError:
                log('Kodi has no GUI capture, feeding video only')
                self.video_only = True
                self.capture = None
                return None
        requested_at = time.monotonic()
        image = self.capture.getImage(FRAME_TIMEOUT_MS)
        failed = (time.monotonic() - requested_at) * 1000 < FAILED_REQUEST_MS
        width = self.capture.getWidth()
        height = self.capture.getHeight()
        if len(image) != width * height * 4:
            if target == FEED_ALL:
                self.empty += 1
                self.failed = self.failed + 1 if failed else 0
                if self.failed >= FAILED_LIMIT or self.empty >= EMPTY_LIMIT:
                    log('no GUI frames, retrying in {} s'.format(BACKOFF_INTERVAL))
                    self.empty = 0
                    self.failed = 0
                    self.retry_at = time.monotonic() + BACKOFF_INTERVAL
            return None
        self.empty = 0
        self.failed = 0
        rgb = bytearray(width * height * 3)
        rgb[0::3] = image[2::4]
        rgb[1::3] = image[1::4]
        rgb[2::3] = image[0::4]
        return rgb, width, height

    def connect(self):
        if self.sock is not None:
            return True
        if time.monotonic() < self.connect_at:
            return False
        try:
            self.sock = socket.create_connection(HYPERIOND_ADDRESS, timeout=1)
        except OSError:
            if not self.connect_failed:
                log('cannot connect to hyperiond at {}:{}'.format(*HYPERIOND_ADDRESS), xbmc.LOGWARNING)
                self.connect_failed = True
            self.connect_at = time.monotonic() + RECONNECT_INTERVAL
            return False
        self.connect_failed = False
        self.registered = False
        self.error = None
        return True

    def disconnect(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        self.received.clear()

    def lost(self):
        self.disconnect()
        self.connect_at = time.monotonic() + RECONNECT_INTERVAL

    def receive(self):
        # every request gets a reply, and hyperiond also sends one unasked when our priority left
        # its muxer
        while select.select([self.sock], [], [], 0)[0]:
            data = self.sock.recv(4096)
            if not data:
                return False
            self.received += data
        while len(self.received) >= 4:
            size = int.from_bytes(self.received[:4], 'big')
            if len(self.received) < size + 4:
                break
            try:
                reply = Reply.GetRootAs(bytes(self.received[4:size + 4]))
                error = reply.Error()
                registered = reply.Registered()
            except Exception:
                log('invalid reply from hyperiond', xbmc.LOGWARNING)
                return False
            del self.received[:size + 4]
            if error is not None and error != self.error:
                log('hyperiond refused a request: {}'.format(error.decode(errors='replace')),
                    xbmc.LOGWARNING)
                self.error = error
            if error is not None or registered == -1:
                self.registered = False
        return True

    def send(self, command_type, command):
        builder = self.builder
        Request.Start(builder)
        Request.AddCommandType(builder, command_type)
        Request.AddCommand(builder, command)
        builder.Finish(Request.End(builder))
        data = builder.Output()
        try:
            self.sock.sendall(len(data).to_bytes(4, 'big') + data)
            if self.receive():
                return True
        except OSError:
            pass
        self.lost()
        return False

    def register(self):
        builder = self.builder
        builder.Clear()
        origin = builder.CreateString(ORIGIN)
        Register.Start(builder)
        Register.AddOrigin(builder, origin)
        Register.AddPriority(builder, PRIORITY)
        self.registered = True
        return self.send(Command.Register, Register.End(builder))

    def image(self, rgb, width, height):
        builder = self.builder
        builder.Clear()
        data = builder.CreateByteVector(rgb)
        RawImage.Start(builder)
        RawImage.AddData(builder, data)
        RawImage.AddWidth(builder, width)
        RawImage.AddHeight(builder, height)
        raw = RawImage.End(builder)
        Image.Start(builder)
        Image.AddDataType(builder, ImageType.RawImage)
        Image.AddData(builder, raw)
        Image.AddDuration(builder, IMAGE_DURATION)
        return self.send(Command.Image, Image.End(builder))

    def run(self):
        while not self.abortRequested():
            started = time.monotonic()
            target = self.target()
            if target is None:
                self.capture = None
                self.disconnect()
            else:
                image = self.grab(target)
                now = time.monotonic()
                if image is not None and self.connect():
                    if not self.registered:
                        self.register()
                    if self.sock is not None and self.image(*image):
                        self.fed_at = now
                elif self.sock is not None and now - self.fed_at >= IMAGE_TIMEOUT:
                    self.disconnect()
            # a timeout of 0 waits forever
            if self.waitForAbort(max(MIN_WAIT, FRAME_INTERVAL - (time.monotonic() - started))):
                break
        self.disconnect()


if __name__ == '__main__':
    Feeder().run()
