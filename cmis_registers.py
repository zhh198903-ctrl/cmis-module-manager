"""CMIS 5.4 register definitions and parse/encode utilities.

Authoritative source: OIF-CMIS-05.4.pdf

CMIS 5.4 is backward compatible: a 5.3 module stays fully readable here. The
fields it added are listed in NEW_IN_5_4 so the UI and the manual can mark them
in one place instead of the tag being retyped beside every field - a module
older than 5.4 simply reports them as absent rather than wrong.
All byte addresses match the spec's decimal byte numbering (linear address
within the 256-byte module memory window). Byte numbers >= 128 (0x80) are
in upper memory and require selecting the appropriate page via byte 0x7F.
"""
import math
import struct

# ---------------------------------------------------------------------------
# Lower Memory (bytes 0-127, page-independent) — Tables 8-4 / 8-5 / 8-6
# ---------------------------------------------------------------------------

REG_IDENTIFIER       = (None, 0x00, 1)   # SFF8024Identifier
REG_CMIS_REVISION    = (None, 0x01, 1)   # Upper nibble=major, lower=minor (0x53 = 5.3)
REG_MEMORY_MODEL     = (None, 0x02, 1)   # bit7: 0=paged, 1=flat
REG_MODULE_STATE     = (None, 0x03, 1)   # bits[3:1]=state, bit0=InterruptDeasserted
REG_FLAGS_SUMMARY    = (None, 0x04, 4)   # 4 bytes — bank/page flags summary
REG_MODULE_FLAGS     = (None, 0x08, 6)   # 6 bytes — module-level flags
REG_TEMPERATURE      = (None, 0x0E, 2)   # signed16 / 256 °C
REG_VOLTAGE          = (None, 0x10, 2)   # uint16 × 100 µV
REG_AUX1_MON         = (None, 0x12, 2)   # signed16
REG_AUX2_MON         = (None, 0x14, 2)   # signed16
REG_AUX3_MON         = (None, 0x16, 2)   # signed16
REG_CUSTOM_MON       = (None, 0x18, 2)
REG_MODULE_CONTROL   = (None, 0x1A, 1)   # Module-level control register
REG_FW_ACTIVE_MAJOR  = (None, 0x27, 1)   # Lower 39: Active FW Major (Table 8-15)
REG_FW_ACTIVE_MINOR  = (None, 0x28, 1)   # Lower 40: Active FW Minor
REG_MODULE_SUBTYPE   = (None, 0x3C, 1)   # Lower 60: [3:0] SFF8024ModuleSubtype
REG_HEATSINK_FIBER   = (None, 0x3D, 1)   # Lower 61: [7:4] HeatsinkType (5.4), [1:0] FiberFaceType
REG_MEDIA_TYPE       = (None, 0x55, 1)   # Lower 85: Media Type Encoding (Table 8-20)
# Application Descriptors (AppSel 1..8) — 4 bytes per descriptor
REG_APP_DESC_BASE    = (None, 0x56, 4 * 8)  # 86..117 (8 descriptors × 4 bytes)
REG_PAGE_SELECT      = (None, 0x7F, 1)

# ---------------------------------------------------------------------------
# Page 00h — Administrative Information (Table 8-26)
# ---------------------------------------------------------------------------
REG_VENDOR_NAME      = (0x00, 0x81, 16)  # 129-144  ASCII
REG_VENDOR_OUI       = (0x00, 0x91, 3)   # 145-147  IEEE OUI
REG_VENDOR_PN        = (0x00, 0x94, 16)  # 148-163  ASCII
REG_VENDOR_REV       = (0x00, 0xA4, 2)   # 164-165  ASCII
REG_VENDOR_SN        = (0x00, 0xA6, 16)  # 166-181  ASCII
REG_DATE_CODE        = (0x00, 0xB6, 8)   # 182-189  YYMMDDLL
REG_CLEI_CODE        = (0x00, 0xBE, 10)  # 190-199  ASCII
REG_MODULE_PWR_CLASS = (0x00, 0xC8, 1)   # bits[7:5]=power class
REG_MODULE_MAX_POWER = (0x00, 0xC9, 1)   # 0.25 W increments
REG_CABLE_LENGTH     = (0x00, 0xCA, 1)   # [7:6]=mult, [5:0]=base
REG_CONNECTOR_TYPE   = (0x00, 0xCB, 1)   # SFF-8024 Table 4-3
REG_CU_ATTENUATION   = (0x00, 0xCC, 6)   # 6 bytes copper attenuation
REG_MEDIA_LANE_INFO  = (0x00, 0xD2, 1)   # MediaLaneUnsupported bitmap
REG_FAR_END_CFG      = (0x00, 0xD3, 1)
REG_MEDIA_IF_TECH    = (0x00, 0xD4, 1)   # Media Interface Technology

# ---------------------------------------------------------------------------
# Page 01h — Advertising (Table 8-43, 8-46, 8-54)
# ---------------------------------------------------------------------------
REG_FW_INACT_MAJOR   = (0x01, 0x80, 1)   # 128: Inactive FW Major
REG_FW_INACT_MINOR   = (0x01, 0x81, 1)   # 129: Inactive FW Minor
REG_HW_REV_MAJOR     = (0x01, 0x82, 1)   # 130: HW Revision Major
REG_HW_REV_MINOR     = (0x01, 0x83, 1)   # 131: HW Revision Minor
REG_LENGTH_SMF       = (0x01, 0x84, 1)   # 132: SMF link length [7:6]=mult [5:0]=base
REG_LENGTH_OM5       = (0x01, 0x85, 1)   # 133
REG_LENGTH_OM4       = (0x01, 0x86, 1)   # 134
REG_LENGTH_OM3       = (0x01, 0x87, 1)   # 135
REG_LENGTH_OM2       = (0x01, 0x88, 1)   # 136
REG_BANKS_SUPPORTED  = (0x01, 0x8E, 1)   # 142  bits[1:0]
# --- CMIS 5.4 additions on Page 01h ---
REG_DEFAULT_POLARITY = (0x01, 0xAB, 2)   # 171-172 Default Input/Output polarity (Table 8-57)
REG_PAGES_EXT        = (0x01, 0xAD, 2)   # 173-174 Supported pages + extra banks (Table 8-58)
REG_MISC_CAPS        = (0x01, 0xFC, 1)   # 252  bit5 MediaLaneSwitchingSupported (Table 8-62)
REG_CDB_CAPS         = (0x01, 0xA3, 4)   # 163-166

# ---------------------------------------------------------------------------
# Page 02h — Thresholds (Table 8-62)
# ---------------------------------------------------------------------------
REG_TEMP_HIGH_ALARM     = (0x02, 0x80, 2)
REG_TEMP_LOW_ALARM      = (0x02, 0x82, 2)
REG_TEMP_HIGH_WARN      = (0x02, 0x84, 2)
REG_TEMP_LOW_WARN       = (0x02, 0x86, 2)
REG_VCC_HIGH_ALARM      = (0x02, 0x88, 2)
REG_VCC_LOW_ALARM       = (0x02, 0x8A, 2)
REG_VCC_HIGH_WARN       = (0x02, 0x8C, 2)
REG_VCC_LOW_WARN        = (0x02, 0x8E, 2)
# Aux1/2/3 thresholds at 0x90-0xA7 (currently not exposed)
REG_TXPWR_HIGH_ALARM    = (0x02, 0xB0, 2)
REG_TXPWR_LOW_ALARM     = (0x02, 0xB2, 2)
REG_TXPWR_HIGH_WARN     = (0x02, 0xB4, 2)
REG_TXPWR_LOW_WARN      = (0x02, 0xB6, 2)
REG_TXBIAS_HIGH_ALARM   = (0x02, 0xB8, 2)
REG_TXBIAS_LOW_ALARM    = (0x02, 0xBA, 2)
REG_TXBIAS_HIGH_WARN    = (0x02, 0xBC, 2)
REG_TXBIAS_LOW_WARN     = (0x02, 0xBE, 2)
REG_RXPWR_HIGH_ALARM    = (0x02, 0xC0, 2)
REG_RXPWR_LOW_ALARM     = (0x02, 0xC2, 2)
REG_RXPWR_HIGH_WARN     = (0x02, 0xC4, 2)
REG_RXPWR_LOW_WARN      = (0x02, 0xC6, 2)

# ---------------------------------------------------------------------------
# Page 10h — Data Path Control (Table 8-67 overview):
#   128     Data Path Control
#   129-142 Lane-Specific Control   (Table 8-69)
#   143-177 Staged Control Set 0    (Tables 8-70 / 8-72)
# Bytes 133 and 140-142 are Reserved. Note there is deliberately no
# OutputSquelchForceRx counterpart to OutputSquelchForceTx (§8.9.2.3).
# ---------------------------------------------------------------------------
REG_DP_DEINIT        = (0x10, 0x80, 1)   # 128  DataPathDeinit
REG_TX_POL_FLIP      = (0x10, 0x81, 1)   # 129  InputPolarityFlipTx
REG_TX_OUTPUT_DIS    = (0x10, 0x82, 1)   # 130  OutputDisableTx
REG_TX_SQUELCH_DIS   = (0x10, 0x83, 1)   # 131  AutoSquelchDisableTx
REG_TX_FORCE_SQUELCH = (0x10, 0x84, 1)   # 132  OutputSquelchForceTx
REG_RX_POL_FLIP      = (0x10, 0x89, 1)   # 137  OutputPolarityFlipRx
REG_RX_OUTPUT_DIS    = (0x10, 0x8A, 1)   # 138  OutputDisableRx
REG_RX_SQUELCH_DIS   = (0x10, 0x8B, 1)   # 139  AutoSquelchDisableRx
REG_APPLY_DATAPATH   = (0x10, 0x8F, 1)   # 143  ApplyDPInit, write 0xFF to apply
REG_APPLY_IMM        = (0x10, 0x90, 1)   # 144  ApplyImmediate
REG_APP_SELECT       = (0x10, 0x91, 8)   # 145-152 DPConfigLane1-8

# ---------------------------------------------------------------------------
# Page 11h — DataPath Status & Monitoring (Table 8-82)
# ---------------------------------------------------------------------------
REG_DP_STATE        = (0x11, 0x80, 4)   # 4 bytes, 4 bits/lane (nibble per lane)
REG_OUTPUT_STATUS_RX= (0x11, 0x84, 1)   # 132  §8.10.2: "OutputStatusRx register (11h:132)"
REG_OUTPUT_STATUS_TX= (0x11, 0x85, 1)   # 133  §8.10.2: "OutputStatusTx register (11h:133)"
REG_DP_STATE_CHANGED= (0x11, 0x86, 1)
REG_TX_FAULT_FLAGS  = (0x11, 0x87, 1)
REG_TX_LOS_FLAGS    = (0x11, 0x88, 1)
REG_TX_CDRLOL_FLAGS = (0x11, 0x89, 1)
REG_TX_AEQ_FAIL     = (0x11, 0x8A, 1)
REG_TXPWR_HIGH_ALARM_FLAGS  = (0x11, 0x8B, 1)
REG_TXPWR_LOW_ALARM_FLAGS   = (0x11, 0x8C, 1)
REG_TXPWR_HIGH_WARN_FLAGS   = (0x11, 0x8D, 1)
REG_TXPWR_LOW_WARN_FLAGS    = (0x11, 0x8E, 1)
REG_TXBIAS_HIGH_ALARM_FLAGS = (0x11, 0x8F, 1)
REG_TXBIAS_LOW_ALARM_FLAGS  = (0x11, 0x90, 1)
REG_TXBIAS_HIGH_WARN_FLAGS  = (0x11, 0x91, 1)
REG_TXBIAS_LOW_WARN_FLAGS   = (0x11, 0x92, 1)
REG_RX_LOS_FLAGS    = (0x11, 0x93, 1)
REG_RX_CDRLOL_FLAGS = (0x11, 0x94, 1)
REG_RXPWR_HIGH_ALARM_FLAGS  = (0x11, 0x95, 1)
REG_RXPWR_LOW_ALARM_FLAGS   = (0x11, 0x96, 1)
REG_RXPWR_HIGH_WARN_FLAGS   = (0x11, 0x97, 1)
REG_RXPWR_LOW_WARN_FLAGS    = (0x11, 0x98, 1)
REG_TX_POWER        = (0x11, 0x9A, 16)  # 8 lanes × 2B, ×0.1 µW
REG_TX_BIAS         = (0x11, 0xAA, 16)  # 8 lanes × 2B, ×2 µA
REG_RX_POWER        = (0x11, 0xBA, 16)  # 8 lanes × 2B, ×0.1 µW
REG_CONFIG_STATUS   = (0x11, 0xCA, 4)   # 4 bytes, 4 bits/lane (nibble per lane)
REG_DP_CONFIG_LANE  = (0x11, 0xCE, 8)   # 1 byte/lane DPConfigLane Active Set

# ---------------------------------------------------------------------------
# Page 04h — Laser Capabilities (Table 8-66, RO)
# ---------------------------------------------------------------------------
REG_GRID_SUPPORTED   = (0x04, 0x80, 2)   # 128-129: grid support + fine tuning
REG_GRID_CHANNELS    = (0x04, 0x82, 36)  # 130-165: S16 low/high per grid (9 grids × 4)
REG_FINE_RESOLUTION  = (0x04, 0xBE, 2)   # 190-191: U16 0.001 GHz units
REG_FINE_LOW_OFFSET  = (0x04, 0xC0, 2)   # 192-193: S16 0.001 GHz
REG_FINE_HIGH_OFFSET = (0x04, 0xC2, 2)   # 194-195: S16 0.001 GHz
REG_PROG_PWR_MIN     = (0x04, 0xC6, 2)   # 198-199: S16 0.01 dBm
REG_GRID_300_CHANNELS= (0x04, 0xA6, 4)   # 166-169: S16 low/high for the 300 GHz grid (5.4)
REG_REL_THR_CAP      = (0x04, 0xC4, 1)   # 196: bit6 relative Tx power thresholds supported (5.4)
REG_PROG_PWR_MAX     = (0x04, 0xC8, 2)   # 200-201: S16 0.01 dBm

# ---------------------------------------------------------------------------
# Page 12h — Laser Tuning Control & Status (Table 8-99, banked)
# ---------------------------------------------------------------------------
REG_GRID_SPACING_TX  = (0x12, 0x80, 8)   # 128-135: 1B/lane [7:4]=grid [0]=FineTuneEn
REG_CHANNEL_NUM_TX   = (0x12, 0x88, 16)  # 136-151: S16/lane (2B × 8)
REG_FINE_OFFSET_TX   = (0x12, 0x98, 16)  # 152-167: S16/lane (2B × 8)
REG_CURRENT_FREQ_TX  = (0x12, 0xA8, 32)  # 168-199: U32/lane (4B × 8) in 0.001 GHz
REG_TARGET_PWR_TX    = (0x12, 0xC8, 16)  # 200-215: S16/lane (0.01 dBm)
REG_REL_THRESHOLDS   = (0x12, 0xD8, 2)   # 216-217: relative Tx power thresholds (5.4)
REG_TUNING_STATUS_TX = (0x12, 0xDE, 8)   # 222-229: 1B/lane [1]=TuningInProgress [0]=Unlocked
REG_TUNING_FLAGS_TX  = (0x12, 0xE7, 8)   # 231-238: 1B/lane latched flags

# ---------------------------------------------------------------------------
# Page 13h — Diagnostic Controls (Tables 8-109..8-117)
# Each PRBS block is 8 bytes per side:
#   +0 Enable, +1 DataInvert, +2 ByteSwap, +3 Pre/PostFEC, +4..+7 PatternSelect
# ---------------------------------------------------------------------------
REG_HOST_PRBS_GEN    = (0x13, 0x90, 8)   # 144-151
REG_MEDIA_PRBS_GEN   = (0x13, 0x98, 8)   # 152-159
REG_HOST_PRBS_CHK    = (0x13, 0xA0, 8)   # 160-167
REG_MEDIA_PRBS_CHK   = (0x13, 0xA8, 8)   # 168-175
REG_MEDIA_OUT_LB     = (0x13, 0xB4, 1)
REG_MEDIA_IN_LB      = (0x13, 0xB5, 1)
REG_HOST_OUT_LB      = (0x13, 0xB6, 1)
REG_HOST_IN_LB       = (0x13, 0xB7, 1)

# ---------------------------------------------------------------------------
# Page 14h — Diagnostic Results (Tables 8-126..8-129)
# ---------------------------------------------------------------------------
REG_DIAG_SELECTOR    = (0x14, 0x80, 1)
REG_HOST_PRBS_LOL    = (0x14, 0x8A, 1)   # PatternCheckerLOL Host (138)
REG_MEDIA_PRBS_LOL   = (0x14, 0x8B, 1)   # PatternCheckerLOL Media (139)
REG_DIAG_DATA        = (0x14, 0xC0, 64)  # selector-dependent

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

MODULE_ID_NAMES = {
    0x03: "SFP/SFP+/SFP28",
    0x0B: "DWDM-SFP/SFP+",
    0x0C: "QSFP",
    0x0D: "QSFP+",
    0x11: "QSFP28",
    0x18: "QSFP-DD",
    0x19: "OSFP",
    0x1B: "DSFP",
    0x1E: "QSFP-DD CMIS",
    0x1F: "OSFP CMIS",
    0x20: "x4 MiniLink",
    0x22: "QSFP-DD CMIS",
    0x23: "QSFP56",
    0x24: "OSFP-XD",
    0x25: "CMIS-Compliant",
}

# Table 8-7: Module State (3-bit field in bits[3:1] of byte 0x03)
MODULE_STATES = {
    0b000: "Reserved",
    0b001: "ModuleLowPwr",
    0b010: "ModulePwrUp",
    0b011: "ModuleReady",
    0b100: "ModulePwrDn",
    0b101: "ModuleFault",
    0b110: "Reserved",
    0b111: "Reserved",
}

# Table 8-84: DataPath State (4-bit field per lane on Page 11h:128-131)
DP_STATE_NAMES = {
    0x0: "Reserved",
    0x1: "Deactivated",
    0x2: "Init",
    0x3: "Deinit",
    0x4: "Activated",
    0x5: "TxTurnOn",
    0x6: "TxTurnOff",
    0x7: "Initialized",
}

# Table 8-91: ConfigStatus codes (4 bits per lane)
CONFIG_STATUS_NAMES = {
    0x0: "ConfigUndefined",
    0x1: "ConfigSuccess",
    0x2: "ConfigRejected",
    0x3: "ConfigRejectedInvalidAppSel",
    0x4: "ConfigRejectedInvalidDataPath",
    0x5: "ConfigRejectedInvalidSI",
    0x6: "ConfigRejectedLanesInUse",
    0x7: "ConfigRejectedPartialDataPath",
    0xC: "ConfigInProgress",
}

# SFF-8024 Table 4-3 — Connector Type
CONNECTOR_TYPES = {
    0x00: "Unknown",
    0x01: "SC",
    0x02: "FC Style 1",
    0x03: "FC Style 2",
    0x04: "BNC/TNC",
    0x05: "FC coax",
    0x06: "FiberJack",
    0x07: "LC",
    0x08: "MT-RJ",
    0x09: "MU",
    0x0A: "SG",
    0x0B: "Optical Pigtail",
    0x0C: "MPO 1×12",
    0x0D: "MPO 2×16",
    0x20: "HSSDC II",
    0x21: "Copper Pigtail",
    0x22: "RJ45",
    0x23: "No Separable Connector",
    0x24: "MXC 2×16",
    0x25: "CS Optical Connector",
    0x26: "SN Optical Connector",
    0x27: "MPO 2×12",
    0x28: "MPO 1×16",
}

# Table 8-40 — Media Interface Technology
MEDIA_IF_TECH = {
    0x00: "850 nm VCSEL",
    0x01: "1310 nm VCSEL",
    0x02: "1550 nm VCSEL",
    0x03: "1310 nm FP",
    0x04: "1310 nm DFB",
    0x05: "1550 nm DFB",
    0x06: "1310 nm EML",
    0x07: "1550 nm EML",
    0x08: "Other",
    0x09: "1490 nm DFB",
    0x0A: "Copper unequalized",
    0x0B: "Copper passive equalized",
    0x0C: "Copper near-far end limiting active equalizers",
    0x0D: "Copper far end limiting active equalizers",
    0x0E: "Copper near end limiting active equalizers",
    0x0F: "Copper linear active equalizers",
    0x10: "C-band tunable laser",
    0x11: "L-band tunable laser",
}

# Table 8-21 — Media Type (Lower memory byte 85)
MEDIA_TYPES = {
    0x00: "Undefined",
    0x01: "MMF",
    0x02: "SMF",
    0x03: "Passive Copper",
    0x04: "Active Cable",
    0x05: "BASE-T",
}

# PRBS pattern IDs (Table 8-105)
PRBS_PATTERN_NAMES = [
    'PRBS31Q', 'PRBS31', 'PRBS23Q', 'PRBS23', 'PRBS15Q', 'PRBS15',
    'PRBS13Q', 'PRBS13', 'PRBS9Q', 'PRBS9', 'PRBS7Q', 'PRBS7', 'SSPRQ',
]

# ---------------------------------------------------------------------------
# Parse / encode utilities
# ---------------------------------------------------------------------------

def parse_temperature(raw: bytes) -> float:
    return struct.unpack(">h", raw[:2])[0] / 256.0


def parse_voltage(raw: bytes) -> float:
    return struct.unpack(">H", raw[:2])[0] * 0.0001


def parse_threshold_voltage(raw: bytes) -> float:
    return struct.unpack(">H", raw[:2])[0] * 0.0001


def parse_power_uw(raw: bytes) -> float:
    return struct.unpack(">H", raw[:2])[0] * 0.1


def uw_to_dbm(uw: float) -> float:
    if uw <= 0:
        return -40.0
    return 10.0 * math.log10(uw / 1000.0)


def parse_tx_bias_ma(raw: bytes) -> float:
    return struct.unpack(">H", raw[:2])[0] * 0.002


def parse_ascii(raw: bytes) -> str:
    return raw.decode("ascii", errors="replace").rstrip("\x00 ")


def parse_oui(raw: bytes) -> str:
    """Format 3-byte IEEE OUI as XX-XX-XX hex string."""
    return "-".join(f"{b:02X}" for b in raw[:3])


def parse_module_state(byte_val: int) -> str:
    """Decode lower memory byte 0x03: bits[3:1] = ModuleState, bit0 = InterruptDeasserted."""
    state = (byte_val >> 1) & 0x07
    return MODULE_STATES.get(state, f"Unknown({state})")


def parse_interrupt_asserted(byte_val: int) -> bool:
    """Bit 0: 1=not asserted (default), 0=asserted (inverted sense)."""
    return (byte_val & 0x01) == 0


def parse_dp_states(data: bytes) -> list:
    """Decode 4 bytes of DataPath state at Page 11h:128-131.

    Each byte holds 2 lanes, 4 bits per lane:
      11h:128: bits[7:4]=Lane2, bits[3:0]=Lane1
      11h:129: bits[7:4]=Lane4, bits[3:0]=Lane3
      11h:130: bits[7:4]=Lane6, bits[3:0]=Lane5
      11h:131: bits[7:4]=Lane8, bits[3:0]=Lane7
    """
    states = []
    for lane in range(8):
        byte_idx = lane // 2
        if byte_idx >= len(data):
            states.append("Unknown")
            continue
        nibble = (data[byte_idx] >> ((lane % 2) * 4)) & 0x0F
        states.append(DP_STATE_NAMES.get(nibble, f"Unknown(0x{nibble:X})"))
    return states


def parse_config_status(data: bytes) -> list:
    """Decode 4 bytes of ConfigStatus at Page 11h:202-205 (4 bits/lane)."""
    statuses = []
    for lane in range(8):
        byte_idx = lane // 2
        if byte_idx >= len(data):
            statuses.append("Unknown")
            continue
        nibble = (data[byte_idx] >> ((lane % 2) * 4)) & 0x0F
        statuses.append(CONFIG_STATUS_NAMES.get(nibble, f"Unknown(0x{nibble:X})"))
    return statuses


def parse_lane_flags(byte_val: int) -> list:
    """Bit i (i=0..7) → Lane i+1 flag bool."""
    return [bool((byte_val >> i) & 1) for i in range(8)]


def unpack_appselect(data: bytes) -> list:
    """8 bytes, 1 byte per lane (DPConfigLane, Table 8-71):
    bits[7:4]=AppSelCode, bits[3:1]=DataPathID, bit[0]=ExplicitControl.
    """
    return [(data[i] >> 4) & 0x0F if i < len(data) else 0 for i in range(8)]


def pack_appselect(values: list) -> bytes:
    """Pack 8 AppSelCodes into 8 DPConfigLane bytes.

    DataPathID (bits[3:1]) and ExplicitControl (bit[0]) are left at 0, i.e.
    one Data Path starting at lane 1 using Application-dependent SI settings.
    """
    return bytes((v & 0x0F) << 4 for v in values[:8] + [0] * (8 - len(values)))


def unpack_prbs_patterns(data: bytes) -> list:
    """4 bytes, 4 bits/lane (2 lanes/byte). Lane 1 = low nibble of byte 0."""
    patterns = []
    for i in range(8):
        byte_idx = i // 2
        b = data[byte_idx] if byte_idx < len(data) else 0
        patterns.append(b & 0x0F if i % 2 == 0 else (b >> 4) & 0x0F)
    return patterns


def pack_prbs_patterns(patterns: list) -> bytes:
    result = bytearray(4)
    for i, v in enumerate(patterns[:8]):
        byte_idx = i // 2
        if i % 2 == 0:
            result[byte_idx] = (result[byte_idx] & 0xF0) | (v & 0x0F)
        else:
            result[byte_idx] = (result[byte_idx] & 0x0F) | ((v & 0x0F) << 4)
    return bytes(result)


def parse_f16_ber(raw: bytes) -> float:
    """CMIS F16 decimal format. bits[15:11]=exp, bits[10:0]=mantissa.
    value = mantissa × 10^(exp − 24)
    word=0 → below measurement floor (return 0.0).
    """
    word = struct.unpack(">H", raw[:2])[0]
    if word == 0:
        return 0.0
    exp = (word >> 11) & 0x1F
    mantissa = word & 0x07FF
    return mantissa * (10.0 ** (exp - 24))


def encode_f16_ber(ber: float) -> int:
    """Encode BER float → CMIS F16 word (decimal floating point).

    Picks the smallest exponent s such that mantissa m = round(ber × 10^(24-s))
    fits in 11 bits (≤2047). This maximizes mantissa magnitude, minimizing
    quantization error for values like 5.5e-9 where naive encoding would truncate.
    """
    if ber <= 0:
        return 0x0000
    for s in range(32):
        m = round(ber * (10.0 ** (24 - s)))
        if m <= 2047:
            if m < 1:
                # ber too small even at s=0: fallback to below-floor sentinel
                return 0x0000
            return ((s & 0x1F) << 11) | (m & 0x07FF)
    # ber larger than max representable: saturate
    return (31 << 11) | 2047


def parse_snr_db(raw: bytes) -> float:
    """Page 14h selector 0x06 SNR: U16 little-endian, 1/256 dB units."""
    value = struct.unpack("<H", raw[:2])[0]
    return value / 256.0


def parse_power_class(byte_val: int) -> dict:
    """Byte 00h:200 ModulePowerCharacteristics — bits[7:5] = power class (0..7)."""
    cls = (byte_val >> 5) & 0x07
    return {'class': cls + 1, 'code': cls}


def parse_max_power_w(byte_val: int) -> float:
    """Byte 00h:201 MaxPower in 0.25 W units."""
    return byte_val * 0.25


def parse_cable_length_m(byte_val: int) -> float:
    """Byte 00h:202: bits[7:6]=multiplier (0.1, 1, 10, 100), bits[5:0]=base (0..63)."""
    base = byte_val & 0x3F
    mult_code = (byte_val >> 6) & 0x03
    mult = [0.1, 1.0, 10.0, 100.0][mult_code]
    return base * mult


def connector_type_name(code: int) -> str:
    return CONNECTOR_TYPES.get(code, f"Unknown(0x{code:02X})")


def media_if_tech_name(code: int) -> str:
    return MEDIA_IF_TECH.get(code, f"Unknown(0x{code:02X})")


def media_type_name(code: int) -> str:
    return MEDIA_TYPES.get(code, f"Unknown(0x{code:02X})")


def module_id_name(mid: int) -> str:
    return MODULE_ID_NAMES.get(mid, f"Unknown (0x{mid:02X})")


def cmis_revision_str(rev: int) -> str:
    """Convert CMIS revision byte to string like '5.3'.
    Upper nibble = major, lower = minor.
    """
    return f"{(rev >> 4) & 0x0F}.{rev & 0x0F}"


def parse_application_descriptors(data: bytes) -> list:
    """Parse Application Descriptors from lower memory bytes 86-117 (8 × 4 bytes).

    Each descriptor:
      +0: HostInterfaceID (0xFF = unused/end)
      +1: MediaInterfaceID
      +2: bits[7:4]=HostLaneCount, bits[3:0]=MediaLaneCount
      +3: HostLaneAssignmentOptions (bitmap)
    """
    apps = []
    for i in range(8):
        off = i * 4
        if off + 4 > len(data):
            break
        host_if = data[off]
        if host_if == 0xFF:
            break
        media_if = data[off + 1]
        lane_count = data[off + 2]
        host_lane_assign = data[off + 3]
        apps.append({
            'app_sel': i + 1,
            'host_if_id': host_if,
            'media_if_id': media_if,
            'host_lanes': (lane_count >> 4) & 0x0F,
            'media_lanes': lane_count & 0x0F,
            'host_lane_assign_mask': host_lane_assign,
        })
    return apps


def parse_module_control(byte_val: int) -> dict:
    """Decode lower memory byte 0x1A Module Control register."""
    return {
        'bank_broadcast_enable':    bool((byte_val >> 7) & 1),
        'low_pwr_allow_request_hw': bool((byte_val >> 6) & 1),
        'squelch_method_select':    bool((byte_val >> 5) & 1),
        'low_pwr_request_sw':       bool((byte_val >> 4) & 1),
        'software_reset':           bool((byte_val >> 3) & 1),
    }


MODULE_CONTROL_BITS = {
    'bank_broadcast':  7,
    'allow_lp_hw':     6,
    'squelch_method':  5,
    'low_pwr':         4,
    'software_reset':  3,
}


def encode_module_control(low_pwr: bool = False, software_reset: bool = False,
                          allow_lp_hw: bool = True, squelch_method: int = 0,
                          bank_broadcast: bool = False) -> int:
    """Build a complete Module Control byte (0x1A) from scratch.

    Prefer update_module_control when only some fields are being changed: this
    one forces every unnamed field back to its default.
    """
    val = 0
    if bank_broadcast:    val |= (1 << 7)
    if allow_lp_hw:       val |= (1 << 6)
    if squelch_method:    val |= (1 << 5)
    if low_pwr:           val |= (1 << 4)
    if software_reset:    val |= (1 << 3)
    return val


def update_module_control(current: int, **fields) -> int:
    """Change only the named bits of an already-read Module Control byte.

    Byte 0x1A packs unrelated controls together, so rebuilding it from scratch
    to toggle low power would also clear SquelchMethodSelect and
    BankBroadcastEnable and force AllowLowPwrRequestHW on. Read first, then
    change only what the caller asked for. Bits 2-0 are Custom and are carried
    through untouched.
    """
    val = current & 0xFF
    for name, value in fields.items():
        if value is None:
            continue
        try:
            bit = MODULE_CONTROL_BITS[name]
        except KeyError:
            raise ValueError(f"unknown Module Control field: {name}")
        if value:
            val |= (1 << bit)
        else:
            val &= ~(1 << bit) & 0xFF
    return val


# ---------------------------------------------------------------------------
# CMIS 5.4 additions
# ---------------------------------------------------------------------------
# Every field this tool surfaces that did not exist in CMIS 5.3. The UI tags
# these and the manual lists them from here, so the claim "new in 5.4" is made
# in exactly one place and cannot drift from what the decoders actually read.
NEW_IN_5_4 = frozenset({
    'heatsink_type',
    'abnormal_fw_flag',
    'abnormal_fw_mask',
    'default_input_polarity_tx',
    'default_output_polarity_rx',
    'page_0ch_supported',
    'page_0dh_supported',
    'page_60h_supported',
    'page_61h_supported',
    'page_62h_supported',
    'extra_lane_banks',
    'media_lane_switching_supported',
    'max_lanes',
    'grid_300ghz_supported',
    'grid_300ghz_range',
    'relative_power_thresholds_supported',
    'relative_power_thresholds',
    'relative_thresholds_enabled',
})

# Grid spacing code 1001b was added in CMIS 5.4; 5.3 stopped at 150 GHz.
GRID_CODES = {0: '3.125 GHz', 1: '6.25 GHz', 2: '12.5 GHz', 3: '25 GHz',
              4: '50 GHz', 5: '100 GHz', 6: '33 GHz', 7: '75 GHz',
              8: '150 GHz', 9: '300 GHz', 15: 'Not available'}


def is_new_in_5_4(field: str) -> bool:
    return field in NEW_IN_5_4


def parse_supported_pages(byte_142: int, ext: bytes = b'') -> dict:
    """Decode which optional pages and how many lane banks a module has.

    01h:142 (Table 8-47) has advertised pages and a 2-bit bank count since 5.2.
    CMIS 5.4 gave that field an escape value: 11b means "read the real count
    from 01h:174", which is what lifts the ceiling from 32 lanes to 256. A
    module answering 00b/01b/10b is pre-5.4 or simply small, and 01h:174 must
    not be consulted for it - the byte is not required to exist.
    """
    banks_code = byte_142 & 0x03
    b173 = ext[0] if len(ext) > 0 else 0
    b174 = ext[1] if len(ext) > 1 else 0
    if banks_code == 0x03:
        extra = b174 & 0x1F           # n < 32, meaning (n+1) banks of 8 lanes
        banks = extra + 1
    else:
        extra = None
        banks = (1, 2, 4)[banks_code]
    return {
        'network_path_pages_supported': bool((byte_142 >> 7) & 1),
        'vdm_pages_supported':          bool((byte_142 >> 6) & 1),
        'diagnostic_pages_supported':   bool((byte_142 >> 5) & 1),
        'coherent_pages_supported':     bool((byte_142 >> 4) & 1),
        'cmis_ff_supported':            bool((byte_142 >> 3) & 1),
        'page_03h_supported':           bool((byte_142 >> 2) & 1),
        'banks_supported':              banks,
        'max_lanes':                    banks * 8,
        'extra_lane_banks':             extra,
        'page_0ch_supported':           bool((b173 >> 7) & 1),
        'page_0dh_supported':           bool((b173 >> 6) & 1),
        'page_60h_supported':           bool((b174 >> 7) & 1),
        'page_61h_supported':           bool((b174 >> 6) & 1),
        'page_62h_supported':           bool((b174 >> 5) & 1),
    }


def parse_default_polarity(raw: bytes) -> list:
    """Per-lane default polarity from 01h:171-172 (Table 8-57), lane 1 first.

    'Inverted' here describes how the module is wired, not a control: it says
    the host has to invert its own polarity setting to match. It is advertised
    on Page 01h because a static-memory module has no other place to say so.
    """
    if len(raw) < 2:
        return []
    tx, rx = raw[0], raw[1]
    return [{'lane': i + 1,
             'input_tx_inverted':  bool((tx >> i) & 1),
             'output_rx_inverted': bool((rx >> i) & 1)}
            for i in range(8)]


def parse_extended_module_info(subtype_byte: int, heatsink_byte: int) -> dict:
    """Lower 60-61 (Table 8-18). HeatsinkType is the 5.4 addition.

    The code meanings live in SFF-8024, not in CMIS, so the raw value is
    reported rather than guessed at; zero is the spec's "not specified".
    """
    return {
        'module_subtype':   subtype_byte & 0x0F,
        'heatsink_type':    (heatsink_byte >> 4) & 0x0F,
        'fiber_face_type':  heatsink_byte & 0x03,
    }


def parse_misc_caps(byte_252: int) -> dict:
    """01h:252 (Table 8-62). Bit 5 is the 5.4 media lane switching advertisement."""
    return {'media_lane_switching_supported': bool((byte_252 >> 5) & 1)}


def parse_relative_thresholds(raw: bytes) -> dict:
    """12h:216-217 (Table 8-109), the 5.4 power-relative supervision thresholds.

    Offsets, not absolute powers, and deliberately not per-lane: the spec takes
    the view that a meaningful monitoring window is a property of the optics,
    not of which lane you are looking at. Both offsets are U4 counted from half
    a dB, so the smallest window a module can express is nominal +/- 0.5 dB.

    When a lane enables these (12h:128-135 bit 1), Page 02h's module-wide
    absolute thresholds stop applying to it and must not be shown as if they
    still did.
    """
    if len(raw) < 2:
        return {}
    hi, lo = raw[0], raw[1]
    return {
        'hi_alarm_offset_db': (1 + ((hi >> 4) & 0x0F)) * 0.5,
        'hi_warn_offset_db':  (1 + (hi & 0x0F)) * 0.5,
        'lo_alarm_offset_db': -(1 + ((lo >> 4) & 0x0F)) * 0.5,
        'lo_warn_offset_db':  -(1 + (lo & 0x0F)) * 0.5,
    }
