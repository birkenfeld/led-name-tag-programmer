#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

import pickle
import struct
import subprocess
import sys
import time
from os import path

from PyQt5 import Qt, uic
from PyQt5.QtCore import pyqtSlot, pyqtSignal

# Some general info:
#
# The display is configured by sending one or more packets to its USB
# device on the interrupt endpoint 1.  (It registers as a HID device but
# does not seem to do anything HID-related.)
#
# The first USB packet has the full 64 byte length and contains a header with
# info about all 8 messages: animations, speed, other modes, and how the
# following bitmaps are structured.  (There is also a timestamp in a funky
# format, I don't really know what the device can do with it.)
#
# Following are as many packets as necessary where all active bitmaps are
# simply concatenated.

MAGIC = b'wang'

HEADER = struct.Struct(
    '>'
    '4s'  # magic number
    '2x'
    'BB'  # flash and border bits
    '8B'  # 1 speed/mode byte per message
    '8H'  # 1 length word per message
    '4x'
    'Q'   # timestamp
    '20x'
)

# Number of messages possible.
MESSAGES = 8

# Number of bits per byte.  This can't really change :)
BPB = 8

# Apparently other sizes are possible.  Until I get to order and test one,
# let's keep these as constants.
HEIGHT = 11
WIDTH = 44

# Size of LED pixels and grid border in the preview.
PREV_LED = 6
PREV_GRID = 2
PREV_PIXEL = PREV_LED + PREV_GRID

# Possible selectable animations.
ANIMS = ['Left', 'Right', 'Up', 'Down',
         'Freeze', 'Animate', 'Pileup', 'Split',
         'Laser', 'Smooth', 'Rotate']


class Bitmap(object):
    """Bitmap which keeps data in the bytestream format.

    Each bitmap's width comes in units of 8 pixels, since 8 pixels in a line
    are stuffed into one byte.  The bitmap is a sequence of 11 (or HEIGHT) such
    bytes at a time.

    This is different from the usual format (complete scanlines being
    contiguous in memory), so we convert to that here.
    """

    def __init__(self, obj=b'', width=None):
        if isinstance(obj, Qt.QImage):
            assert obj.format() == Qt.QImage.Format_Mono
            width_bytes = (width + 7) // BPB
            stride = obj.bytesPerLine()
            array = obj.bits().asarray(stride * HEIGHT)
            self.data = bytes(array[row*stride + col]
                              for col in range(width_bytes)
                              for row in range(HEIGHT))
        else:
            self.data = obj

    def __bool__(self):
        return bool(self.data)

    @property
    def width(self):
        return BPB * (len(self.data) // HEIGHT)

    @property
    def width_bytes(self):
        return len(self.data) // HEIGHT

    def byte_pixels(self, i):
        for (row, data) in enumerate(self.data[HEIGHT*i:HEIGHT*(i+1)]):
            for col in range(BPB):
                if data & (1 << (7 - col)):
                    yield (col, row)


class Message(object):
    """Represents a single message.

    It can be in one of two modes: if self.bitmap is None, it is generated
    on the fly from self.text and associated font settings.  Otherwise the
    bitmap is used directly.
    """

    def __init__(self):
        self.active = False
        self.flash = False
        self.border = False
        self.anim = 0
        self.speed = 3
        self.bitmap = None
        self.text = ''
        self.font = 'Sans'
        self.offset = 0

    def genBitmap(self):
        if self.bitmap is not None:
            return self.bitmap
        font = Qt.QFont()
        font.fromString(self.font)
        # for some reason the bounding rect calculated here is wrong by quite a
        # bit, so let's use twice as a rough estimation
        width = 2 * Qt.QFontMetrics(font).boundingRect(self.text).width()
        if not width:
            return Bitmap()
        image = Qt.QImage(width, HEIGHT, Qt.QImage.Format_Mono)
        image.fill(0)
        with Qt.QPainter(image) as painter:
            # no antialiasing
            painter.setRenderHints(Qt.QPainter.RenderHints())
            painter.setFont(font)
            painter.setPen(Qt.QPen(Qt.QColor('white')))
            # here we get the real width of the drawn text
            real_width = painter.drawText(0, -self.offset,
                                          width, HEIGHT + self.offset,
                                          Qt.Qt.AlignTop | Qt.Qt.AlignLeft,
                                          self.text).width()
        return Bitmap(image, real_width)


class Design(object):
    """Represents all data that can be transferred to the display.

    This object is pickled to save a design as a .leddesign file.
    """

    def __init__(self):
        self.msgs = [Message() for _ in range(MESSAGES)]

    def genBytestream(self):
        t = time.localtime()
        timestamp = ((t.tm_year - 1999) << 40 | t.tm_mon << 32 |
                     t.tm_mday << 24 | t.tm_hour << 16 |
                     t.tm_min << 8 | t.tm_sec)
        bitmaps = [msg.genBitmap() if msg.active else Bitmap()
                   for msg in self.msgs]
        if all(not b for b in bitmaps):
            return b''
        header = HEADER.pack(
            MAGIC,
            sum(msg.flash << i for (i, msg) in enumerate(self.msgs)),
            sum(msg.border << i for (i, msg) in enumerate(self.msgs)),
            *(msg.speed << 4 | msg.anim for msg in self.msgs),
            *(bmp.width_bytes for bmp in bitmaps),
            timestamp)
        return b''.join([header] + [bmp.data for bmp in bitmaps])


class Preview(Qt.QWidget):
    """The widget used to draw the preview of the currently edited message."""

    def __init__(self, parent):
        Qt.QWidget.__init__(self, parent)
        self.bitmap = Bitmap()
        # we draw each pixel as 6x6 with a 2px wide grid inbetween
        self.setMinimumHeight(PREV_PIXEL*HEIGHT + PREV_GRID)
        self.setMaximumHeight(PREV_PIXEL*HEIGHT + PREV_GRID)
        self._grid = []
        self._background = Qt.QColor('black')
        self._gridcolor = Qt.QColor('#666666')
        self._ledcolor = Qt.QColor('#ff9900')
        self._endcolor = Qt.QColor('#00aa00')
        self._stopcolor = Qt.QColor('#ff0000')
        # horizontal view offset (in 8-pixel units), controlled by scrollbar
        self.offset = 0

    def _gridline_horz(self, n, w):
        return Qt.QLineF(0, PREV_PIXEL*n + PREV_GRID//2,
                         w, PREV_PIXEL*n + PREV_GRID//2)

    def _gridline_vert(self, n):
        return Qt.QLineF(
            PREV_PIXEL*n + PREV_GRID//2, 0,
            PREV_PIXEL*n + PREV_GRID//2, PREV_PIXEL*HEIGHT + PREV_GRID)

    def resizeEvent(self, event):
        w = self.width()
        # the always-visible grid lines can be generated here, since they
        # don't change for every paint event
        self._grid = \
            [self._gridline_vert(i) for i in range(w//PREV_PIXEL + 1)] + \
            [self._gridline_horz(i, w) for i in range(HEIGHT + 1)]

    def paintEvent(self, event):
        painter = Qt.QPainter(self)
        painter.fillRect(painter.window(), self._background)
        # draw basic grid
        painter.setPen(Qt.QPen(self._gridcolor, PREV_GRID))
        painter.drawLines(self._grid)
        # draw lit up pixels
        byte = -1
        for byte, ix in enumerate(range(self.offset, self.bitmap.width_bytes)):
            for (x, y) in self.bitmap.byte_pixels(ix):
                painter.fillRect(
                    Qt.QRect(PREV_GRID + PREV_PIXEL*(BPB*byte + x),
                             PREV_GRID + PREV_PIXEL*y, PREV_LED, PREV_LED),
                    self._ledcolor)
        # draw end of display
        painter.setPen(Qt.QPen(self._stopcolor, PREV_GRID))
        painter.drawLine(self._gridline_vert(WIDTH - BPB*self.offset))
        # draw end of used display area
        painter.setPen(Qt.QPen(self._endcolor, PREV_GRID))
        painter.drawLine(self._gridline_vert(BPB*(byte + 1)))


class MessageEditor(Qt.QWidget):
    """One of the 8 identical widgets that edit the properties of a message."""

    changed = pyqtSignal()

    def __init__(self, number, parent):
        Qt.QWidget.__init__(self, parent)
        uic.loadUi('message.ui', self)
        self.numberLbl.setText(str(number))
        self.animBox.addItems(ANIMS)
        self.font = self.font()
        self.bitmap = None

    def on_bmpBox_toggled(self, on):
        self.stacker.setCurrentIndex(int(on))
        if not on:
            self.bitmap = None
        else:
            self.bitmap = Bitmap()
        self.changed.emit()

    @pyqtSlot()
    def on_bmpLoadBtn_clicked(self):
        settings = Qt.QSettings()
        default_dir = settings.value('imgdir', '') or ''
        fn, ok = Qt.QFileDialog.getOpenFileName(
            self, 'Select bitmap file', default_dir,
            'Image files (*.bmp *.png *.jpg *.gif *.xbm '
            '*.xpm *.pbm *.pgm *.ppm);;All (*)')
        if not ok:
            return
        settings.setValue('imgdir', path.dirname(fn))
        img = Qt.QImage(fn)
        if img.isNull():
            Qt.QMessageBox.warning(self, 'Error', 'Image could not be read.')
            return
        img.convertToFormat(Qt.QImage.Format_Mono, Qt.Qt.MonoOnly)
        # make sure we have at least the required height
        if img.height() < HEIGHT:
            img = img.copy(0, 0, img.width(), HEIGHT)
        img.invertPixels()
        self.bitmap = Bitmap(img, min(img.width(), 4096))
        self.changed.emit()

    @pyqtSlot()
    def on_fontBtn_clicked(self):
        fnt, ok = Qt.QFontDialog.getFont(self.font, self)
        if ok:
            self.font = fnt
        self.changed.emit()


class MainWindow(Qt.QMainWindow):
    def __init__(self):
        Qt.QMainWindow.__init__(self)
        uic.loadUi('main.ui', self)

        self.design = Design()

        self._delay_update = False
        self.msgEditors = []
        for i in range(MESSAGES):
            widget = MessageEditor(i + 1, self)
            widget.changed.connect(lambda i=i: self.updateDesign(i))
            self.msgBox.layout().addWidget(widget)
            self.msgEditors.append(widget)
        self.preview = Preview(self)
        self.prevBox.layout().insertWidget(0, self.preview)

        settings = Qt.QSettings()
        self.restoreGeometry(settings.value('geometry', '', Qt.QByteArray))

    def closeEvent(self, event):
        settings = Qt.QSettings()
        settings.setValue('geometry', self.saveGeometry())
        return Qt.QMainWindow.closeEvent(self, event)

    @pyqtSlot()
    def on_loadBtn_clicked(self):
        settings = Qt.QSettings()
        default_dir = settings.value('dir', '') or ''
        fn, ok = Qt.QFileDialog.getOpenFileName(
            self, 'Select save file', default_dir,
            'LED design files (*.leddesign);;All (*)')
        if not ok:
            return
        settings.setValue('dir', path.dirname(fn))
        self.loadDesign(fn)

    def loadDesign(self, fn):
        try:
            with open(fn, 'rb') as fp:
                self.design = pickle.load(fp)
        except Exception as err:
            Qt.QMessageBox.warning(self, 'Error', 'Load failed: %s' % err)
        else:
            self.updateEditors()
            self.updatePreview(0)

    @pyqtSlot()
    def on_saveBtn_clicked(self):
        settings = Qt.QSettings()
        default_dir = settings.value('dir', '') or ''
        fn, ok = Qt.QFileDialog.getSaveFileName(
            self, 'Select save file', default_dir,
            'LED design files (*.leddesign);;All (*)')
        if not ok:
            return
        settings.setValue('dir', path.dirname(fn))
        try:
            with open(fn, 'wb') as fp:
                pickle.dump(self.design, fp, pickle.HIGHEST_PROTOCOL)
        except Exception as err:
            Qt.QMessageBox.warning(self, 'Error', 'Save failed: %s' % err)

    def _get_bytestream(self):
        bytestream = self.design.genBytestream()
        if not bytestream:
            Qt.QMessageBox.information(
                self, 'Error',
                'Nothing to program (you need to check the "Active" boxes).')
            return None
        if len(bytestream) > 4096+64:
            Qt.QMessageBox.information(self, 'Error', 'Too much data!')
            return None
        return bytestream

    @pyqtSlot()
    def on_byteSaveBtn_clicked(self):
        bytestream = self._get_bytestream()
        if bytestream is None:
            return
        settings = Qt.QSettings()
        default_dir = settings.value('leddir', '') or ''
        fn, ok = Qt.QFileDialog.getSaveFileName(
            self, 'Select save file', default_dir,
            'LED bytestream files (*.led);;All (*)')
        if not ok:
            return
        settings.setValue('leddir', path.dirname(fn))
        try:
            open(fn, 'wb').write(bytestream)
        except Exception as err:
            Qt.QMessageBox.warning(self, 'Error', 'Save failed: %s' % err)

    @pyqtSlot()
    def on_progBtn_clicked(self):
        bytestream = self._get_bytestream()
        if bytestream is None:
            return
        proc = subprocess.Popen([sys.executable, 'program.py', '-'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        output = proc.communicate(bytestream)
        if output[0]:
            Qt.QMessageBox.warning(self, 'Error occurred',
                                   output[0].decode('latin1'))

    def updateEditors(self):
        """Update the editor widgets with new design data."""
        # prevent the widgets triggering an update in the other direction
        self._delay_update = True
        try:
            for (msg, editor) in zip(self.design.msgs, self.msgEditors):
                editor.activeBox.setChecked(msg.active)
                editor.flashBox.setChecked(msg.flash)
                editor.borderBox.setChecked(msg.border)
                editor.animBox.setCurrentIndex(msg.anim)
                editor.speedBox.setValue(msg.speed + 1)
                editor.offsetBox.setValue(msg.offset)
                editor.textEdit.setText(msg.text)
                editor.font.fromString(msg.font)
                editor.bmpBox.setChecked(msg.bitmap is not None)
                editor.bitmap = msg.bitmap
        finally:
            self._delay_update = False

    def updateDesign(self, i):
        """Update the i-th message with new data from the editor."""
        if self._delay_update:
            return
        editor = self.msgEditors[i]
        msg = self.design.msgs[i]
        msg.active = editor.activeBox.isChecked()
        msg.flash = editor.flashBox.isChecked()
        msg.border = editor.borderBox.isChecked()
        msg.anim = editor.animBox.currentIndex()
        msg.speed = editor.speedBox.value() - 1
        msg.offset = editor.offsetBox.value()
        msg.text = editor.textEdit.text()
        msg.font = editor.font.toString()
        msg.bitmap = editor.bitmap
        self.updatePreview(i)

    def updatePreview(self, i):
        """Update the bitmap to be drawn in the preview from message i."""
        if self._delay_update:
            return
        msg = self.design.msgs[i]
        self.prevBox.setTitle('Preview: Message %s' % (i + 1))
        bitmap = msg.genBitmap()
        self.preview.bitmap = bitmap
        # if the message bitmap is wider than the available horizontal
        # preview space, activate the scrollbar
        bytes_visible = self.preview.width() // 64
        max_scroll = max(bitmap.width_bytes - bytes_visible, 0)
        self.prevScroll.setRange(0, max_scroll)
        self.preview.update()

    def on_prevScroll_valueChanged(self, value):
        self.preview.offset = value
        self.preview.update()


if __name__ == '__main__':
    app = Qt.QApplication([])
    app.setOrganizationName('gb')
    app.setApplicationName('ledtag')
    main = MainWindow()
    if len(sys.argv) > 1:
        main.loadDesign(sys.argv[1])
    main.show()
    app.exec_()
