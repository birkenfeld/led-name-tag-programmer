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

import argparse
import sys

import usb

VENDOR_ID  = 0x0416  # Winbond... :)
PRODUCT_ID = 0x5020


def error(msg):
    print('Error:', msg, file=sys.stderr)
    sys.exit(1)


parser = argparse.ArgumentParser(
    description='Uploads a configuration to a 11x44 monochrome LED name tag')
parser.add_argument('config', help='File with the byte stream to configure, '
                    'or "-" to read from stdin')

args = parser.parse_args()

if args.config == '-':
    bytestream = sys.stdin.buffer.read()
else:
    bytestream = open(args.config, 'rb').read()

dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
if dev is None:
    error('Did not find a name tag device')
try:
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
    interface = dev.configurations()[0].interfaces()[0]
    cfg = dev.get_active_configuration()
    intf = cfg[(0,0)]
    endpoint = usb.util.find_descriptor(
        intf,
        # match the first OUT endpoint
        custom_match = \
        lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == \
            usb.util.ENDPOINT_OUT)
except Exception as err:
    error('Could not configure the device: %s' % err)
try:
    endpoint.write(bytestream)
except Exception as err:
    error('Could not write to the device: %s' % err)
