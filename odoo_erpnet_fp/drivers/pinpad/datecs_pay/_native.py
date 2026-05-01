#!/usr/bin/env python3
"""
Python wrapper for DatecsPay pinpad driver
Uses ctypes to interface with the C library
"""

import ctypes
import os
from typing import Optional, Tuple, List
from dataclasses import dataclass
from datetime import datetime

# Load the shared library — bundled at lib/libdatecs_pinpad.so inside
# the package, with system-paths fallback. We defer hard failure until
# the lib is actually used so that simply importing the package (e.g.
# during CI / pure-Python tests) does not require the .so.
_lib_path = os.path.join(os.path.dirname(__file__), 'lib', 'libdatecs_pinpad.so')
_LIB_LOAD_ERROR = None
try:
    _lib = ctypes.CDLL(_lib_path)
except OSError as _err1:
    try:
        _lib = ctypes.CDLL('libdatecs_pinpad.so')
    except OSError as _err2:
        _lib = None
        _LIB_LOAD_ERROR = _err2


def _require_lib():
    if _lib is None:
        raise RuntimeError(
            f"libdatecs_pinpad.so could not be loaded: {_LIB_LOAD_ERROR}. "
            "Bundle it under datecs_pay/lib/ or install on the system."
        )

# Constants
DATECS_ERR_NO_ERR = 0
DATECS_ERR_TIMEOUT = 9
DATECS_ERR_BUSY = 38

# Transaction types
TRANS_PURCHASE = 0x01
TRANS_PURCHASE_CASHBACK = 0x02
TRANS_PURCHASE_REFERENCE = 0x03
TRANS_CASH_ADVANCE = 0x04
TRANS_AUTHORIZATION = 0x05
TRANS_PURCHASE_CODE = 0x06
TRANS_VOID_PURCHASE = 0x07
TRANS_VOID_CASH_ADVANCE = 0x08
TRANS_VOID_AUTHORIZATION = 0x09
TRANS_END_OF_DAY = 0x0A
TRANS_LOYALTY_BALANCE = 0x0B
TRANS_LOYALTY_SPEND = 0x0C
TRANS_VOID_LOYALTY_SPEND = 0x0D
TRANS_TEST_CONNECTION = 0x0E
TRANS_TMS_UPDATE = 0x0F

# TLV Tags
TAG_AMOUNT = 0x81
TAG_CASHBACK = 0x9F04
TAG_RRN = 0xDF01
TAG_AUTH_ID = 0xDF02
TAG_REFERENCE = 0xDF03
TAG_TIP = 0xDF63
TAG_TRANS_RESULT = 0xDF05
TAG_TRANS_ERROR = 0xDF06
TAG_HOST_RRN = 0xDF07
TAG_HOST_AUTH_ID = 0xDF08
TAG_TERMINAL_ID = 0x9F1C
TAG_MERCHANT_ID = 0x9F16
TAG_TRANS_TYPE = 0xDF10
TAG_TRANS_DATE = 0x9A
TAG_TRANS_TIME = 0x9F21
TAG_STAN = 0x9F41


# C structures
class DatecsDateTime(ctypes.Structure):
    _fields_ = [
        ('year', ctypes.c_uint8),
        ('month', ctypes.c_uint8),
        ('day', ctypes.c_uint8),
        ('hour', ctypes.c_uint8),
        ('minute', ctypes.c_uint8),
        ('second', ctypes.c_uint8)
    ]


class DatecsPinpadInfo(ctypes.Structure):
    _fields_ = [
        ('model_name', ctypes.c_char * 21),
        ('serial_number', ctypes.c_char * 11),
        ('software_version', ctypes.c_uint8 * 4),
        ('terminal_id', ctypes.c_char * 9),
        ('menu_type', ctypes.c_uint8)
    ]


class DatecsPinpadStatus(ctypes.Structure):
    _fields_ = [
        ('reversal_status', ctypes.c_uint8),
        ('end_day_required', ctypes.c_uint8)
    ]


# Function signatures — only set when the library actually loaded.
if _lib is not None:
    _lib.datecs_device_create.argtypes = [ctypes.c_char_p, ctypes.c_int]
    _lib.datecs_device_create.restype = ctypes.c_void_p

    _lib.datecs_device_destroy.argtypes = [ctypes.c_void_p]
    _lib.datecs_device_destroy.restype = None

    _lib.datecs_device_open.argtypes = [ctypes.c_void_p]
    _lib.datecs_device_open.restype = ctypes.c_int

    _lib.datecs_device_close.argtypes = [ctypes.c_void_p]
    _lib.datecs_device_close.restype = ctypes.c_int

    _lib.datecs_ping.argtypes = [ctypes.c_void_p]
    _lib.datecs_ping.restype = ctypes.c_int

    _lib.datecs_get_pinpad_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(DatecsPinpadInfo)]
    _lib.datecs_get_pinpad_info.restype = ctypes.c_int

    _lib.datecs_get_pinpad_status.argtypes = [ctypes.c_void_p, ctypes.POINTER(DatecsPinpadStatus)]
    _lib.datecs_get_pinpad_status.restype = ctypes.c_int

    _lib.datecs_start_transaction.argtypes = [
        ctypes.c_void_p, ctypes.c_uint8, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint16
    ]
    _lib.datecs_start_transaction.restype = ctypes.c_int

    _lib.datecs_end_transaction.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    _lib.datecs_end_transaction.restype = ctypes.c_int

    _lib.datecs_get_receipt_tags.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint8,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)), ctypes.POINTER(ctypes.c_uint16)
    ]
    _lib.datecs_get_receipt_tags.restype = ctypes.c_int

    _lib.datecs_get_rtc.argtypes = [ctypes.c_void_p, ctypes.POINTER(DatecsDateTime)]
    _lib.datecs_get_rtc.restype = ctypes.c_int

    _lib.datecs_set_rtc.argtypes = [ctypes.c_void_p, ctypes.POINTER(DatecsDateTime)]
    _lib.datecs_set_rtc.restype = ctypes.c_int

    _lib.datecs_tlv_find_tag.argtypes = [
        ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_uint16,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)), ctypes.POINTER(ctypes.c_uint8)
    ]
    _lib.datecs_tlv_find_tag.restype = ctypes.c_int

    _lib.datecs_tlv_get_uint32.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint8]
    _lib.datecs_tlv_get_uint32.restype = ctypes.c_uint32

    _lib.datecs_error_string.argtypes = [ctypes.c_int]
    _lib.datecs_error_string.restype = ctypes.c_char_p


@dataclass
class PinpadInfo:
    """Pinpad device information"""
    model_name: str
    serial_number: str
    software_version: Tuple[int, int, int, int]
    terminal_id: str
    menu_type: int


@dataclass
class PinpadStatus:
    """Pinpad device status"""
    reversal_status: int
    end_day_required: bool
    
    @property
    def has_reversal(self) -> bool:
        return self.reversal_status in (ord('R'), ord('C'))
    
    @property
    def has_hang_transaction(self) -> bool:
        return self.reversal_status == ord('C')


class DatecsPinpadDriver:
    """Python interface to DatecsPay pinpad driver"""
    
    def __init__(self, port: str, baudrate: int = 115200):
        """
        Initialize pinpad driver
        
        Args:
            port: Serial port path (e.g., '/dev/ttyUSB0')
            baudrate: Serial baudrate (default: 115200)
        """
        self.port = port
        self.baudrate = baudrate
        self._device = None
        self._is_open = False
    
    def open(self) -> None:
        """Open connection to pinpad device"""
        if self._is_open:
            return
        _require_lib()

        port_bytes = self.port.encode('utf-8')
        self._device = _lib.datecs_device_create(port_bytes, self.baudrate)
        if not self._device:
            raise RuntimeError("Failed to create device")
        
        ret = _lib.datecs_device_open(self._device)
        if ret < 0:
            _lib.datecs_device_destroy(self._device)
            self._device = None
            raise RuntimeError(f"Failed to open device: {self.port}")
        
        self._is_open = True
    
    def close(self) -> None:
        """Close connection to pinpad device"""
        if not self._is_open:
            return
        
        if self._device:
            _lib.datecs_device_close(self._device)
            _lib.datecs_device_destroy(self._device)
            self._device = None
        
        self._is_open = False
    
    def __enter__(self):
        """Context manager entry"""
        self.open()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()
        return False
    
    def __del__(self):
        """Destructor"""
        self.close()
    
    def ping(self) -> bool:
        """
        Ping the device to check if it's connected
        
        Returns:
            True if device responds, False otherwise
        """
        if not self._is_open:
            return False
        
        ret = _lib.datecs_ping(self._device)
        return ret == 0
    
    def get_info(self) -> PinpadInfo:
        """
        Get pinpad device information
        
        Returns:
            PinpadInfo object with device details
        """
        if not self._is_open:
            raise RuntimeError("Device not open")
        
        info = DatecsPinpadInfo()
        ret = _lib.datecs_get_pinpad_info(self._device, ctypes.byref(info))
        
        if ret != 0:
            raise RuntimeError(f"Failed to get pinpad info: {self._error_string(ret)}")
        
        return PinpadInfo(
            model_name=info.model_name.decode('utf-8').rstrip('\x00'),
            serial_number=info.serial_number.decode('utf-8').rstrip('\x00'),
            software_version=tuple(info.software_version),
            terminal_id=info.terminal_id.decode('utf-8').rstrip('\x00'),
            menu_type=info.menu_type
        )
    
    def get_status(self) -> PinpadStatus:
        """
        Get pinpad device status
        
        Returns:
            PinpadStatus object with device status
        """
        if not self._is_open:
            raise RuntimeError("Device not open")
        
        status = DatecsPinpadStatus()
        ret = _lib.datecs_get_pinpad_status(self._device, ctypes.byref(status))
        
        if ret != 0:
            raise RuntimeError(f"Failed to get pinpad status: {self._error_string(ret)}")
        
        return PinpadStatus(
            reversal_status=status.reversal_status,
            end_day_required=bool(status.end_day_required)
        )
    
    def start_transaction(self, trans_type: int, params: Optional[bytes] = None) -> None:
        """
        Start a transaction
        
        Args:
            trans_type: Transaction type (TRANS_PURCHASE, etc.)
            params: Optional TLV-encoded transaction parameters
        """
        if not self._is_open:
            raise RuntimeError("Device not open")
        
        if params:
            params_array = (ctypes.c_uint8 * len(params))(*params)
            ret = _lib.datecs_start_transaction(
                self._device, trans_type, params_array, len(params)
            )
        else:
            ret = _lib.datecs_start_transaction(
                self._device, trans_type, None, 0
            )
        
        if ret != 0:
            raise RuntimeError(f"Failed to start transaction: {self._error_string(ret)}")
    
    def end_transaction(self, success: bool = True) -> None:
        """
        End a transaction
        
        Args:
            success: True for successful completion, False for failure
        """
        if not self._is_open:
            raise RuntimeError("Device not open")
        
        ret = _lib.datecs_end_transaction(self._device, success)
        
        if ret != 0:
            raise RuntimeError(f"Failed to end transaction: {self._error_string(ret)}")
    
    def get_receipt_tags(self, tags: List[int]) -> bytes:
        """
        Get transaction receipt tags
        
        Args:
            tags: List of tag IDs to retrieve
        
        Returns:
            TLV-encoded response data
        """
        if not self._is_open:
            raise RuntimeError("Device not open")
        
        tags_array = (ctypes.c_uint8 * len(tags))(*tags)
        response_ptr = ctypes.POINTER(ctypes.c_uint8)()
        response_len = ctypes.c_uint16()
        
        ret = _lib.datecs_get_receipt_tags(
            self._device, tags_array, len(tags),
            ctypes.byref(response_ptr), ctypes.byref(response_len)
        )
        
        if ret != 0:
            raise RuntimeError(f"Failed to get receipt tags: {self._error_string(ret)}")
        
        # Copy data to Python bytes
        data = bytes(response_ptr[:response_len.value])
        
        # Free the C buffer
        ctypes.pythonapi.free(response_ptr)
        
        return data
    
    def get_datetime(self) -> datetime:
        """
        Get pinpad real-time clock
        
        Returns:
            datetime object with current device time
        """
        if not self._is_open:
            raise RuntimeError("Device not open")
        
        dt = DatecsDateTime()
        ret = _lib.datecs_get_rtc(self._device, ctypes.byref(dt))
        
        if ret != 0:
            raise RuntimeError(f"Failed to get RTC: {self._error_string(ret)}")
        
        return datetime(
            year=2000 + dt.year,
            month=dt.month,
            day=dt.day,
            hour=dt.hour,
            minute=dt.minute,
            second=dt.second
        )
    
    def set_datetime(self, dt: datetime) -> None:
        """
        Set pinpad real-time clock
        
        Args:
            dt: datetime object with time to set
        """
        if not self._is_open:
            raise RuntimeError("Device not open")
        
        c_dt = DatecsDateTime()
        c_dt.year = dt.year - 2000
        c_dt.month = dt.month
        c_dt.day = dt.day
        c_dt.hour = dt.hour
        c_dt.minute = dt.minute
        c_dt.second = dt.second
        
        ret = _lib.datecs_set_rtc(self._device, ctypes.byref(c_dt))
        
        if ret != 0:
            raise RuntimeError(f"Failed to set RTC: {self._error_string(ret)}")
    
    @staticmethod
    def parse_tlv(data: bytes) -> dict:
        """
        Parse TLV-encoded data
        
        Args:
            data: TLV-encoded bytes
        
        Returns:
            Dictionary mapping tag IDs to their values
        """
        result = {}
        pos = 0
        
        while pos < len(data):
            # Read tag (1 or 2 bytes)
            if data[pos] in (0xDF, 0x9F):
                if pos + 1 >= len(data):
                    break
                tag = (data[pos] << 8) | data[pos + 1]
                pos += 2
            else:
                tag = data[pos]
                pos += 1
            
            # Read length
            if pos >= len(data):
                break
            length = data[pos]
            pos += 1
            
            # Read value
            if pos + length > len(data):
                break
            value = data[pos:pos + length]
            pos += length
            
            result[tag] = value
        
        return result
    
    @staticmethod
    def build_tlv(tag: int, value: bytes) -> bytes:
        """
        Build TLV-encoded data
        
        Args:
            tag: Tag ID
            value: Tag value
        
        Returns:
            TLV-encoded bytes
        """
        result = bytearray()
        
        # Write tag
        if tag > 0xFF:
            result.append((tag >> 8) & 0xFF)
            result.append(tag & 0xFF)
        else:
            result.append(tag & 0xFF)
        
        # Write length
        result.append(len(value))
        
        # Write value
        result.extend(value)
        
        return bytes(result)
    
    @staticmethod
    def encode_amount(amount: int) -> bytes:
        """
        Encode amount as 4-byte big-endian integer
        
        Args:
            amount: Amount in smallest currency units (e.g., cents)
        
        Returns:
            4-byte encoded amount
        """
        return amount.to_bytes(4, byteorder='big')
    
    @staticmethod
    def decode_amount(data: bytes) -> int:
        """
        Decode amount from 4-byte big-endian integer
        
        Args:
            data: 4-byte encoded amount
        
        Returns:
            Amount in smallest currency units
        """
        return int.from_bytes(data, byteorder='big')
    
    @staticmethod
    def _error_string(error_code: int) -> str:
        """Get error string for error code"""
        err_str = _lib.datecs_error_string(error_code)
        if err_str:
            return err_str.decode('utf-8')
        return f"Unknown error {error_code}"


# Convenience functions for common operations
def create_purchase_params(amount: int, tip: Optional[int] = None, 
                          cashback: Optional[int] = None,
                          reference: Optional[str] = None) -> bytes:
    """
    Create TLV parameters for purchase transaction
    
    Args:
        amount: Purchase amount in smallest currency units
        tip: Optional tip amount
        cashback: Optional cashback amount
        reference: Optional reference string
    
    Returns:
        TLV-encoded parameters
    """
    params = bytearray()
    
    # Add amount
    params.extend(DatecsPinpadDriver.build_tlv(TAG_AMOUNT, 
                                               DatecsPinpadDriver.encode_amount(amount)))
    
    # Add tip if present
    if tip is not None:
        params.extend(DatecsPinpadDriver.build_tlv(TAG_TIP,
                                                   DatecsPinpadDriver.encode_amount(tip)))
    
    # Add cashback if present
    if cashback is not None:
        params.extend(DatecsPinpadDriver.build_tlv(TAG_CASHBACK,
                                                   DatecsPinpadDriver.encode_amount(cashback)))
    
    # Add reference if present
    if reference is not None:
        params.extend(DatecsPinpadDriver.build_tlv(TAG_REFERENCE,
                                                   reference.encode('utf-8')))
    
    return bytes(params)


def create_void_params(amount: int, rrn: str, auth_id: str,
                       tip: Optional[int] = None, 
                       cashback: Optional[int] = None) -> bytes:
    """
    Create TLV parameters for void transaction
    
    Args:
        amount: Original transaction amount
        rrn: Original transaction RRN
        auth_id: Original transaction authorization ID
        tip: Optional tip amount (for void of purchase with tip)
        cashback: Optional cashback amount (for void of purchase with cashback)
    
    Returns:
        TLV-encoded parameters
    """
    params = bytearray()
    
    # Add amount
    params.extend(DatecsPinpadDriver.build_tlv(TAG_AMOUNT,
                                               DatecsPinpadDriver.encode_amount(amount)))
    
    # Add RRN
    params.extend(DatecsPinpadDriver.build_tlv(TAG_RRN, rrn.encode('utf-8')))
    
    # Add authorization ID
    params.extend(DatecsPinpadDriver.build_tlv(TAG_AUTH_ID, auth_id.encode('utf-8')))
    
    # Add tip if present
    if tip is not None:
        params.extend(DatecsPinpadDriver.build_tlv(TAG_TIP,
                                                   DatecsPinpadDriver.encode_amount(tip)))
    
    # Add cashback if present
    if cashback is not None:
        params.extend(DatecsPinpadDriver.build_tlv(TAG_CASHBACK,
                                                   DatecsPinpadDriver.encode_amount(cashback)))
    
    return bytes(params)
