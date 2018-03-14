#bdDetect.py
#A part of NonVisual Desktop Access (NVDA)
#This file is covered by the GNU General Public License.
#See the file COPYING for more details.
#Copyright (C) 2013-2017 NV Access Limited

"""Support for braille display detection.
This allows devices to be automatically detected and used when they become available,
as well as providing utilities to query for possible devices for a particular driver.
To support detection for a driver, devices need to be associated
using the C{add*} functions.
Drivers distributed with NVDA do this at the bottom of this module.
For drivers in add-ons, this must be done in a global plugin.
"""

import itertools
from collections import namedtuple, defaultdict, OrderedDict
import threading
import wx
import hwPortUtils
import braille
import winKernel
import core
import ctypes
from logHandler import log
import config
import time
import thread
from win32con import WM_DEVICECHANGE, DBT_DEVNODES_CHANGED
import appModuleHandler

_driverDevices = OrderedDict()

class DeviceMatch(
	namedtuple("DeviceMatch", ("type","id", "port", "deviceInfo"))
):
	"""Represents a detected device.
	@ivar id: The identifier of the device.
	@type id: unicode
	@ivar port: The port that can be used by a driver to communicate with a device.
	@type port: unicode
	@ivar deviceInfo: all known information about a device.
	@type deviceInfo: dict
	"""
	__slots__ = ()

# Device type constants
#: Key constant for HID devices
KEY_HID = "hid"
#: Key for serial devices (COM ports)
KEY_SERIAL = "serial"
#: Key for devices with a manufacturer specific driver
KEY_CUSTOM = "custom"
#: Key for bluetooth devices
KEY_BLUETOOTH = "bluetooth"

# Constants for USB and bluetooth detection to be used by the background thread scanner.
DETECT_USB = 1
DETECT_BLUETOOTH = 2

def _isDebug():
	return config.conf["debugLog"]["hwIo"]

def _getDriver(driver):
	try:
		return _driverDevices[driver]
	except KeyError:
		ret = _driverDevices[driver] = defaultdict(set)
		return ret

def addUsbDevices(driver, type, ids):
	"""Associate USB devices with a driver.
	@param driver: The name of the driver.
	@type driver: str
	@param type: The type of the driver, either C{KEY_HID}, C{KEY_SERIAL} or C{KEY_CUSTOM}.
	@type type: str
	@param ids: A set of USB IDs in the form C{"VID_xxxx&PID_XXXX"}.
	@type ids: set of str
	"""
	devs = _getDriver(driver)
	driverUsb = devs[type]
	driverUsb.update(ids)

def addBluetoothDevices(driver, matchFunc):
	"""Associate Bluetooth HID or COM ports with a driver.
	@param driver: The name of the driver.
	@type driver: str
	@param matchFunc: A function which determines whether a given Bluetooth device matches.
		It takes a L{DeviceMatch} as its only argument
		and returns a C{bool} indicating whether it matched.
	@type matchFunc: callable
	"""
	devs = _getDriver(driver)
	devs[KEY_BLUETOOTH] = matchFunc

def getDriversForConnectedUsbDevices():
	"""Get any matching drivers for connected USB devices.
	@return: Pairs of drivers and device information.
	@rtype: generator of (str, L{DeviceMatch}) tuples
	"""
	usbDevs = itertools.chain(
		(DeviceMatch(KEY_CUSTOM, port["usbID"], port["devicePath"], port)
			for port in hwPortUtils.listUsbDevices()),
		(DeviceMatch(KEY_HID, port["usbID"], port["devicePath"], port)
			for port in hwPortUtils.listHidDevices() if port["provider"]=="usb"),
		(DeviceMatch(KEY_SERIAL, port["usbID"], port["port"], port)
			for port in hwPortUtils.listComPorts() if "usbID" in port)
	)
	for match in usbDevs:
		for driver, devs in _driverDevices.iteritems():
			for type, ids in devs.iteritems():
				if match.type==type and match.id in ids:
					yield driver, match

def getDriversForPossibleBluetoothDevices():
	"""Get any matching drivers for possible Bluetooth devices.
	@return: Pairs of drivers and port information.
	@rtype: generator of (str, L{DeviceMatch}) tuples
	"""
	btDevs = itertools.chain(
		(DeviceMatch(KEY_SERIAL, port["bluetoothName"], port["port"], port)
			for port in hwPortUtils.listComPorts()
			if "bluetoothName" in port),
		(DeviceMatch(KEY_HID, port["hardwareID"], port["devicePath"], port)
			for port in hwPortUtils.listHidDevices() if port["provider"]=="bluetooth"),
	)
	for match in btDevs:
		for driver, devs in _driverDevices.iteritems():
			matchFunc = devs[KEY_BLUETOOTH]
			if not callable(matchFunc):
				continue
			if matchFunc(match):
				yield driver, match

class Detector(object):
	"""Automatically detect braille displays.
	This should only be used by the L{braille} module.
	"""

	def __init__(self):
		self._BgScanApc = winKernel.PAPCFUNC(self._bgScan)
		self._btComsLock = threading.Lock()
		self._btComs = None
		core.windowMessageReceived.register(self.handleWindowMessage)
		appModuleHandler.appSwitched.register(self.pollBluetoothDevices)
		self._stopEvent = threading.Event()
		self._queuedScanLock = threading.Lock()
		self._scanQueued = False
		# Perform initial scan.
		self._startBgScan(usb=True, bluetooth=True)

	def _startBgScan(self, usb=False, bluetooth=False):
		detectionParam = usb | bluetooth << 1
		with self._queuedScanLock:
			if not self._scanQueued:
				braille._BgThread.queueApc(self._BgScanApc, param=detectionParam)
				self._scanQueued = True

	def _stopBgScan(self):
		self._stopEvent.set()

	def _bgScan(self, param):
		# Clear the stop event before a scan is started.
		# Since a scan can take some time to complete, another thread can set the stop event to cancel it.
		self._stopEvent.clear()
		detectUsb = bool(param & DETECT_USB)
		detectBluetooth = bool(param & DETECT_BLUETOOTH)
		with self._queuedScanLock:
			self._scanQueued = False
		if detectUsb:
			if self._stopEvent.isSet():
				return
			for driver, match in getDriversForConnectedUsbDevices():
				if self._stopEvent.isSet():
					return
				if braille.handler.setDisplayByName(driver, detected=match):
					return
		if detectBluetooth:
			if self._stopEvent.isSet():
				return
			with self._btComsLock:
				if self._btComs is None:
					btComs = list(getDriversForPossibleBluetoothDevices())
					# Cache Bluetooth com ports for next time.
					btComsCache = []
				else:
					btComs = self._btComs
					btComsCache = btComs
			for driver, match in btComs:
				if self._stopEvent.isSet():
					return
				if btComsCache is not btComs:
					btComsCache.append((driver, match))
				if braille.handler.setDisplayByName(driver, detected=match):
					return
			if self._stopEvent.isSet():
				return
			if btComsCache is not btComs:
				with self._btComsLock:
					self._btComs = btComsCache

	def rescan(self):
		"""Stop a current scan when in progress, and start scanning from scratch."""
		self._stopBgScan()
		with self._btComsLock:
			# A Bluetooth com port might have been added.
			self._btComs = None
		self._startBgScan(usb=True, bluetooth=True)

	def handleWindowMessage(self, msg=None, wParam=None):
		if msg == WM_DEVICECHANGE and wParam == DBT_DEVNODES_CHANGED:
			self.rescan()

	def pollBluetoothDevices(self):
		"""Poll bluetooth devices that might be in range.
		This does not cancel the current scan and only queues a new scan when no scan is in progress."""
		with self._btComsLock:
			if not self._btComs:
				return
		self._startBgScan(bluetooth=True)

	def terminate(self):
		appModuleHandler.appSwitched.unregister(self.pollBluetoothDevices)
		core.windowMessageReceived.unregister(self.handleWindowMessage)
		self._stopBgScan()

def getConnectedUsbDevicesForDriver(driver):
	"""Get any connected USB devices associated with a particular driver.
	@param driver: The name of the driver.
	@type driver: str
	@return: Device information for each device.
	@rtype: generator of L{DeviceMatch}
	@raise LookupError: If there is no detection data for this driver.
	"""
	devs = _driverDevices[driver]
	usbDevs = itertools.chain(
		(DeviceMatch(KEY_CUSTOM, port["usbID"], port["devicePath"], port)
		for port in hwPortUtils.listUsbDevices()),
		(DeviceMatch(KEY_HID, port["usbID"], port["devicePath"], port)
		for port in hwPortUtils.listHidDevices() if port["provider"]=="usb"),
		(DeviceMatch(KEY_SERIAL, port["usbID"], port["port"], port)
		for port in hwPortUtils.listComPorts() if "usbID" in port)
	)
	for match in usbDevs:
		for type, ids in devs.iteritems():
			if match.type==type and match.id in ids:
				yield match

def getPossibleBluetoothDevicesForDriver(driver):
	"""Get any possible Bluetooth devices associated with a particular driver.
	@param driver: The name of the driver.
	@type driver: str
	@return: Port information for each port.
	@rtype: generator of L{DeviceMatch}
	@raise LookupError: If there is no detection data for this driver.
	"""
	matchFunc = _driverDevices[driver][KEY_BLUETOOTH]
	if not callable(matchFunc):
		return
	btDevs = itertools.chain(
		(DeviceMatch(KEY_SERIAL, port["bluetoothName"], port["port"], port)
			for port in hwPortUtils.listComPorts()
			if "bluetoothName" in port),
		(DeviceMatch(KEY_HID, port["hardwareID"], port["devicePath"], port)
			for port in hwPortUtils.listHidDevices() if port["provider"]=="bluetooth"),
	)
	for match in btDevs:
		if matchFunc(match):
			yield match

def driverHasPossibleDevices(driver):
	"""Determine whether there are any possible devices associated with a given driver.
	@param driver: The name of the driver.
	@type driver: str
	@return: C{True} if there are possible devices, C{False} otherwise.
	@rtype: bool
	@raise LookupError: If there is no detection data for this driver.
	"""
	return bool(next(itertools.chain(
		getConnectedUsbDevicesForDriver(driver),
		getPossibleBluetoothDevicesForDriver(driver)
	), None))

### Detection data
# alva
addUsbDevices("alva", KEY_HID, {
	"VID_0798&PID_0640", # BC640
	"VID_0798&PID_0680", # BC680
	"VID_0798&PID_0699", # USB protocol converter
})

addBluetoothDevices("alva", lambda m: m.id.startswith("ALVA "))

# baum
addUsbDevices("baum", KEY_HID, {
	"VID_0904&PID_3001", # RefreshaBraille 18
	"VID_0904&PID_6101", # VarioUltra 20
	"VID_0904&PID_6103", # VarioUltra 32
	"VID_0904&PID_6102", # VarioUltra 40
	"VID_0904&PID_4004", # Pronto! 18 V3
	"VID_0904&PID_4005", # Pronto! 40 V3
	"VID_0904&PID_4007", # Pronto! 18 V4
	"VID_0904&PID_4008", # Pronto! 40 V4
	"VID_0904&PID_6001", # SuperVario2 40
	"VID_0904&PID_6002", # SuperVario2 24
	"VID_0904&PID_6003", # SuperVario2 32
	"VID_0904&PID_6004", # SuperVario2 64
	"VID_0904&PID_6005", # SuperVario2 80
	"VID_0904&PID_6006", # Brailliant2 40
	"VID_0904&PID_6007", # Brailliant2 24
	"VID_0904&PID_6008", # Brailliant2 32
	"VID_0904&PID_6009", # Brailliant2 64
	"VID_0904&PID_600A", # Brailliant2 80
	"VID_0904&PID_6201", # Vario 340
	"VID_0483&PID_A1D3", # Orbit Reader 20
})

addUsbDevices("baum", KEY_SERIAL, {
	"VID_0403&PID_FE70", # Vario 40
	"VID_0403&PID_FE71", # PocketVario
	"VID_0403&PID_FE72", # SuperVario/Brailliant 40
	"VID_0403&PID_FE73", # SuperVario/Brailliant 32
	"VID_0403&PID_FE74", # SuperVario/Brailliant 64
	"VID_0403&PID_FE75", # SuperVario/Brailliant 80
	"VID_0904&PID_2001", # EcoVario 24
	"VID_0904&PID_2002", # EcoVario 40
	"VID_0904&PID_2007", # VarioConnect/BrailleConnect 40
	"VID_0904&PID_2008", # VarioConnect/BrailleConnect 32
	"VID_0904&PID_2009", # VarioConnect/BrailleConnect 24
	"VID_0904&PID_2010", # VarioConnect/BrailleConnect 64
	"VID_0904&PID_2011", # VarioConnect/BrailleConnect 80
	"VID_0904&PID_2014", # EcoVario 32
	"VID_0904&PID_2015", # EcoVario 64
	"VID_0904&PID_2016", # EcoVario 80
	"VID_0904&PID_3000", # RefreshaBraille 18
})

addBluetoothDevices("baum", lambda m: any(m.id.startswith(prefix) for prefix in (
	"Baum SuperVario",
	"Baum PocketVario",
	"Baum SVario",
	"HWG Brailliant",
	"Refreshabraille",
	"VarioConnect",
	"BrailleConnect",
	"Pronto!",
	"VarioUltra",
	"Orbit Reader 20",
)))

# brailleNote
addUsbDevices("brailleNote", KEY_SERIAL, {
	"VID_1C71&PID_C004", # Apex
})
addBluetoothDevices("brailleNote", lambda m:
	any(first <= m.deviceInfo.get("bluetoothAddress",0) <= last for first, last in (
		(0x0025EC000000, 0x0025EC01869F), # Apex
	)) or m.id.startswith("Braillenote"))

# brailliantB
addUsbDevices("brailliantB", KEY_HID, {
	"VID_1C71&PID_C006", # Brailliant BI 32, 40 and 80
	"VID_1C71&PID_C022", # Brailliant BI 14
	"VID_1C71&PID_C00A", # BrailleNote Touch
})
addUsbDevices("brailliantB", KEY_SERIAL, {
	"Vid_1c71&Pid_c005", # Brailliant BI 32, 40 and 80
	"Vid_1c71&Pid_c021", # Brailliant BI 14
})
addBluetoothDevices("brailliantB", lambda m:
	m.id.startswith("Brailliant B") or m.id == "Brailliant 80")

# eurobraille
addUsbDevices("eurobraille", KEY_HID, {
	"VID_C251&PID_1122", # Esys (version < 3.0, no SD card
	"VID_C251&PID_1123", # Esys (version >= 3.0, with HID keyboard, no SD card
	"VID_C251&PID_1124", # Esys (version < 3.0, with SD card
	"VID_C251&PID_1125", # Esys (version >= 3.0, with HID keyboard, with SD card
	"VID_C251&PID_1126", # Esys (version >= 3.0, no SD card
	"VID_C251&PID_1127", # Reserved
	"VID_C251&PID_1128", # Esys (version >= 3.0, with SD card
	"VID_C251&PID_1129", # Reserved
	"VID_C251&PID_112A", # Reserved
	"VID_C251&PID_112B", # Reserved
	"VID_C251&PID_112C", # Reserved
	"VID_C251&PID_112D", # Reserved
	"VID_C251&PID_112E", # Reserved
	"VID_C251&PID_112F", # Reserved
	"VID_C251&PID_1130", # Esytime
	"VID_C251&PID_1131", # Reserved
	"VID_C251&PID_1132", # Reserved
})

addBluetoothDevices("eurobraille", lambda m: m.id.startswith("Esys"))

# handyTech
addUsbDevices("handyTech", KEY_SERIAL, {
	"VID_0403&PID_6001", # FTDI chip
	"VID_0921&PID_1200", # GoHubs chip
})

# Newer Handy Tech displays have a native HID processor
addUsbDevices("handyTech", KEY_HID, {
	"VID_1FE4&PID_0054", # Active Braille
	"VID_1FE4&PID_0081", # Basic Braille 16
	"VID_1FE4&PID_0082", # Basic Braille 20
	"VID_1FE4&PID_0083", # Basic Braille 32
	"VID_1FE4&PID_0084", # Basic Braille 40
	"VID_1FE4&PID_008A", # Basic Braille 48
	"VID_1FE4&PID_0086", # Basic Braille 64
	"VID_1FE4&PID_0087", # Basic Braille 80
	"VID_1FE4&PID_008B", # Basic Braille 160
	"VID_1FE4&PID_0061", # Actilino
	"VID_1FE4&PID_0064", # Active Star 40
})

# Some older HT displays use a HID converter and an internal serial interface
addUsbDevices("handyTech", KEY_HID, {
	"VID_1FE4&PID_0003", # USB-HID adapter
	"VID_1FE4&PID_0074", # Braille Star 40
	"VID_1FE4&PID_0044", # Easy Braille
})

addBluetoothDevices("handyTech", lambda m: any(m.id.startswith(prefix) for prefix in (
	"Actilino AL",
	"Active Braille AB",
	"Active Star AS",
	"Basic Braille BB",
	"Braille Star 40 BS",
	"Braille Wave BW",
	"Easy Braille EBR",
)))

# hims
# Bulk devices
addUsbDevices("hims", KEY_CUSTOM, {
	"VID_045E&PID_930A", # Braille Sense & Smart Beetle
	"VID_045E&PID_930B", # Braille EDGE 40
})

# Sync Braille, serial device
addUsbDevices("hims", KEY_SERIAL, {
	"VID_0403&PID_6001",
})

addBluetoothDevices("hims", lambda m: any(m.id.startswith(prefix) for prefix in (
	"BrailleSense",
	"BrailleEDGE",
	"SmartBeetle",
)))

# superBrl
addUsbDevices("superBrl", KEY_SERIAL, {
	"VID_10C4&PID_EA60", # SuperBraille 3.2
})