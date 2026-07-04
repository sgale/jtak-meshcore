"""
BME280 / BME680 / SHT31-D auto-detecting sensor reader.
- BME280: hand-rolled smbus2 (working, no extra deps)
- BME680: pimoroni bme680 library (handles calibration correctly)
- SHT31-D: hand-rolled smbus2 at 0x44 (temp + humidity only, no pressure/gas)
Tries 0x44 first, then 0x76/0x77. Updates latest_hub_env every `interval` seconds.
"""

import asyncio
import struct
import time
import math
import logging
from smbus2 import SMBus
from utils.config import get as _cfg

log = logging.getLogger("bme_sensor")

BME_BUS    = 1
_ADDRS     = [0x76, 0x77]
_SHT31_ADDR = 0x44

_CHIP_BME280 = (0x60, 0x58)
_CHIP_BME680 = (0x61,)

# Lazy-init pimoroni sensor object (one per process)
_bme680_sensor = None

# IAQ thresholds: gas resistance (Ω) → rough air quality %
# Higher resistance = cleaner air. Log-scaled 0–100.
_GAS_BASELINE = 50_000   # Ω — "good" clean-air reference
_GAS_FLOOR    =  1_000   # Ω — worst expected


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _detect():
    """Return (addr, chip_id) for first supported sensor found, else raise.
    Checks SHT31-D at 0x44 first, then BME280/680 at 0x76/0x77.
    SHT31 has no chip-ID register — detected by triggering a measurement."""
    # SHT31-D — use no-clock-stretching command (0x2400) via i2c_msg
    try:
        from smbus2 import i2c_msg
        with SMBus(BME_BUS) as bus:
            bus.i2c_rdwr(i2c_msg.write(_SHT31_ADDR, [0x24, 0x00]))
            time.sleep(0.02)
            r = i2c_msg.read(_SHT31_ADDR, 6)
            bus.i2c_rdwr(r)
        return _SHT31_ADDR, 'SHT31'
    except Exception:
        pass
    # BME280 / BME680
    with SMBus(BME_BUS) as bus:
        for addr in _ADDRS:
            try:
                cid = bus.read_byte_data(addr, 0xD0)
                if cid in _CHIP_BME280 or cid in _CHIP_BME680:
                    return addr, cid
            except Exception:
                pass
    raise RuntimeError("No BME280/BME680/SHT31 found on I2C bus")


def _gas_to_iaq(gas_ohm):
    """Simplified IAQ 0–100%: 100 = pristine, 0 = very poor."""
    import math
    if not gas_ohm or gas_ohm <= 0:
        return None
    ratio = gas_ohm / _GAS_BASELINE
    iaq = max(0.0, min(100.0,
        (1.0 - math.log(ratio) / math.log(_GAS_FLOOR / _GAS_BASELINE)) * 100))
    return round(iaq, 1)


# ── BME280 (hand-rolled smbus2) ────────────────────────────────────────────────

def _bme280_read(addr):
    with SMBus(BME_BUS) as bus:
        raw = bus.read_i2c_block_data(addr, 0x88, 24)
        T1, T2, T3, P1, P2, P3, P4, P5, P6, P7, P8, P9 = \
            struct.unpack_from('<HhhHhhhhhhhh', bytes(raw))
        h_raw = bus.read_i2c_block_data(addr, 0xE1, 7)
        H1 = bus.read_byte_data(addr, 0xA1)
        H2 = struct.unpack_from('<h', bytes(h_raw), 0)[0]
        H3 = h_raw[2]
        H4 = (h_raw[3] << 4) | (h_raw[4] & 0x0F)
        H5 = (h_raw[4] >> 4) | (h_raw[5] << 4)
        H6 = struct.unpack_from('<b', bytes(h_raw), 6)[0]

        bus.write_byte_data(addr, 0xF2, 0x01)
        bus.write_byte_data(addr, 0xF4, 0x25)
        time.sleep(0.015)

        d = bus.read_i2c_block_data(addr, 0xF7, 8)
        raw_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        raw_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        raw_h = (d[6] << 8) | d[7]

    v1 = ((raw_t / 16384.0) - (T1 / 1024.0)) * T2
    v2 = ((raw_t / 131072.0) - (T1 / 8192.0)) ** 2 * T3
    t_fine = v1 + v2
    temp_c = t_fine / 5120.0

    v1 = t_fine / 2.0 - 64000.0
    v2 = v1 * v1 * P6 / 32768.0 + v1 * P5 * 2.0
    v2 = v2 / 4.0 + P4 * 65536.0
    v1 = (P3 * v1 * v1 / 524288.0 + P2 * v1) / 524288.0
    v1 = (1.0 + v1 / 32768.0) * P1
    pres_hpa = 0.0
    if v1 != 0:
        p = 1048576.0 - raw_p
        p = (p - v2 / 4096.0) * 6250.0 / v1
        v1 = P9 * p * p / 2147483648.0
        v2 = p * P8 / 32768.0
        pres_hpa = (p + (v1 + v2 + P7) / 16.0) / 100.0

    hum = t_fine - 76800.0
    hum = (raw_h - (H4 * 64.0 + H5 / 16384.0 * hum)) * (
        H2 / 65536.0 * (1.0 + H6 / 67108864.0 * hum * (1.0 + H3 / 67108864.0 * hum)))
    hum = max(0.0, min(100.0, hum * (1.0 - H1 * hum / 524288.0)))

    return round(temp_c, 2), round(pres_hpa, 2), round(hum, 2), None, None


# ── BME680 (pimoroni library) ──────────────────────────────────────────────────

def _bme680_init(addr):
    global _bme680_sensor
    import bme680 as _lib
    i2c_addr = _lib.I2C_ADDR_SECONDARY if addr == 0x77 else _lib.I2C_ADDR_PRIMARY
    s = _lib.BME680(i2c_addr)
    s.set_humidity_oversample(_lib.OS_2X)
    s.set_pressure_oversample(_lib.OS_4X)
    s.set_temperature_oversample(_lib.OS_8X)
    s.set_filter(_lib.FILTER_SIZE_3)
    s.set_gas_status(_lib.ENABLE_GAS_MEAS)
    s.set_gas_heater_temperature(320)
    s.set_gas_heater_duration(150)
    s.select_gas_heater_profile(0)
    _bme680_sensor = s
    log.info(f"BME680 pimoroni sensor initialised at 0x{addr:02x}")


def _bme680_read(addr):
    global _bme680_sensor
    if _bme680_sensor is None:
        _bme680_init(addr)

    for _ in range(6):
        if _bme680_sensor.get_sensor_data():
            break
        time.sleep(0.5)
    else:
        _bme680_sensor = None  # force reinit next cycle (stale connection after hot-swap)
        raise RuntimeError("BME680 data not ready")

    d = _bme680_sensor.data
    gas_ohm = None
    iaq     = None
    if d.heat_stable:
        gas_ohm = round(d.gas_resistance)
        iaq     = _gas_to_iaq(gas_ohm)

    return round(d.temperature, 2), round(d.pressure, 2), round(d.humidity, 2), gas_ohm, iaq


# ── SHT31-D (hand-rolled smbus2) ──────────────────────────────────────────────

def _sht31_read():
    """Single-shot high-repeatability read. Returns (temp_c, None, hum, None, None)."""
    from smbus2 import i2c_msg
    with SMBus(BME_BUS) as bus:
        bus.i2c_rdwr(i2c_msg.write(_SHT31_ADDR, [0x24, 0x00]))
        time.sleep(0.02)
        r = i2c_msg.read(_SHT31_ADDR, 6)
        bus.i2c_rdwr(r)
        d = list(r)
    raw_t = (d[0] << 8) | d[1]
    raw_h = (d[3] << 8) | d[4]
    temp_c  = round(-45.0 + 175.0 * raw_t / 65535.0, 2)
    hum_pct = round(100.0 * raw_h / 65535.0, 2)
    return temp_c, None, hum_pct, None, None


# ── Calibration offset ────────────────────────────────────────────────────────

def _apply_offset(temp_c: float, hum: float) -> tuple[float, float]:
    """Apply bme.temp_offset_f from jtak.yaml and recalculate RH via Magnus formula."""
    offset_f = float(_cfg("bme.temp_offset_f", 0.0))
    if offset_f == 0.0:
        return temp_c, hum
    offset_c = offset_f / 1.8
    temp_corrected = temp_c + offset_c
    # Recalculate RH: preserve dew point (absolute moisture), rescale to corrected temp
    a, b = 17.625, 243.04
    if hum > 0:
        alpha  = math.log(hum / 100.0) + a * temp_c / (b + temp_c)
        t_dew  = b * alpha / (a - alpha)
        hum_corrected = 100.0 * (
            math.exp(a * t_dew / (b + t_dew)) /
            math.exp(a * temp_corrected / (b + temp_corrected))
        )
        hum_corrected = max(0.0, min(100.0, hum_corrected))
    else:
        hum_corrected = hum
    return round(temp_corrected, 2), round(hum_corrected, 2)


# ── Public read dispatch ───────────────────────────────────────────────────────

def _read_once():
    addr, chip_id = _detect()
    if chip_id == 'SHT31':
        return addr, "SHT31", _sht31_read()
    elif chip_id in _CHIP_BME680:
        return addr, "BME680", _bme680_read(addr)
    else:
        return addr, "BME280", _bme280_read(addr)


# ── Async background loop ──────────────────────────────────────────────────────

async def run_bme280_loop(hub_env: dict, interval: int = 30):
    """Reads BME280 or BME680 every `interval` seconds, updates hub_env in place."""
    chip_logged = False
    while True:
        try:
            loop = asyncio.get_event_loop()
            addr, chip, (temp_c, pres_hpa, hum, gas_ohm, iaq) = \
                await loop.run_in_executor(None, _read_once)
            if not chip_logged:
                log.info(f"{chip} detected at 0x{addr:02x}")
                chip_logged = True
            temp_c, hum = _apply_offset(temp_c, hum)
            hub_env["hub_temp_c"]       = temp_c
            hub_env["hub_humidity_pct"] = hum
            hub_env["hub_pressure_hpa"] = pres_hpa
            hub_env["hub_iaq_pct"]      = iaq
            log.info(f"{chip} 0x{addr:02x}: {temp_c}°C  {hum}%RH  {pres_hpa}hPa"
                     + (f"  gas={gas_ohm}Ω  IAQ={iaq}%" if gas_ohm else ""))
        except Exception as e:
            log.warning(f"BME read failed: {e}")
        await asyncio.sleep(interval)
