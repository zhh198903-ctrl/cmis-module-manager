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
REG_MEMORY_MODEL     = (None, 0x02, 1)   # [7] memory model, [6] SteppedConfigOnly,
                                         # [5:2] MciMaxSpeed, [1:0] AutoCommissioning
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
REG_FAULT_CAUSE      = (None, 0x29, 1)   # Lower 41: ModuleFaultCause (Table 8-16)
# Lower 56-57 (Table 8-18), both RO and Required, and both in the same
# table as the subtype byte below that this already reads.
# Lower 31-36 (Table 8-12), the Masks for the module-level Flags at 8-13.
REG_MODULE_FLAG_MASKS = (None, 0x1F, 6)
REG_CMIS_SM_SUPPORT  = (None, 0x38, 2)   # 56 CmisSmSupport, 57 FunctionType
REG_MODULE_SUBTYPE   = (None, 0x3C, 1)   # Lower 60: [3:0] SFF8024ModuleSubtype
REG_HEATSINK_FIBER   = (None, 0x3D, 1)   # Lower 61: [7:4] HeatsinkType (5.4), [1:0] FiberFaceType
REG_MEDIA_TYPE       = (None, 0x55, 1)   # Lower 85: Media Type Encoding (Table 8-20)
# Application Descriptors (AppSel 1..8) — 4 bytes per descriptor
REG_APP_DESC_BASE    = (None, 0x56, 4 * 8)  # 86..117 (8 descriptors × 4 bytes)
# Bank and page select are adjacent and are written together: the module
# defers a BankSelect until PageSelect is written (5.4 section 8.2.15).
REG_BANK_SELECT      = (None, 0x7E, 1)
REG_PAGE_SELECT      = (None, 0x7F, 1)

# ---------------------------------------------------------------------------
# Page 00h — Administrative Information (Table 8-27 overview)
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
# Page 1Ch (section 8.24, Table 8-172). 128-247 is fifteen 8-byte
# Normalized Application Descriptors; 248-255 is Reserved and is not a
# sixteenth.
REG_NAD_BLOCK        = (0x1C, 0x80, 120)

# Page 15h (section 8.18, Table 8-141). Banked - each bank is 8 host
# lanes - and present only when 01h:145.3 says so.
REG_DP_RX_LATENCY    = (0x15, 0xE0, 16)  # 224-239, 8 x U16 ns
REG_DP_TX_LATENCY    = (0x15, 0xF0, 16)  # 240-255, 8 x U16 ns
REG_CU_ATTENUATION   = (0x00, 0xCC, 5)   # 204-208 (Table 8-35); 209 is
                                         # Reserved and was inside this
                                         # read as a sixth attenuation
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
# 132-137 (Table 8-45, RO/Required): how far the module reaches on each fibre
# type. Read as one burst - the SMF multiplier can escape to 137, so the last
# byte is not optional.
REG_LINK_LENGTHS     = (0x01, 0x84, 6)   # 132-137
REG_BANKS_SUPPORTED  = (0x01, 0x8E, 1)   # 142  bits[1:0]
# --- CMIS 5.4 additions on Page 01h ---
REG_DEFAULT_POLARITY = (0x01, 0xAB, 2)   # 171-172 Default Input/Output polarity (Table 8-57)
REG_PAGES_EXT        = (0x01, 0xAD, 2)   # 173-174 Supported pages + extra banks (Table 8-58)
# 251 (Table 8-62), RO and Required. Four two-bit advertisements, of which
# FullPageReadSupported decides how many bytes a single READ may ask for:
# section 5.2.2.1 puts Nmax at 8 by default and 128 only when it is supported.
# 143-144 and 167-169 (Tables 8-48, 8-56), all RO and Required. How long the
# module says its own transient states may take, "so that hosts can determine
# when something failed in the module during these states, for example a
# module firmware hang up" - and how long it actually needs after a page
# change, which is not always the specification's worst case.
# 146-150 (Table 8-50): the temperature range the module is allowed to run in,
# the supply voltage it needs, and an AOC's cable delay. Each has its own way
# of saying "not specified", so an absent value is an answer rather than a gap.
# 138-141 (Table 8-46): the module's own nominal transmitter wavelength and
# tolerance. The Media Interface Technology code names a band; this is what
# the module says it actually emits, and on a programmable one it is the
# actual value rather than the standard's.
REG_WAVELENGTH       = (0x01, 0x8A, 4)   # 138-141
REG_MODULE_LIMITS    = (0x01, 0x92, 5)   # 146-150
REG_DURATIONS        = (0x01, 0x8F, 2)   # 143-144
REG_DURATIONS_EXT    = (0x01, 0xA7, 3)   # 167-169
REG_MISC_FEATURES    = (0x01, 0xFB, 1)   # 251
REG_MISC_CAPS        = (0x01, 0xFC, 1)   # 252  bit5 MediaLaneSwitchingSupported (Table 8-62)
REG_CDB_CAPS         = (0x01, 0xA3, 4)   # 163-166

# ---------------------------------------------------------------------------
# Page 02h — Thresholds (Table 8-63 overview)
# ---------------------------------------------------------------------------
# 144-175 (Table 8-64): four thresholds each for the three Aux monitors and
# the Custom monitor. The Aux readings were on screen with nothing to judge
# them by, though the module says where its own alarms and warnings sit.
REG_AUX_THRESHOLDS      = (0x02, 0x90, 32)  # 144-175
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
# Page 10h — Data Path Control (Table 8-77 overview):
#   128     Data Path Control
#   129-142 Lane-Specific Control   (Table 8-79)
#   143-177 Staged Control Set 0    (Tables 8-80 / 8-82 / 8-83 /
#                                    8-84 / 8-85)
# Bytes 133 and 140-142 are Reserved. Note there is deliberately no
# OutputSquelchForceRx counterpart to OutputSquelchForceTx (§8.9.2.3).
# ---------------------------------------------------------------------------
REG_DP_DEINIT        = (0x10, 0x80, 1)   # 128  DataPathDeinit
REG_TX_POL_FLIP      = (0x10, 0x81, 1)   # 129  InputPolarityFlipTx
REG_TX_OUTPUT_DIS    = (0x10, 0x82, 1)   # 130  OutputDisableTx
# 01h:155-156 (Table 8-51) says which of these lane controls the module
# actually implements. Offering one it does not is offering a control that
# writes a register the module ignores.
REG_SUPPORTED_CONTROLS = (0x01, 0x9B, 2)  # 155-156
REG_AUX_OBSERVABLE     = (0x01, 0x91, 1)  # 145 (Table 8-50)
REG_RX_TX_CHARACTER    = (0x01, 0x97, 1)  # 151 (Table 8-50)
# 152 (Table 8-50), the one byte of the Module Characteristics block
# 145-154 that was never read.
REG_CDR_POWER_SAVED    = (0x01, 0x98, 1)  # 152 CDRPowerSavedPerLane
REG_SI_MAXIMA          = (0x01, 0x99, 2)  # 153-154 (Table 8-50 continuation)
REG_SI_CONTROLS_ADV    = (0x01, 0xA1, 2)  # 161-162 (Table 8-54)

# Staged Control Set 0 on Page 10h (Tables 8-83, 8-84). The tool applies this
# set every time somebody presses Apply, so what is in here is committed
# whether or not anyone looked at it.
REG_SCS_TX_ADAPT_EQ    = (0x10, 0x99, 1)  # 153 AdaptiveInputEqEnableTx, 1b/lane
# 154-155. Table 6-5 groups this with Freeze and Store as the controls
# that apply when AdaptiveInputEqEnableTx is set; the target four bytes
# later is the one that applies when it is clear.
REG_SCS_TX_EQ_RECALL   = (0x10, 0x9A, 2)  # 154-155 AdaptiveInputEqRecallTx
REG_SCS_TX_EQ_TARGET   = (0x10, 0x9C, 4)  # 156-159 HostControlledInputEqTargetTx
# 160 CDREnableTx, the byte immediately before the Rx one. Advertised
# together by 01h:161.0-1, the same way 10h:161 is by 01h:162.0-1.
REG_SCS_TX_CDR         = (0x10, 0xA0, 1)  # 160 CDREnableTx, 1b/lane
REG_SCS_RX_CDR         = (0x10, 0xA1, 1)  # 161 CDREnableRx, 1b/lane
REG_SCS_RX_EQ_PRE      = (0x10, 0xA2, 4)  # 162-165 OutputEqPreCursorTargetRx
REG_SCS_RX_EQ_POST     = (0x10, 0xA6, 4)  # 166-169 OutputEqPostCursorTargetRx
REG_SCS_RX_AMPLITUDE   = (0x10, 0xAA, 4)  # 170-173 OutputAmplitudeTargetRx
REG_SUPPORTED_FLAGS    = (0x01, 0x9D, 2)  # 157-158 (Table 8-52)
REG_SUPPORTED_MONITORS = (0x01, 0x9F, 2)  # 159-160 (Table 8-53)
REG_TX_SQUELCH_DIS   = (0x10, 0x83, 1)   # 131  AutoSquelchDisableTx
# 134. Table 8-77 puts 129-142 under Lane-Specific Control, "independent
# of the Data Path State machine or control sets" - so unlike the two
# equalizer fields twenty bytes further on, this one is in force as read
# and needs no Apply.
REG_TX_ADAPT_EQ_FREEZE = (0x10, 0x86, 1)  # 134 AdaptiveInputEqFreezeTx
REG_TX_FORCE_SQUELCH = (0x10, 0x84, 1)   # 132  OutputSquelchForceTx
REG_RX_POL_FLIP      = (0x10, 0x89, 1)   # 137  OutputPolarityFlipRx
REG_RX_OUTPUT_DIS    = (0x10, 0x8A, 1)   # 138  OutputDisableRx
REG_RX_SQUELCH_DIS   = (0x10, 0x8B, 1)   # 139  AutoSquelchDisableRx
REG_APPLY_DATAPATH   = (0x10, 0x8F, 1)   # 143  ApplyDPInit, write 0xFF to apply
REG_APPLY_IMM        = (0x10, 0x90, 1)   # 144  ApplyImmediate
REG_APP_SELECT       = (0x10, 0x91, 8)   # 145-152 DPConfigLane1-8 (staged)
# Page 10h holds the Staged Control Set: what the host asked for. What the
# module is actually running is the Active Control Set on 11h (Table 8-92,
# 206-234). They differ whenever a configuration was rejected.
REG_ACTIVE_APP_SELECT = (0x11, 0xCE, 8)  # 206-213 Active DPConfigLane1-8

# ---------------------------------------------------------------------------
# Page 11h — DataPath Status & Monitoring (Table 8-92 overview)
# ---------------------------------------------------------------------------
REG_DP_STATE        = (0x11, 0x80, 4)   # 4 bytes, 4 bits/lane (nibble per lane)
REG_OUTPUT_STATUS_RX= (0x11, 0x84, 1)   # 132  Table 8-95, RO/Rqd, 1b/lane
REG_OUTPUT_STATUS_TX= (0x11, 0x85, 1)   # 133  Table 8-95, RO/Rqd, 1b/lane
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
# 8.14.2: "there is no Tx output status change reporting Flag defined". Only
# the Rx side has one, because only the Rx side can make a host act on it.
REG_RX_OUTPUT_CHANGED = (0x11, 0x99, 1) # 153  OutputStatusChangedFlagRx, RO/COR
REG_TX_POWER        = (0x11, 0x9A, 16)  # 8 lanes × 2B, ×0.1 µW
REG_TX_BIAS         = (0x11, 0xAA, 16)  # 8 lanes × 2B, ×2 µA
REG_RX_POWER        = (0x11, 0xBA, 16)  # 8 lanes × 2B, ×0.1 µW
REG_CONFIG_STATUS   = (0x11, 0xCA, 4)   # 4 bytes, 4 bits/lane (nibble per lane)
REG_DP_INIT_PENDING    = (0x11, 0xEB, 1)  # 235 DPInitPendingLane, Table 8-106
# 240-255 (Table 8-107): which media wavelength and which physical fibre each
# media lane actually is. On a WDM module several lanes share one fibre and
# differ only by wavelength; on a parallel one each has its own. A per-lane
# power reading means different things in the two cases.
REG_MEDIA_LANE_MAP     = (0x11, 0xF0, 16)  # 240-255 Tx1-8 then Rx1-8

# Tables 8-104 and 8-105: the Active Control Set's half of the signal
# integrity settings - what the module is actually provisioned with, one
# register for each of the staged ones on Page 10h. With ExplicitControl
# clear these "were determined by the module according to the selected
# Application", so they are not the staged values at all.
REG_ADDITIONAL_APPS    = (0x01, 0xDF, 28)  # 223-250 App 9-15, Table 8-61
# 175 (Table 8-59): how many banks of Normalized Application Descriptors the
# module has on Page 1Ch. Applications beyond the basic fifteen live there,
# and a host that never reads this "may even fall back to seeing only the
# first 15 Applications advertised in the Basic Application Descriptors".
REG_NAD_BANKS          = (0x01, 0xAF, 1)   # 175 NADBanksSupported
REG_MEDIA_LANE_ASSIGN  = (0x01, 0xB0, 15)  # 176-190 App 1-15, Table 8-60
REG_ACS_TX_ADAPT_EQ    = (0x11, 0xD6, 1)  # 214 AdaptiveInputEqEnableTx
REG_ACS_TX_EQ_RECALLED = (0x11, 0xD7, 2)  # 215-216 AdaptiveInputEqRecalledTx
REG_ACS_TX_EQ_TARGET   = (0x11, 0xD9, 4)  # 217-220 HostControlledInputEqTargetTx
REG_ACS_TX_CDR         = (0x11, 0xDD, 1)  # 221 CDREnableTx
REG_ACS_RX_CDR         = (0x11, 0xDE, 1)  # 222 CDREnableRx
REG_ACS_RX_EQ_PRE      = (0x11, 0xDF, 4)  # 223-226 OutputEqPreCursorTargetRx
REG_ACS_RX_EQ_POST     = (0x11, 0xE3, 4)  # 227-230 OutputEqPostCursorTargetRx
REG_ACS_RX_AMPLITUDE   = (0x11, 0xE7, 4)  # 231-234 OutputAmplitudeTargetRx

# ---------------------------------------------------------------------------
# Page 04h — Laser Capabilities (Table 8-68, RO)
# ---------------------------------------------------------------------------
REG_GRID_SUPPORTED   = (0x04, 0x80, 2)   # 128-129: grid support + fine tuning
REG_GRID_CHANNELS    = (0x04, 0x82, 36)  # 130-165: S16 low/high per grid (9 grids × 4)
                                         # 166-169 continues it with the 300 GHz
                                         # grid (5.4), read only when advertised
REG_FINE_RESOLUTION  = (0x04, 0xBE, 2)   # 190-191: U16 0.001 GHz units
REG_FINE_LOW_OFFSET  = (0x04, 0xC0, 2)   # 192-193: S16 0.001 GHz
REG_FINE_HIGH_OFFSET = (0x04, 0xC2, 2)   # 194-195: S16 0.001 GHz
REG_PROG_PWR_MIN     = (0x04, 0xC6, 2)   # 198-199: S16 0.01 dBm
REG_REL_THR_CAP      = (0x04, 0xC4, 1)   # 196: bit6 relative Tx power thresholds supported (5.4)
REG_PROG_PWR_MAX     = (0x04, 0xC8, 2)   # 200-201: S16 0.01 dBm

# ---------------------------------------------------------------------------
# Page 12h — Laser Tuning Control & Status (Table 8-109, banked)
# ---------------------------------------------------------------------------
REG_GRID_SPACING_TX  = (0x12, 0x80, 8)   # 128-135: 1B/lane [7:4]=grid [0]=FineTuneEn
REG_CHANNEL_NUM_TX   = (0x12, 0x88, 16)  # 136-151: S16/lane (2B × 8)
REG_FINE_OFFSET_TX   = (0x12, 0x98, 16)  # 152-167: S16/lane (2B × 8)
REG_CURRENT_FREQ_TX  = (0x12, 0xA8, 32)  # 168-199: U32/lane (4B × 8) in 0.001 GHz
REG_TARGET_PWR_TX    = (0x12, 0xC8, 16)  # 200-215: S16/lane (0.01 dBm)
REG_REL_THRESHOLDS   = (0x12, 0xD8, 2)   # 216-217: relative Tx power thresholds (5.4)
REG_TUNING_STATUS_TX = (0x12, 0xDE, 8)   # 222-229: 1B/lane [1]=TuningInProgress [0]=Unlocked
REG_TUNING_FLAG_SUM  = (0x12, 0xE6, 1)   # 230: one bit per lane, summary
REG_TUNING_FLAGS_TX  = (0x12, 0xE7, 8)   # 231-238: 1B/lane latched flags
# 239-246, the Masks for those Flags. One of the two Mask blocks in CMIS
# that default to masked - Table 8-109 writes "Default: 1" against every bit
# here, and Table 8-133 says the same of the diagnostics Masks in one line -
# so on a module out of reset none of these Flags reaches the host. The other
# two blocks (Tables 8-12 and 8-91) state no default at all.
REG_TUNING_FLAG_MASKS= (0x12, 0xEF, 8)   # 239-246: 1B/lane, RW, default 1

# Table 8-109. All RO/COR: the module answers a tuning request here, and the
# read that reports the answer is the read that clears it.
TUNING_FLAG_BITS = (
    (5, 'target_power_out_of_range', 'Target output power outside the advertised range'),
    (4, 'fine_tuning_out_of_range',  'Fine-tuning offset outside the advertised range'),
    (3, 'tuning_not_accepted',       'The module could not serve the tuning request; '
                                     'the programmed grid or channel does not match the laser'),
    (2, 'invalid_channel_number',    'Channel number outside the advertised range for '
                                     'the selected grid spacing'),
    (1, 'wavelength_unlocked',       'Laser wavelength unlocked'),
    (0, 'tuning_complete',           'Laser tuning completed'),
)


def parse_tuning_flags(byte_val: int) -> dict:
    """Decode one lane's Page 12h:231-238 tuning Flag byte."""
    return {name: bool((byte_val >> bit) & 1)
            for bit, name, _desc in TUNING_FLAG_BITS}


# Chapter 10 timings this tool waits on, in seconds. Every constant delay
# in the code is a claim about one of these, and the claims were being made
# without the numbers being looked up.
#
# The difference that matters is what happens to a host that is early:
#
#   Table 10-4    ACCESS hold-off. The module rejects the access, so being
#                 early is visible - "A host not willing to wait for
#                 specified maximum durations can retry a rejected ACCESS".
#   Table 10-5    Content dependency. Nothing is rejected. The section says
#                 it outright: "The module does not prevent access to stale
#                 data in these cases (i.e. ACCESS that is too early is not
#                 rejected)."
#   Table 10-6    Condition to Flag. Nothing is rejected either: the Flag is
#                 simply not up yet, which reads exactly like a condition
#                 that did not occur.
#
# So a wait short of the first kind produces an error, and a wait short of
# either of the other two produces an answer.
TIMING_SECONDS = {
    # Table 10-4. The module may advertise less in 01h:167; this is the
    # ceiling and the fallback.
    'tBPC': 0.010,
    # Table 10-5. From the STOP of the write to 14h:128 to a read that
    # retrieves the newly selected content rather than the previous
    # selector's bytes, decoded as whatever this one means.
    'tDDCS': 0.010,
    # Table 10-6. "Time from onset of condition or occurrence of event to
    # associated Flag bit raised". Reading a Flag sooner than this and
    # finding it clear says nothing.
    'ton_flag': 0.200,
    # Table 10-4 again: the longest ACCESS hold-off after a WRITE - tWRITE
    # for a volatile register (the I2C tNACK), tWRITENV for non-volatile
    # memory (tWR), which Page 03h, the user EEPROM, is (8.7).
    'tWRITE': 0.010,
    'tWRITENV': 0.080,
    # Table 10-2: from "Reset release until the START condition of a READ
    # retrieving the default register value". A SoftwareReset is that release
    # too - "the same as asserting the Reset hardware signal ... followed by
    # its de-assertion" (Table 8-11).
    'tMgmtInit': 2.0,
}


# Every element CMIS marks RO/COR. Table 8-3 defines the access type in
# one line: "All bits in a RO/COR Byte are cleared by the module after the
# Byte value has been read."
#
# So a read of one of these is destructive, and uniquely so: the module keeps
# no second copy. A latched Flag records that something happened - a checker
# that slipped, a data path that bounced, a laser that refused a channel -
# and once read it is gone unless whoever read it kept the answer. Every
# panel in this tool that reads one folds it into the flag history for that
# reason. A raw read from the register panel does not, and nothing said so.
#
# (page, or None for Lower Memory; first byte; last byte; what it holds)
CLEAR_ON_READ_BLOCKS = (
    (None, 0x08, 0x0D, 'module Flags (Table 8-9) - temperature, supply '
                       'voltage, Aux and Custom monitor thresholds, the '
                       'module state change and firmware Flags, and CDB '
                       'command completion'),
    (0x11, 134, 153, 'lane Flags (Tables 8-96 to 8-98) - data path state '
                     'changes, Tx failure, LOS, CDR loss of lock, adaptive '
                     'equalizer failure and every optical threshold'),
    # 230 is the summary and is plain RO: it reads as whatever 231-238 say,
    # so it is not cleared by reading it - only by the Flags underneath going
    # away.
    (0x12, 231, 238, 'laser tuning Flags (Table 8-109) - the only record '
                     'that the module refused a tuning request'),
    (0x14, 132, 139, 'diagnostics Flags (Table 8-138) - loss of reference '
                     'clock, gating complete, pattern generator and checker '
                     'loss of lock'),
    (0x17, 128, 128, 'Network Path State Changed Flags (Table 8-163)'),
    # Table 8-177 gives the whole of Page 2Ch to "Supervision Flag Quads ...
    # (RO/COR access)", numbering them by quad rather than by byte. However
    # the numbering is read, every byte of the page is a latched Flag.
    (0x2C, 128, 255, 'VDM threshold crossing Flags (Table 8-177) - every '
                     'byte of this page is latched'),
)


# Table 8-3: a READ from a WO element "delivers unpredictable values", and
# from a WO/SC one "a zero value" once the module has taken it. Either way
# what reads back is not what was written - for the password areas that is
# the point of the type ("mainly useful when privacy protection of written
# data is to be specified").
# (page, or None for Lower Memory; first; last; access; what it is)
WRITE_ONLY_BLOCKS = (
    (None, 118, 121, 'WO/SC', 'PasswordChangeEntryArea (Table 8-25)'),
    (None, 122, 125, 'WO/SC', 'PasswordEntryArea (Table 8-25)'),
    (0x10, 143, 143, 'WO', 'ApplyDPInit, a trigger'),
    (0x10, 144, 144, 'WO', 'ApplyImmediate, a trigger'),
    (0x60, 192, 193, 'WO', 'ResetAcquisitionCounters (Table 8-189), a trigger'),
    (0x6D, 160, 160, 'WO/SC', 'CommitMediaLaneRedirection (Table 8-196), a trigger'),
)

# Page 10h, ApplyDPInit and ApplyImmediate: "Restriction: This byte must be
# written in a single-byte WRITE".
SINGLE_BYTE_WRITE = ((0x10, 143, 'ApplyDPInit'), (0x10, 144, 'ApplyImmediate'))


def write_only_overlap(page, address, length):
    """The write-only blocks a read of this range reaches, as
    clear_on_read_overlap reports the latched ones."""
    last = address + max(length, 1) - 1
    out = []
    for blk_page, first, blk_last, access, what in WRITE_ONLY_BLOCKS:
        if blk_page != (None if address < 0x80 else page):
            continue
        lo, hi = max(address, first), min(last, blk_last)
        if lo > hi:
            continue
        out.append({'page': blk_page, 'first': lo, 'last': hi,
                    'access': access, 'holds': what})
    return out


def clear_on_read_overlap(page, address, length):
    """The clear-on-read blocks a read of this range would touch.

    `page` is None for Lower Memory. Returns one entry per block the read
    overlaps, with the bytes of that block it actually reaches - a read that
    clips the edge of a Flag block still destroys the part it reaches, and
    saying "11h:134-153" when two bytes were read would overstate it.
    """
    last = address + max(length, 1) - 1
    out = []
    for blk_page, first, blk_last, what in CLEAR_ON_READ_BLOCKS:
        if blk_page != (None if address < 0x80 else page):
            continue
        lo, hi = max(address, first), min(last, blk_last)
        if lo > hi:
            continue
        out.append({
            'page': blk_page,
            'first': lo,
            'last': hi,
            'holds': what,
        })
    return out


# Which side of the module each lane Flag in 11h:134-153 is about, taken
# from the sentence in each row of Tables 8-96, 8-97 and 8-98 rather than
# from the Tx/Rx in its name. The two do not agree, and nothing about the
# block's layout gives it away:
#
#   134       DPStateChanged        "host lane <i>"
#   135       FailureFlagTx         "affecting media lane <i>"
#   136-138   LOSFlagTx, CDRLOLFlagTx, AdaptiveInputEqFailFlagTx
#                                   "host lane <i>" - the Tx *input* arrives
#                                   from the host
#   139-146   OpticalPowerTx, LaserBiasTx thresholds   "media lane <i>"
#   147-152   LOSFlagRx, CDRLOLFlagRx, OpticalPowerRx  "media lane <i>"
#   153       OutputStatusChangedFlagRx                "host lane <i>"
#
# So the Tx group is split three ways and the Rx group two, and a reader who
# took "Tx" for one side and "Rx" for the other would be wrong about five of
# the twenty.
LANE_FLAG_SIDE = {
    'dp_state_changed':    'host',
    'tx_fault':            'media',
    'tx_los':              'host',
    'tx_cdr_lol':          'host',
    'tx_adaptive_eq_fail': 'host',
    'tx_power_high_alarm': 'media',
    'tx_power_low_alarm':  'media',
    'tx_power_high_warn':  'media',
    'tx_power_low_warn':   'media',
    'tx_bias_high_alarm':  'media',
    'tx_bias_low_alarm':   'media',
    'tx_bias_high_warn':   'media',
    'tx_bias_low_warn':    'media',
    'rx_los':              'media',
    'rx_cdr_lol':          'media',
    'rx_power_high_alarm': 'media',
    'rx_power_low_alarm':  'media',
    'rx_power_high_warn':  'media',
    'rx_power_low_warn':   'media',
    'rx_output_changed':   'host',
}


def media_lane_groups(groups, app_select, apps, media_lanes=8) -> dict:
    """Which media lanes each Data Path occupies, by the rule in 7.9.1.

    The host picks host lanes; the media lanes follow from them, and CMIS
    makes the derivation deterministic:

      "The first application instance (in host lane numbering sequence) will
      use the media lane group starting at the lowest numbered available
      media lane advertised for that application. This will consume a
      particular set of consecutively numbered media lanes. The second
      application will use the media lane group starting at the next lowest
      numbered available media lane advertised for that application, and so
      forth. The media lanes assigned to an application therefore depend on
      the number of media lanes required by parallel applications using lower
      host lane numbers."

    "advertised for that application" is MediaLaneAssignmentOptions
    (01h:176-190), a bitmap of media lanes 1-8 this tool already reads and
    shows as a bitmap on the Applications tab.

    Returned as {first host lane of the Data Path: [media lane, ...]}. A Data
    Path whose Application advertises no bitmap - a flat memory module has no
    Page 01h to put one on - is left out rather than guessed at.

    Note this is the nominal allocation. A module advertising
    MediaLaneSwitchingSupported can redirect it, and then Page 6Dh's
    committed mapping is the truth; the caller says which it is showing.
    """
    by_sel = {a.get('app_sel'): a for a in apps}
    taken = set()
    out = {}
    for group in sorted(groups or [], key=lambda g: g[0] if g else 0):
        if not group:
            continue
        first = group[0]
        sel = app_select[first - 1] if first - 1 < len(app_select) else 0
        app = by_sel.get(sel)
        if not sel or app is None:
            continue
        mask = app.get('media_lane_assign_mask')
        width = app.get('media_lanes') or 0
        if not mask or not width:
            continue
        # The lowest advertised start whose whole run is still free. Both
        # halves matter: a start that is advertised but overlaps an earlier
        # Data Path is not "available", and one that is free but not
        # advertised is not a start this Application may use.
        for start in range(1, media_lanes + 1):
            if not (mask >> (start - 1)) & 1:
                continue
            lanes = list(range(start, start + width))
            if lanes[-1] > media_lanes or taken.intersection(lanes):
                continue
            taken.update(lanes)
            out[first] = lanes
            break
    return out


def lane_start_violations(groups, app_select, apps) -> list:
    """Data Paths that begin on a lane their Application may not start on.

    6.2.3.2.1 states it as an obligation on the host: "The host must assign
    lanes to Data Paths in accordance with the Lane Assignment Options field
    advertised by the module for that Application." The field is
    HostLaneAssignmentOptions, the fourth Application Descriptor byte - "Bits
    0-7 form a bit map corresponding to Host Lanes 1-8. A bit value of 1
    indicates that the lane group of the advertised Application can begin on
    the corresponding host lane."

    The rule is about where a Data Path *begins*, not about which lane holds
    which code. A four-lane Application starting on lane 1 occupies lanes 2-4
    as well, and the host writes the same AppSel into all four - those are
    continuation lanes and the bitmap says nothing about them. Checking each
    lane against the bitmap instead would flag three lanes out of every four
    on a conformant module.

    A module answers ConfigRejectedInvalidDataPath (4h, Table 8-101,
    "invalid set of lanes for AppSel") to an Apply that breaks this.

    `groups` are lane numbers, 1-based, as _datapath_groups reports them.
    """
    masks = {}
    for a in apps:
        mask = a.get('host_lane_assign_mask')
        # None on a flat memory module, whose fourth descriptor byte is the
        # HostInterfaceGID and says nothing about lane groups. Zero is a
        # module advertising that the Application can begin nowhere, which is
        # not a constraint anyone can satisfy - read as no statement rather
        # than as a refusal of every lane.
        if mask:
            masks[a.get('app_sel')] = mask
    out = []
    for group in groups or []:
        if not group:
            continue
        first = group[0]
        sel = app_select[first - 1] if first - 1 < len(app_select) else 0
        # No `if not sel` guard: AppSel 0 is the absence of an Application
        # and cannot carry a bitmap, because the descriptor array is numbered
        # from 1 - so the lookup below already declines it. A second test for
        # the same thing reads as a rule and is not one.
        mask = masks.get(sel)
        if mask is None:
            continue
        # "Bits 0-7 form a bit map corresponding to Host Lanes 1-8". On a
        # module wider than eight lanes the specification does not say how
        # the eight bits extend - whether lane 9 is read as bank 1's lane 1
        # or is simply not covered - and answering that here would enforce a
        # rule 5.4 does not have. Checked where the bitmap's meaning is
        # unambiguous, and silent past it.
        if first > 8:
            continue
        if not (mask >> (first - 1)) & 1:
            out.append({
                'lane': first,
                'app_sel': sel,
                'lanes': list(group),
                'allowed_starts': [b + 1 for b in range(8) if (mask >> b) & 1],
                'mask': mask,
            })
    return out


def diag_mask_addr(flag_addr: int) -> int:
    """The Page 13h Mask byte that governs a Page 14h diagnostics Flag byte.

    The two blocks are positional: 14h:132 is masked by 13h:206, and each
    byte after it by the byte after that. Written out at every call site this
    is an offset that looks like a typo and reads like one.
    """
    first_flag, first_mask = FLAG_MASK_BLOCKS[2][1], FLAG_MASK_BLOCKS[2][3]
    return first_mask + (flag_addr - first_flag)


def parse_tuning_masks(byte_val: int) -> dict:
    """Decode one lane's Page 12h:239-246 Mask byte.

    Table 8-109 gives the Masks the same bit positions and the same names as
    the Flags they suppress, so the two decode through one table - a Mask
    read with a table of its own is a table that can drift from the Flags it
    is supposed to line up with.
    """
    return parse_tuning_flags(byte_val)


def tuning_summary_bit(summary_bytes, lane_index: int) -> bool:
    """12h:230 for one media lane, across banks.

    One byte per bank and one bit per lane within it, lane 1 in bit 0 - so
    media lane 9 is bit 0 of the next bank's byte, not bit 8 of a wider
    number. Inline in the caller this is three index expressions that all
    look alike and fail silently on a module with one media lane.
    """
    idx = lane_index // 8
    if idx < 0 or idx >= len(summary_bytes):
        return False
    return bool((summary_bytes[idx] >> (lane_index % 8)) & 1)


def tuning_summary_disagreements(summary: int, flag_bytes: bytes,
                                 lanes: int = 8) -> list:
    """Lanes where 12h:230 and 12h:231-238 contradict each other.

    Table 8-109 defines the summary as exact, not advisory: bit <n>-1 "is set
    if and only if any of the Flags in Bytes 231-238 are 1 for the particular
    Lane <n>". Both directions are a fault, and they fail differently - a
    summary bit with no Flag behind it sends the host looking for a condition
    that is not there, and a Flag with no summary bit is a condition the
    procedure in that same note never arrives at.
    """
    out = []
    for i in range(min(lanes, len(flag_bytes))):
        said = bool((summary >> i) & 1)
        has = bool(flag_bytes[i])
        if said != has:
            out.append({'lane': i + 1, 'summary': said, 'flags': has})
    return out


def parse_grid_channel_ranges(data: bytes) -> dict:
    """04h:130 onwards, an S16 low/high pair per grid code.

    The module says here which channel numbers are legal on each grid it
    supports, which is the only way for a host to know before it writes.

    Codes 0-8 occupy 130-165 and CMIS 5.4 continued the same table at 166-169
    with grid code 9, the 300 GHz grid. How many codes this covers is decided
    by how many bytes the caller passes: 166-169 is not required to mean
    anything on a module that does not advertise that grid.
    """
    import struct as _struct
    out = {}
    for code in range(len(data) // 4):
        off = code * 4
        if off + 4 > len(data):
            break
        low = _struct.unpack('>h', data[off:off + 2])[0]
        high = _struct.unpack('>h', data[off + 2:off + 4])[0]
        if low or high:
            out[code] = [low, high]
    return out

def advertised_grid_codes(grid_sup: bytes) -> list:
    """The GridSpacingTx codes (12h:128.7-4) this module advertises.

    Table 8-68: 04h:128 bit n is GridSupported for code n (3.125 GHz up to
    75 GHz), 04h:129.6 the 150 GHz grid (code 8), 04h:129.5 the 300 GHz grid
    (code 9). The channel ranges at 04h:130-169 are RO Rqd for every grid,
    so they are no evidence of support either way.
    """
    b128 = grid_sup[0] if len(grid_sup) > 0 else 0
    b129 = grid_sup[1] if len(grid_sup) > 1 else 0
    codes = [c for c in range(8) if (b128 >> c) & 1]
    if (b129 >> 6) & 1:
        codes.append(8)
    if (b129 >> 5) & 1:
        codes.append(9)
    return codes


def advertised_grid_ranges(grid_sup: bytes, data: bytes) -> dict:
    """{grid code: [low n, high n]} for the advertised grids only.

    Deciding by the range bytes instead - a grid listed when its pair was
    non-zero - offered a grid the module does not advertise wherever those
    bytes held anything, and dropped an advertised one whose plan is the
    single channel n = 0 (193.1 THz), which reads [0, 0].
    """
    import struct as _struct
    out = {}
    for code in advertised_grid_codes(grid_sup):
        off = code * 4
        if off + 4 <= len(data):
            out[code] = [_struct.unpack('>h', data[off:off + 2])[0],
                         _struct.unpack('>h', data[off + 2:off + 4])[0]]
    return out


# ---------------------------------------------------------------------------
# Page 13h — Diagnostic Controls (Tables 8-110..8-134)
# Each PRBS block is 8 bytes per side:
#   +0 Enable, +1 DataInvert, +2 ByteSwap, +3 Pre/PostFEC, +4..+7 PatternSelect
# ---------------------------------------------------------------------------
REG_HOST_PRBS_GEN    = (0x13, 0x90, 8)   # 144-151
REG_MEDIA_PRBS_GEN   = (0x13, 0x98, 8)   # 152-159
REG_HOST_PRBS_CHK    = (0x13, 0xA0, 8)   # 160-167
REG_MEDIA_PRBS_CHK   = (0x13, 0xA8, 8)   # 168-175
# 184-191 HostScratchPad0-7 (Table 8-132), RW, advertised in 01h:251.7-6.
REG_HOST_SCRATCHPAD  = (0x13, 0xB8, 8)
# 128-142 are the diagnostic capability advertisements: which loopbacks the
# module has, how it can gate a measurement, and which patterns each generator
# and checker actually supports. Offering the rest is offering nothing.
REG_DIAG_CAPS        = (0x13, 0x80, 15)  # 128-142
# 176-179 (Table 8-127): where each pattern generator and checker takes its
# clock from. Whether a lost reference clock invalidates a pattern run is a
# question about these bytes, not about the flag on its own.
# 224-255 (Table 8-134): the pattern that Pattern ID 15 sends. Offering
# "User Pattern" in a dropdown without this is offering to transmit whatever
# happens to be in these bytes.
# 206-213, the Masks for the diagnostics Flags on 14h:132-139. Table
# 8-133 states the default in one line: "The default value for all Mask
# bits on this page is 1 (masked)." So a module nobody has configured
# raises none of these to the host.
REG_DIAG_FLAG_MASKS  = (0x13, 0xCE, 8)   # 206-213
REG_USER_PATTERN     = (0x13, 0xE0, 32)  # 224-255
REG_CLOCK_MEAS       = (0x13, 0xB0, 4)   # 176-179
REG_MEDIA_OUT_LB     = (0x13, 0xB4, 1)
REG_MEDIA_IN_LB      = (0x13, 0xB5, 1)
REG_HOST_OUT_LB      = (0x13, 0xB6, 1)
REG_HOST_IN_LB       = (0x13, 0xB7, 1)

# ---------------------------------------------------------------------------
# Page 14h — Diagnostic Results (Tables 8-135..8-139)
# ---------------------------------------------------------------------------
REG_DIAG_SELECTOR    = (0x14, 0x80, 1)
# Table 8-138 "Latched Diagnostics Flags" has five flag bytes; the checker
# pair below was the only one read. A generator that has lost lock is not
# sending the pattern the table says it is, and a module that has lost its
# reference clock cannot be measuring anything - both are RO/COR, so the read
# that reports them is also the read that clears them.
REG_REF_CLOCK_LOL    = (0x14, 0x84, 1)   # LossOfReferenceClockFlag (132)
REG_HOST_GATE_DONE   = (0x14, 0x86, 1)   # PatternCheckGatingComplete Host (134)
REG_MEDIA_GATE_DONE  = (0x14, 0x87, 1)   # PatternCheckGatingComplete Media (135)
REG_HOST_GEN_LOL     = (0x14, 0x88, 1)   # PatternGeneratorLOL Host (136)
REG_MEDIA_GEN_LOL    = (0x14, 0x89, 1)   # PatternGeneratorLOL Media (137)
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

# Table 8-94 (Data Path State Encoding), reported per host lane at
# 11h:128-131 - Table 8-93 names those fields DPStateHostLane<i>.
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

# Figure 6-5 splits these into steady and transient states: a transition
# signal (name suffix S) is the exit condition of a steady state, while a
# transient state exits on its own completion. The difference matters on
# screen - a Data Path passing through DPTxTurnOn is coming up, and showing
# it the way a DPDeactivated lane is shown says the opposite.
DP_STATE_KIND = {
    "Deactivated": 'down',        # steady, and really down
    "Initialized": 'holding',     # steady: initialised, Tx not turned on
    "Activated":   'up',          # steady, carrying traffic
    "Init":        'transient',
    "Deinit":      'transient',
    "TxTurnOn":    'transient',
    "TxTurnOff":   'transient',
}


# Table 6-21 (Lane-Specific Flagging Conformance Rules): the DataPath states
# in which a module may set each lane Flag on Page 11h. In the others the
# Flag is "N/A" - the module does not raise it there - so a clear bit read in
# such a state says nothing about the lane. A set bit still means something:
# Flags latch, and one raised before the state changed stays until read.
_DP_EVERY_STATE = frozenset(('Deactivated', 'Init', 'Deinit', 'Initialized',
                             'TxTurnOn', 'TxTurnOff', 'Activated'))
_DP_INITIALIZED_ON = frozenset(('Initialized', 'TxTurnOn', 'TxTurnOff',
                                'Activated'))
FLAG_ALLOWED_STATES = {
    'dp_state_changed':    frozenset(('Deactivated', 'Initialized', 'Activated')),
    'tx_fault':            _DP_EVERY_STATE,
    'tx_los':              _DP_INITIALIZED_ON,
    'tx_cdr_lol':          _DP_INITIALIZED_ON,
    'tx_adaptive_eq_fail': _DP_INITIALIZED_ON | {'Init'},
    'tx_power_high_alarm': _DP_EVERY_STATE,
    'tx_power_low_alarm':  _DP_INITIALIZED_ON,
    'tx_power_high_warn':  _DP_EVERY_STATE,
    'tx_power_low_warn':   _DP_INITIALIZED_ON,
    'tx_bias_high_alarm':  _DP_EVERY_STATE,
    'tx_bias_low_alarm':   _DP_INITIALIZED_ON,
    'tx_bias_high_warn':   _DP_EVERY_STATE,
    'tx_bias_low_warn':    _DP_INITIALIZED_ON,
    'rx_los':              _DP_EVERY_STATE,
    'rx_cdr_lol':          _DP_INITIALIZED_ON,
    'rx_power_high_alarm': _DP_EVERY_STATE,
    'rx_power_low_alarm':  _DP_INITIALIZED_ON,
    'rx_power_high_warn':  _DP_EVERY_STATE,
    'rx_power_low_warn':   _DP_INITIALIZED_ON,
    'rx_output_changed':   _DP_INITIALIZED_ON,
}
# The same table's Note 1, the entries it marks "allowed1": in DPInitialized
# these are N/A "for media lanes where the Tx output is squelched or disabled
# by the host".
FLAGS_NA_TX_OFF_INITIALIZED = frozenset((
    'tx_power_low_alarm', 'tx_power_low_warn', 'tx_bias_high_alarm',
    'tx_bias_low_alarm', 'tx_bias_high_warn', 'tx_bias_low_warn'))


# Table 6-21's Page 12h rows, which CMIS 5.3 added (E07). The four Flags
# that answer a request - power or fine tuning out of range, not accepted,
# invalid channel - are allowed in every state; these two are not.
TUNING_FLAG_ALLOWED_STATES = {
    'wavelength_unlocked': _DP_INITIALIZED_ON | {'Init'},
    'tuning_complete':     _DP_INITIALIZED_ON,
}


def tuning_flags_not_allowed(dp_state) -> list:
    """The Page 12h tuning Flags Table 6-21 does not allow in `dp_state`.

    None (a media lane no Data Path carries) or a reserved state rules
    nothing out.
    """
    if dp_state not in _DP_EVERY_STATE:
        return []
    return sorted(n for n, ok in TUNING_FLAG_ALLOWED_STATES.items()
                  if dp_state not in ok)


def flags_not_allowed(dp_state: str, tx_off_by_host: bool = False) -> list:
    """The Page 11h lane Flags Table 6-21 does not allow in `dp_state`.

    A reserved state encoding has no rule, so nothing is ruled out there.
    """
    if dp_state not in _DP_EVERY_STATE:
        return []
    out = {n for n, ok in FLAG_ALLOWED_STATES.items() if dp_state not in ok}
    if dp_state == 'Initialized' and tx_off_by_host:
        out |= FLAGS_NA_TX_OFF_INITIALIZED
    return sorted(out)


# Section 6.2.4: an Apply trigger aimed at a Data Path in one of the four
# transient states is discarded - "the module silently ignores requests
# received while still being in a transient state" - and ApplyImmediate is
# ignored anywhere but the two initialized states. A silent discard is the
# one outcome the operator cannot tell from success, so the host has to know
# these two sets rather than write and hope.
DP_STATES_TRANSIENT = ("Init", "Deinit", "TxTurnOn", "TxTurnOff")
DP_STATES_APPLY_IMMEDIATE = ("Initialized", "Activated")


def dp_state_is_transient(state: str) -> bool:
    return state in DP_STATES_TRANSIENT


def dp_state_takes_apply_immediate(state: str) -> bool:
    return state in DP_STATES_APPLY_IMMEDIATE


# Section 6.3.3: setting the permitted alarm and warning Flags of Data Path
# related monitors, and the interrupts that go with them, "is only assured in
# the DPInitialized and DPActivated states". Everywhere else the module still
# publishes a power reading, so a lane on its way down reports -40 dBm and a
# table that colours by threshold calls it a fault - which is the one thing
# the module has not promised. Two of the seven states, and only these two.
DP_STATES_MONITORS_ASSURED = ("Initialized", "Activated")


def dp_monitors_assured(state: str) -> bool:
    """Whether this lane's monitors and Flags mean anything yet."""
    return state in DP_STATES_MONITORS_ASSURED


# The four transient states each have a MaxDuration advertisement (Tables
# 8-48 and 8-56). The steady states have none: they last as long as the
# module is left in them.
DP_STATE_DURATION_FIELD = {
    "Init":     'dp_init',
    "Deinit":   'dp_deinit',
    "TxTurnOn": 'dp_tx_turn_on',
    "TxTurnOff": 'dp_tx_turn_off',
}


def dp_state_kind(name: str) -> str:
    """Which of those a DataPath state name is; 'unknown' for the reserved
    encoding, which is not something to colour as either."""
    return DP_STATE_KIND.get(name, 'unknown')


# Table 8-101 (Configuration Command Execution and Result Status Codes),
# 4 bits per lane. 8-91 is Lane-Specific Masks on Page 10h.
CONFIG_STATUS_NAMES = {
    0x0: "ConfigUndefined",
    0x1: "ConfigSuccess",
    0x2: "ConfigRejected",
    0x3: "ConfigRejectedInvalidAppSel",
    0x4: "ConfigRejectedInvalidDataPath",
    0x5: "ConfigRejectedInvalidSI",
    0x6: "ConfigRejectedLanesInUse",
    0x7: "ConfigRejectedPartialDataPath",
    0x8: "ConfigRejectedNoEmulation",
    0xC: "ConfigInProgress",
}

# Table 8-101 labels 2h-Bh and Dh-Fh "Negative Result Status" as one block, so
# every code in those ranges is a rejection - including the ones it leaves
# reserved and the custom ones it does not name. Deciding by the name prefix
# read those as anything but a rejection, and a module answering, say, 9h got a
# "configuration applied" toast.
CONFIG_STATUS_REJECTED = frozenset(list(range(0x2, 0xC)) + list(range(0xD, 0x10)))

# Table 8-101, Ch: "a new configuration command is ignored for this lane while
# ConfigInProgress".
CONFIG_IN_PROGRESS = 0xC


def config_status_name(nibble: int) -> str:
    """Table 8-101 leaves the Name column empty for 9h-Bh and Dh-Fh, but not
    the meaning: they sit inside the Negative Result Status block, 9h-Bh as
    "other validation failures" the spec reserved and Dh-Fh as rejections "for
    custom reasons". Calling those Unknown told the operator the module had
    said something unintelligible, when what it said was that it had refused
    the configuration."""
    if nibble in CONFIG_STATUS_NAMES:
        return CONFIG_STATUS_NAMES[nibble]
    kind = 'custom' if nibble >= 0xD else 'reserved'
    return 'Rejected (%s %Xh)' % (kind, nibble)


def parse_config_status_codes(data: bytes) -> list:
    """The raw ConfigStatus nibbles at Page 11h:202-205, one per lane."""
    codes = []
    for lane in range(8):
        byte_idx = lane // 2
        if byte_idx >= len(data):
            codes.append(None)
            continue
        codes.append((data[byte_idx] >> ((lane % 2) * 4)) & 0x0F)
    return codes

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

# Table 8-41 — Media Interface Technology encodings
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
    0x0F: "Copper linear active equalizers (deprecated)",
    0x10: "C-band tunable laser",
    0x11: "L-band tunable laser",
    0x12: "Copper near-far end linear active equalizers",
    0x13: "Copper far end linear active equalizers",
    0x14: "Copper near end linear active equalizers",
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


def uw_to_dbm(uw: float):
    """Power in dBm, or None for 0 uW - which has no dBm value.

    The registers count in 0.1 uW (Table 8-99, Table 8-65), so the smallest
    power they express is -40 dBm and a zero is no power at all. This used to
    return -40.0 for zero: a dark receiver read as a -40 dBm measurement, and
    a 0 uW low threshold - one no reading can ever cross - as an alarm set at
    -40 dBm.
    """
    if uw <= 0:
        return None
    return 10.0 * math.log10(uw / 1000.0)


def parse_tx_bias_ma(raw: bytes, scale: int = 1) -> float:
    """Tx bias in mA. The register counts 2 uA increments, but 01h:160.4-3
    multiplies that by 1, 2 or 4 (Table 8-53): 65535 increments of 2 uA stop
    at 131 mA, so anything above that has to scale. Ignoring the factor
    understates every bias reading and threshold by 2x or 4x.
    """
    return struct.unpack(">H", raw[:2])[0] * 0.002 * scale


def parse_ascii(raw: bytes) -> str:
    return raw.decode("ascii", errors="replace").rstrip("\x00 ")


def parse_oui(raw: bytes) -> str:
    """Format 3-byte IEEE OUI as XX-XX-XX hex string."""
    return "-".join(f"{b:02X}" for b in raw[:3])


# Lower 41 ModuleFaultCause (Table 8-16), RO Opt.: "Reason of entering the
# ModuleFault state".
MODULE_FAULT_CAUSES = {
    0: 'No fault detected (or field not supported)',
    1: 'TEC runaway',
    2: 'Data memory corrupted',
    3: 'Program memory corrupted',
    4: 'Transmitter fault',
    5: 'Receiver fault',
    6: 'Temperature related fault',
}


def parse_module_fault_cause(code: int) -> dict:
    """Lower 41 (Table 8-16): 1-6 defined, 7-31 reserved fault codes, 32-63
    custom fault codes, 64-255 reserved. 0 is both "no fault detected" and
    "field not supported", so it is no cause either way."""
    if code in MODULE_FAULT_CAUSES:
        return {'code': code, 'name': MODULE_FAULT_CAUSES[code],
                'kind': 'none' if code == 0 else 'defined'}
    if 32 <= code <= 63:
        return {'code': code, 'name': 'Vendor fault code %d' % code,
                'kind': 'custom'}
    return {'code': code, 'name': 'Reserved code %d' % code,
            'kind': 'reserved'}


def parse_fw_capabilities(byte_val: int) -> dict:
    """0Dh:128 CapabilitiesRegister (Table 8-76)."""
    return {'cdb_download': bool(byte_val & 0x80),
            'fixed_load_provides_service': bool(byte_val & 0x08),
            'fixed_bank': bool(byte_val & 0x04),
            'bank_b': bool(byte_val & 0x02),
            'bank_a': bool(byte_val & 0x01)}


def parse_fw_loads_status(byte_val: int) -> dict:
    """0Dh:136 LoadsStatusRegister (Table 8-76): per Bank A and B, validity
    - 0b is valid, "the unexpected zero-encoding of a positive validity
    status maintains backwards compatibility with CMIS 4.0" - then committed
    and running. Its footnote: 00h, 04h, 40h and 44h "indicate that a
    Factory Load is running"."""
    banks = {}
    for name, shift in (('A', 0), ('B', 4)):
        banks[name] = {'valid': not ((byte_val >> (shift + 2)) & 1),
                       'committed': bool((byte_val >> (shift + 1)) & 1),
                       'running': bool((byte_val >> shift) & 1)}
    return {'banks': banks,
            'factory_running': byte_val in (0x00, 0x04, 0x40, 0x44)}


def parse_version_descriptor(raw: bytes) -> dict:
    """Table 8-75: MajorVersion, MinorVersion, U16 BuildNumber and an
    ASCII[32] Description."""
    return {'major': raw[0], 'minor': raw[1],
            'build': (raw[2] << 8) | raw[3],
            'description': bytes(raw[4:36]).decode('ascii', 'replace')
            .rstrip(' ' + chr(0))}


def parse_module_state(byte_val: int) -> str:
    """Decode lower memory byte 0x03: bits[3:1] = ModuleState, bit0 = InterruptDeasserted."""
    state = (byte_val >> 1) & 0x07
    return MODULE_STATES.get(state, f"Unknown({state})")


def parse_interrupt_asserted(byte_val: int) -> bool:
    """Bit 0: 1=not asserted (default), 0=asserted (inverted sense)."""
    return (byte_val & 0x01) == 0


def dp_state_name(nibble: int) -> str:
    """Table 8-94 names 0h and 8h-Fh Reserved - it does define them.

    Calling those Unknown says the tool did not recognise what the module
    reported, which sends the reader looking for a newer tool. What the module
    actually did was report an encoding the standard reserves, which is a
    question about the module.

    The same distinction the ConfigStatus decoder already draws one table
    over; this one had not been given it.
    """
    return DP_STATE_NAMES.get(nibble, 'Reserved (%Xh)' % nibble)


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
        states.append(dp_state_name(nibble))
    return states


# Table 8-107 gives each fibre code two names, one plain and one from the
# hardware specification's TR/RT numbering.
_FIBER_NAMES = {0: None, 1: 'fibre 1 (TR1)', 2: 'fibre 2 (RT1)',
                3: 'fibre 3 (TR2)', 4: 'fibre 4 (RT2)',
                5: 'fibre 5 (TR3)', 6: 'fibre 6 (RT3)',
                7: 'fibre 7 (TR4)', 8: 'fibre 8 (RT4)'}
# The hardware specification's own name for the same fibre, which is what
# fits in a table cell.
_FIBER_SHORT = {0: None, 1: 'TR1', 2: 'RT1', 3: 'TR2', 4: 'RT2',
                5: 'TR3', 6: 'RT3', 7: 'TR4', 8: 'RT4'}


def parse_media_lane_mapping(data: bytes, lanes: int = 8) -> list:
    """11h:240-255 (Table 8-107), RO and Conditional.

    Sixteen bytes: media lanes 1-8 for Tx, then the same for Rx. The high
    nibble is the media wavelength and the low nibble the physical fibre, and
    0000b in either means "Mapping unknown or undefined" - which is what a
    module that does not multiplex says, so an absent mapping is an answer
    rather than a gap.

    Page 11h is banked in groups of eight lanes, so a wider module repeats
    those sixteen bytes per bank and `data` is the banks concatenated. Reading
    one bank and describing eight lanes left the mapping of lanes 9 and up
    blank - which the table draws exactly like "unknown or undefined", so a
    WDM module reported no wavelength for its upper lanes rather than the one
    it had named.
    """
    out = []
    for lane in range(lanes):
        bank, within = divmod(lane, 8)
        entry = {}
        for side, off in (('tx', 0), ('rx', 8)):
            i = bank * 16 + off + within
            byte = data[i] if i < len(data) else 0
            wl = (byte >> 4) & 0x0F
            fiber = byte & 0x0F
            entry[side] = {
                'wavelength': wl if 1 <= wl <= 8 else None,
                'fiber': fiber if 1 <= fiber <= 8 else None,
                'fiber_name': _FIBER_NAMES.get(fiber),
                'fiber_short': _FIBER_SHORT.get(fiber),
            }
        entry['known'] = any(entry[s]['wavelength'] or entry[s]['fiber']
                             for s in ('tx', 'rx'))
        out.append(entry)
    return out


def parse_dp_init_pending(byte_val: int, lanes: int = 8) -> list:
    """Table 8-106, 11h:235 - DPInitPendingLane<i>, one bit per host lane.

    Set when a Provision triggered by ApplyDPInit has copied a Staged Control
    Set into the Active Control Set but the transit through DPInit that would
    commit it to hardware has not happened yet. The spec is blunt about what
    that means: "the Active Control Set content may deviate from the actual
    hardware configuration". Reading the Active Control Set as "what the
    module is running" is only true while this bit is clear.
    """
    return [bool((byte_val >> i) & 1) for i in range(lanes)]


def parse_config_status(data: bytes) -> list:
    """Decode 4 bytes of ConfigStatus at Page 11h:202-205 (4 bits/lane)."""
    statuses = []
    for lane in range(8):
        byte_idx = lane // 2
        if byte_idx >= len(data):
            statuses.append("Unknown")
            continue
        nibble = (data[byte_idx] >> ((lane % 2) * 4)) & 0x0F
        statuses.append(config_status_name(nibble))
    return statuses


def parse_lane_flags(byte_val: int) -> list:
    """Bit i (i=0..7) → Lane i+1 flag bool."""
    return [bool((byte_val >> i) & 1) for i in range(8)]


def unpack_appselect(data: bytes) -> list:
    """8 bytes, 1 byte per lane (DPConfigLane, Table 8-102): the AppSelCode
    in bits 7-4 only. bits 3-1 are DPIDX and bit 0 ExplicitControl - see
    unpack_dpconfig.
    """
    return [(data[i] >> 4) & 0x0F if i < len(data) else 0 for i in range(8)]


def unpack_dpconfig(data: bytes) -> list:
    """The whole of each DPConfigLane byte, not just the Application.

    Table 8-102 gives the byte three fields, all RO and Required in the Active
    Control Set:

      7-4 AppSelCode      which Application this lane's Data Path runs
      3-1 DPIDX           "the Data Path Index (DPIDX) of that Data Path:
                          DPID (lowest numbered lane of Data Path)", 0 = lane 1
      0   ExplicitControl 0b: this lane's SI settings are Application
                          dependent; 1b: they are host defined

    unpack_appselect keeps only the first. The Active Control Set is the
    module's own answer, and there DPIDX says which lanes form a Data Path
    without anybody having to work it out from Application widths, and
    ExplicitControl says per lane whether the staged signal integrity values
    are the ones in force.

    "When host lane <i> is unused, the DPIDX field is to be ignored", so an
    unused lane reports None rather than a Data Path index of zero, which
    would read as lane 1.
    """
    out = []
    for i in range(8):
        b = data[i] if i < len(data) else 0
        app = (b >> 4) & 0x0F
        out.append({
            'app_sel': app,
            'dpidx': ((b >> 1) & 0x07) if app else None,
            'explicit_control': bool(b & 0x01),
        })
    return out


def pack_dpconfig(app_sels: list, dpidx: list, explicit: list = None) -> bytes:
    """Eight DPConfigLane bytes (Table 8-102): AppSelCode in bits 7-4,
    DPIDX - the Data Path's lowest lane within its Bank, less one - in 3-1,
    ExplicitControl in bit 0."""
    explicit = list(explicit or [])
    out = bytearray(8)
    for i in range(min(8, len(app_sels))):
        out[i] = (((app_sels[i] & 0x0F) << 4)
                  | ((dpidx[i] & 0x07) << 1 if i < len(dpidx) else 0)
                  | (explicit[i] & 1 if i < len(explicit) else 0))
    return bytes(out)


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
    """Byte 00h:202: bits[7:6]=multiplier (0.1, 1, 10, 100), bits[5:0]=base (0..63).

    The arithmetic only. Two byte values are defined to mean something other
    than their product - see parse_cable_length, which is what the panel uses.
    """
    base = byte_val & 0x3F
    mult_code = (byte_val >> 6) & 0x03
    mult = [0.1, 1.0, 10.0, 100.0][mult_code]
    return base * mult


def parse_cable_length(byte_202: int) -> dict:
    """00h:202 (Table 8-33), and the two values that are not lengths.

    "A CableAssemblyLinkLength value of 1111 1111b indicates a link length
    greater than 6300 m." The product of that byte is exactly 6300, so the
    tool printed "6300 m" - a precise measurement of a cable the module said
    it could not measure. The one byte that means "longer than I can say" is
    the one that looked most like an answer.

    And of BaseLength: "A value of 0 indicates an undefined Link Length, e.g.
    when the physical media can be disconnected from the module." That is any
    byte whose low six bits are zero, not only 00h - a multiplier with no base
    is still undefined, and the product is zero either way, so the panel's
    test for exactly zero happened to catch it but could not say why.

    "Modules with separable optical media shall set the CableAssemblyLinkLength
    value to 0000 0000b", so on a transceiver this field being absent is the
    specified behaviour rather than a gap.
    """
    base = byte_202 & 0x3F
    mult = [0.1, 1.0, 10.0, 100.0][(byte_202 >> 6) & 0x03]
    out = {'code': byte_202, 'metres': None,
           'over_max': byte_202 == 0xFF, 'undefined': False}
    if out['over_max']:
        out['text'] = 'greater than 6300 m'
        return out
    if base == 0:
        out['undefined'] = True
        out['text'] = ('undefined - the media can be disconnected from the '
                       'module')
        return out
    metres = round(base * mult, 1)
    out['metres'] = metres
    out['text'] = '%g m' % metres
    return out


def connector_type_name(code: int) -> str:
    return CONNECTOR_TYPES.get(code, f"Unknown(0x{code:02X})")


def media_if_tech_name(code: int) -> str:
    """Table 8-41 ends at 14h and reserves 15h-FFh.

    Reporting a reserved code as Unknown says the tool failed to recognise it,
    which sends the reader after a newer tool. The tool recognises it
    perfectly well: the standard sets it aside. That is a question about the
    module.
    """
    if code in MEDIA_IF_TECH:
        return MEDIA_IF_TECH[code]
    return "Reserved (0x%02X)" % code


def media_type_name(code: int) -> str:
    """Table 8-20 defines the whole byte, not just the five named types:
    06h-3Fh and 90h-FFh Reserved, and 40h-8Fh Custom.

    Custom is the one that mattered. A module on a vendor-defined media type
    is doing something the standard provides for, and the tool has everything
    it needs to say so - it was answering "Unknown", which reads as a gap in
    the tool and tells the operator nothing about where to look next. The
    Application Descriptors on such a module are read against a vendor ID
    table, and that is worth knowing before trying to interpret them.
    """
    if code in MEDIA_TYPES:
        return MEDIA_TYPES[code]
    if 0x40 <= code <= 0x8F:
        return "Custom (0x%02X)" % code
    return "Reserved (0x%02X)" % code


def module_id_name(mid: int) -> str:
    return MODULE_ID_NAMES.get(mid, f"Unknown (0x{mid:02X})")


def cmis_revision_str(rev: int) -> str:
    """Convert CMIS revision byte to string like '5.3'.
    Upper nibble = major, lower = minor.
    """
    return f"{(rev >> 4) & 0x0F}.{rev & 0x0F}"


def parse_application_descriptors(data: bytes, media_type: int = 0x02,
                                  extra: bytes = b'',
                                  media_assign: bytes = b'',
                                  flat_memory: bool = False) -> list:
    """Parse Application Descriptors from lower memory bytes 86-117 and,
    where the module has them, the additional ones on Page 01h.

    Each descriptor:
      +0: HostInterfaceID (0xFF = unused/end)
      +1: MediaInterfaceID
      +2: bits[7:4]=HostLaneCount, bits[3:0]=MediaLaneCount
      +3: HostLaneAssignmentOptions (bitmap) - paged modules only

    The fourth byte is where the two descriptor formats part company, and the
    difference is not cosmetic. Table 8-22 (Paged Memory Modules) gives it to
    HostLaneAssignmentOptions, a bitmap of the host lanes an Application may
    begin on. Table 8-23 (Flat Memory Modules) gives the same byte to
    HostInterfaceGID, "the Group ID of the table in [5] defining the
    HostInterfaceID".

    Read as a bitmap, a GID of 1 says the Application may begin on host lane
    1, and a GID of 2 says lane 2 - plausible-looking answers to a question
    the module was not asked. So the caller has to say which kind of module
    this is, and a flat one gets no lane-assignment mask at all rather than a
    fabricated one.

    (The specification prints the GID row as bits 7-0 and then reserves bits
    3-0 of the same byte, which cannot both be true. The whole byte is
    reported rather than picking a shift the specification does not settle.)

    6.2.1.6 calls MediaLaneAssignmentOptions "the fifth byte" of the
    descriptor, and notes that its registers "are located on Memory Map Page
    01h ... separated from the first four bytes". Reading only the four left
    every descriptor four fifths told: the host side said where an Application
    may start, and nothing said where the instance lands on the media.

    8.4.17: "Bytes 01h:223-250 provide space for seven additional Application
    Descriptors ... in addition to the eight Application Descriptors in Bytes
    86-177", and AppSelCode is four bits wide. Reading only the first eight
    made Applications 9-15 invisible - absent from the list a host can choose
    from, and indistinguishable from codes the module never advertised.
    """
    data = bytes(data) + bytes(extra)
    apps = []
    for i in range(15):
        off = i * 4
        if off + 4 > len(data):
            break
        host_if = data[off]
        if host_if == 0xFF:
            break
        media_if = data[off + 1]
        lane_count = data[off + 2]
        host_lanes, host_lanes_text = lane_count_field((lane_count >> 4) & 0x0F)
        media_lanes, media_lanes_text = lane_count_field(lane_count & 0x0F)
        if flat_memory:
            host_lane_assign = None
            host_if_gid = data[off + 3]
        else:
            host_lane_assign = data[off + 3]
            host_if_gid = None
        apps.append({
            'app_sel': i + 1,
            'host_if_id': host_if,
            'media_if_id': media_if,
            # The codes alone say nothing without SFF-8024 in hand, which is
            # the whole reason those tables are in this file.
            'host_if_name': host_interface_name(host_if),
            'media_if_name': media_interface_name(media_if, media_type),
            'host_lanes': host_lanes,
            'host_lanes_text': host_lanes_text,
            'media_lanes': media_lanes,
            'media_lanes_text': media_lanes_text,
            'host_lane_assign_mask': host_lane_assign,
            'host_interface_gid': host_if_gid,
            # Not required of a flat-memory module, which has no Page 01h to
            # put it on, so its absence is a shape of module rather than a
            # read that failed.
            'media_lane_assign_mask': (media_assign[i]
                                       if i < len(media_assign) else None),
        })
    return apps


def lane_count_field(nibble: int) -> tuple:
    """Table 8-22 byte 2: the two lane-count nibbles do not only hold counts.

    "0000b: lane count defined by interface ID, or explicit: 0001b: 1 lane
    ... 1000b: 8 lanes. 1001b-1111b: reserved."

    So zero is not zero lanes - it is the module declining to state a width
    that its interface ID already fixes - and a reserved code is not a width
    at all. Both were being passed on as numbers: an Application the module
    describes this way showed as having no lanes, and a reserved encoding
    showed as nine to fifteen of them and was added to the module's
    advertised total.

    Returns the count to do arithmetic with and the text to print. The count
    is zero for both special cases, which keeps them out of a total that is
    supposed to mean lanes; the text says which one it was.
    """
    if 1 <= nibble <= 8:
        return nibble, str(nibble)
    if nibble == 0:
        # Resolving it needs the lane count behind the SFF-8024 interface ID,
        # which is not a table this tool carries.
        return 0, 'per interface ID'
    return 0, 'Reserved (%Xh)' % nibble


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

# Table 8-11 types SoftwareReset (Lower 26.3) WO/SC, and Table 8-3 says a READ
# from a WO/SC element "delivers a zero value, except transiently when reading
# before the module has evaluated and cleared the non-zero bits written". The
# specification puts no bound on that window - "evaluated and cleared" appears
# once in the whole document, in Table 8-3, with no timing beside it - so no
# amount of waiting lets a read-modify-write establish that it is outside it.
# A merge that carries the bit back therefore fires the trigger a second time,
# in the middle of an unrelated change. Keyed by (page, address) because the
# rule is about the byte, not about this one register.
WRITE_ONLY_TRIGGER_BITS = {
    (None, 0x1A): 0x08,   # SoftwareReset (Table 8-11)
}


def drop_write_only_bits(page, address: int, value: int) -> int:
    """Strip the write-only trigger bits from a byte read back for merging."""
    return value & ~WRITE_ONLY_TRIGGER_BITS.get((page, address), 0) & 0xFF


# The access type of each field in byte 0x1A (Table 8-11). A panel that shows
# them all in one status column is claiming they are all readable state;
# Table 8-3 says one of them is not.
MODULE_CONTROL_ACCESS = {
    'bank_broadcast_enable':    'RW',
    'low_pwr_allow_request_hw': 'RW',
    'squelch_method_select':    'RW',
    'low_pwr_request_sw':       'RW',
    'software_reset':           'WO/SC',
}


def update_module_control(current: int, **fields) -> int:
    """Change only the named bits of an already-read Module Control byte.

    Byte 0x1A packs unrelated controls together, so rebuilding it from scratch
    to toggle low power would also clear SquelchMethodSelect and
    BankBroadcastEnable and force AllowLowPwrRequestHW on. Read first, then
    change only what the caller asked for. Bits 2-0 are Custom and are carried
    through untouched.

    The byte read back is not trusted whole: its WO/SC trigger bits are dropped
    before the caller's fields are applied, so only a caller that names the
    trigger can fire it.
    """
    val = drop_write_only_bits(None, 0x1A, current)
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
# Every field this tool surfaces that did not exist in CMIS 5.3, by the key it
# is surfaced under. The Module Info rows take their 5.4 badge from here (via
# /api/module/capabilities), so the claim "new in 5.4" is made in one place.
#
# Checked against the Rev 5.4 lists on pages 11-12 of the specification and
# nothing else. Pages 8-10 are the Rev 5.3 lists: host lane switching (5.3
# E14, 01h:252.7, Page 1Dh), the Page 12h rows of Table 6-21 (5.3 E07), the
# Application hint on Page 02h and the ModuleLowPwr clarification (5.3 M23)
# are 5.3's, and host lane switching was listed here - with a 5.4 badge on
# its row and card - until round 102. Two keys named registers no reply
# carried: the polarity is surfaced as default_polarity, and the
# AbnormalFwIndicationMask under firmware_flag_masks.
NEW_IN_5_4 = frozenset({
    'heatsink_type',
    'abnormal_fw_flag',
    'default_polarity',
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
# Table 8-115 Pattern IDs. ID 13 is Reserved and is deliberately absent: the
# dropdown falls back to this table when a module advertises no pattern at
# all, and an encoding the standard reserves is not something to offer as a
# choice. 14 (Custom) and 15 (User Pattern) are real selections and are
# listed.
#
# The table's full caption used to appear only on a second copy of this list
# that nothing read, so deleting the copy would have taken the reference with
# it.
PATTERN_NAMES = {
    0: 'PRBS31Q', 1: 'PRBS31', 2: 'PRBS23Q', 3: 'PRBS23', 4: 'PRBS15Q',
    5: 'PRBS15', 6: 'PRBS13Q', 7: 'PRBS13', 8: 'PRBS9Q', 9: 'PRBS9',
    10: 'PRBS7Q', 11: 'PRBS7', 12: 'SSPRQ', 14: 'Custom', 15: 'User Pattern',
}


SQUELCH_METHOD_TX = {
    0: 'Not supported',
    1: 'Reduces OMA',
    2: 'Reduces Pav',
    3: 'Host selects OMA or Pav',
}


# Table 8-45. The SMF field is in km with a two-bit multiplier that can escape
# to a second multiplier in 01h:137; the multimode fields are plain byte counts
# of 2 m, except OM2 which counts single metres.
_SMF_MULT = (0.1, 1.0, 10.0)
_SMF_MULT2 = (50.0, 100.0, 200.0, 500.0)


# Lower 02h. The I2C column of MciMaxSpeed; a module on SPI reads the same
# field against a different scale, and this tool only ever speaks I2C.
_MCI_I2C = {0: '400 kHz', 1: '1 MHz', 2: '3.4 MHz'}


def parse_config_capabilities(raw: int) -> dict:
    """Lower 02h: memory model plus which reconfiguration procedures work.

    ApplyImmediate (10h:144) commits a staged configuration without taking the
    Data Path down, and a module that does not support it "ignores any WRITE"
    - silently, so offering the trigger unconditionally would give the host a
    button that does nothing on most modules.
    """
    stepped = bool((raw >> 6) & 1)
    auto = raw & 0x03
    if stepped and auto == 0b11:
        # "11: reserved". 8.1.3.8: unknown - not "neither", which is 00b and
        # is what this used to report, refusing ApplyImmediate on a claim
        # the module never made.
        hot = regular = None
    elif stepped:
        hot = auto == 0b10
        regular = auto == 0b01
    else:
        # "xx: both regular and hot supported (legacy default)"
        hot = regular = True
    speed_code = (raw >> 2) & 0x0F
    return {
        'memory_model': 'Flat' if (raw >> 7) & 1 else 'Paged',
        'stepped_config_only': stepped,
        'auto_commissioning': auto,
        'hot_reconfig': hot,
        'regular_reconfig': regular,
        'mci_max_speed_code': speed_code,
        'mci_max_speed_i2c': _MCI_I2C.get(speed_code),
    }


def parse_link_lengths(data: bytes) -> list:
    """01h:132-137 -> the fibre types this module reaches, longest first.

    "Unsupported media types shall be populated with zeroes", and an active
    optical cable zeroes the whole table and reports its real length in
    00h:202 instead - so an empty list is a statement, not a failure.
    """
    out = []
    smf = data[0] if len(data) > 0 else 0
    base = smf & 0x3F
    if base:
        code = (smf >> 6) & 0x03
        if code == 0x03:
            b137 = data[5] if len(data) > 5 else 0
            mult = _SMF_MULT2[(b137 >> 6) & 0x03]
        else:
            mult = _SMF_MULT[code]
        out.append({'media': 'SMF', 'km': round(base * mult, 1)})
    for i, (name, step) in enumerate((('OM5', 2), ('OM4', 2),
                                      ('OM3', 2), ('OM2', 1)), start=1):
        raw = data[i] if len(data) > i else 0
        if raw:
            out.append({'media': name, 'm': raw * step})
    return out


def parse_supported_controls(data: bytes) -> dict:
    """01h:155-156 (Table 8-51).

    Every one of these gates a control the UI puts on screen. A module that
    says 00b for SquelchMethodTx has no Tx output squelching at all, which
    also means no automatic Tx squelching to disable.
    """
    b155 = data[0] if len(data) > 0 else 0
    b156 = data[1] if len(data) > 1 else 0
    method = (b155 >> 4) & 0x03
    return {
        'wavelength_controllable':   bool(b155 & 0x80),
        'transmitter_tunable':       bool(b155 & 0x40),
        'squelch_method_tx':         method,
        'squelch_method_tx_name':    SQUELCH_METHOD_TX[method],
        'tx_squelch_supported':      method != 0,
        'forced_squelch_tx':         bool(b155 & 0x08),
        'auto_squelch_disable_tx':   bool(b155 & 0x04),
        'output_disable_tx':         bool(b155 & 0x02),
        'input_polarity_flip_tx':    bool(b155 & 0x01),
        'bank_broadcast':            bool(b156 & 0x80),
        'auto_squelch_disable_rx':   bool(b156 & 0x04),
        'output_disable_rx':         bool(b156 & 0x02),
        'output_polarity_flip_rx':   bool(b156 & 0x01),
    }


AUX_OBSERVABLE_NAMES = {
    'custom':            ('Custom', '\u81ea\u5b9a\u4e49'),
    'tec_current':       ('TEC Current', 'TEC \u7535\u6d41'),
    'laser_temperature': ('Laser Temperature', '\u6fc0\u5149\u5668\u6e29\u5ea6'),
    'vcc2':              ('Additional Supply Voltage', '\u9644\u52a0\u7535\u6e90\u7535\u538b'),
}


RX_OUTPUT_EQ_TYPES = {
    0: 'Peak-to-peak amplitude constant, or not implemented',
    1: 'Steady-state amplitude constant',
    2: 'Average of peak-to-peak and steady-state amplitude constant',
    3: 'Reserved',
}


RX_OUTPUT_EQ_CONTROL = {
    0: 'Not supported', 1: 'Pre-cursor only',
    2: 'Post-cursor only', 3: 'Pre- and post-cursor',
}


def unpack_nibbles(data: bytes, lanes: int = 8) -> list:
    """One 4-bit value per lane, lane 1 in the low nibble of the first byte.

    The same packing as DPConfigLane, and the same trap: reading it high
    nibble first silently swaps every pair of lanes.
    """
    out = []
    for i in range(lanes):
        byte = data[i // 2] if i // 2 < len(data) else 0
        out.append((byte >> 4) & 0x0F if i % 2 else byte & 0x0F)
    return out


def unpack_pairs(data: bytes, lanes: int = 8) -> list:
    """One 2-bit value per lane, lane 1 in the low pair of the first byte.

    Four lanes to a byte, packed low-first like unpack_nibbles - so the
    same trap applies, reading the high pair first reverses each group
    of four lanes rather than swapping pairs.
    """
    out = []
    for i in range(lanes):
        byte = data[i // 4] if i // 4 < len(data) else 0
        out.append((byte >> (2 * (i % 4))) & 0x03)
    return out


# 10h:154-155 and 11h:215-216. Value 3 is reserved in both, so a module
# reporting it is saying something the specification does not define.
EQ_RECALL_NAMES = {
    0: 'no recall',
    1: 'buffer 1',
    2: 'buffer 2',
    3: 'reserved',
}


def recall_buffer_violations(values, buffers: int) -> list:
    """Lanes whose recall code names a buffer the module did not advertise.

    Tables 8-83, 8-88 and 8-104 encode these two bits identically - 00b do
    not recall, 01b buffer 1, 10b buffer 2, 11b reserved - and all three cite
    the same advertisement, 01h:161.6-5, which Table 8-54 defines as a
    *count*: 01b is one buffer, 10b is two. So the value is an index and the
    advertisement is a count, and a module with one buffer has no buffer 2.

    11b in a lane is reserved rather than out of range: it names no buffer at
    all, and the panel reports it as its own kind of wrong. 11b in the
    advertisement is reserved too - there is then no count to judge against,
    and it needs no case of its own here because no two-bit value can exceed
    it. Neither does "do not recall" need one: 0 is over no count.
    """
    if not buffers:
        return []
    return [i + 1 for i, v in enumerate(values) if v < 3 and v > buffers]


def parse_si_controls_adv(data: bytes) -> dict:
    """01h:161-162 (Table 8-54): which signal integrity controls exist."""
    b161 = data[0] if len(data) > 0 else 0
    b162 = data[1] if len(data) > 1 else 0
    rx_eq = (b162 >> 3) & 0x03
    return {
        'tx_input_eq_recall_buffers': (b161 >> 5) & 0x03,
        'tx_input_eq_freeze':      bool(b161 & 0x10),
        'tx_adaptive_input_eq':    bool(b161 & 0x08),
        'tx_input_eq_host_control': bool(b161 & 0x04),
        'tx_cdr_bypass_control':   bool(b161 & 0x02),
        'tx_cdr':                  bool(b161 & 0x01),
        'versatile_control_set':   bool(b162 & 0x80),
        'unidir_reconfig':         bool(b162 & 0x40),
        'staged_set_1':            bool(b162 & 0x20),
        'rx_output_eq_control':    rx_eq,
        'rx_output_eq_control_name': RX_OUTPUT_EQ_CONTROL[rx_eq],
        'rx_output_amplitude_control': bool(b162 & 0x04),
        'rx_cdr_bypass_control':   bool(b162 & 0x02),
        # 162.0 RxCDRSupported was the one bit of this byte not decoded,
        # while its Tx twin at 161.0 was. The asymmetry mattered: see
        # cdr_host_controllable.
        'rx_cdr':                  bool(b162 & 0x01),
    }


def parse_cdr_power_saved(byte_152: int, lanes: int, tx: bool,
                          rx: bool) -> dict:
    """01h:152 (Table 8-50), what bypassing a CDR actually buys.

    "U8 Minimum power consumption saved per CDR per lane when placed in CDR
    bypass, in multiples of 0.01 W rounded up to the next whole multiple of
    0.01 W."

    It is the number behind the decision the CDR columns present. Minimum, so
    the module is promising at least this much, and per CDR per lane, so a
    module with both CDRs bypassable on eight lanes is offering sixteen times
    it.

    Zero is not a saving of nothing. Unlike its neighbours at 148-150, this
    row does not define zero as "not specified" - but a real saving rounds
    *up* to the next whole 0.01 W, so it can never round to zero. A module
    reporting zero has declined to state a figure rather than measured one.
    Reasoned from this field's own rounding rule, not carried over from the
    rows above it.
    """
    sides = (1 if tx else 0) + (1 if rx else 0)
    return {
        'raw': byte_152,
        'stated': byte_152 > 0,
        'per_cdr_w': byte_152 / 100.0 if byte_152 else None,
        'sides': sides,
        'lanes': lanes,
        # What the module would save with every bypassable CDR bypassed. The
        # per-lane figure alone is not the number a power budget is decided
        # on, and multiplying it is arithmetic over two things the panel
        # already knows.
        'all_bypassed_w': (byte_152 / 100.0 * lanes * sides
                           if byte_152 and sides else None),
    }


def cdr_host_controllable(adv: dict, side: str) -> bool:
    """Whether CDREnable<side> is a control the host actually has.

    Both Staged Control Set bytes name a two-bit advertisement rather than
    one bit: 10h:160 CDREnableTx says "Advertisement: 01h:161.0-1", and
    10h:161 CDREnableRx says "Advertisement: 01h:162.0-1".

    Table 8-54 splits each of those pairs into a CDR and a bypass control,
    and writes the second one conditionally:
    "0b: If a Tx CDR is supported, it cannot be bypassed".

    So a bypass-control bit on its own is not a control. A module with no CDR
    may set it, because the sentence is about a CDR that may not exist, and
    the panel was gating the Rx column on that bit alone - offering a switch
    for a retimer the module had just said it does not have. mock_sr8
    advertises exactly that pair.
    """
    return bool(adv.get(side + '_cdr') and adv.get(side + '_cdr_bypass_control'))


def parse_si_maxima(data: bytes) -> dict:
    """01h:153-154: the largest value each signal integrity target accepts,
    plus which Rx output amplitude codes exist."""
    b153 = data[0] if len(data) > 0 else 0
    b154 = data[1] if len(data) > 1 else 0
    return {
        'rx_output_levels': [i for i in range(4) if (b153 >> (4 + i)) & 1],
        'tx_input_eq_max':  b153 & 0x0F,
        'rx_output_eq_post_cursor_max': (b154 >> 4) & 0x0F,
        'rx_output_eq_pre_cursor_max':  b154 & 0x0F,
    }


def parse_rx_tx_characteristics(byte_val: int) -> dict:
    """01h:151 (Table 8-50).

    Two of these change what the interface means rather than just adding a
    label. 151.4 decides whether the Rx power monitor reports OMA or average
    power - different quantities, several dB apart on a modulated signal, and
    a receiver limit is stated for one or the other. 151.0 says the Tx output
    disable is module-wide, so a per-lane row of checkboxes is not per lane at
    all: clearing one takes every Tx lane down with it.
    """
    eq = (byte_val >> 5) & 0x03
    return {
        'apd_detector':          bool(byte_val & 0x80),
        'detector_type':         'APD' if byte_val & 0x80 else 'PIN',
        'rx_output_eq_type':     eq,
        'rx_output_eq_name':     RX_OUTPUT_EQ_TYPES[eq],
        'rx_power_is_average':   bool(byte_val & 0x10),
        'rx_power_type':         'Average power' if byte_val & 0x10 else 'OMA',
        'rx_los_on_average':     bool(byte_val & 0x08),
        'rx_los_type':           'Pav' if byte_val & 0x08 else 'OMA',
        'rx_los_is_fast':        bool(byte_val & 0x04),
        'tx_disable_is_fast':    bool(byte_val & 0x02),
        'tx_disable_module_wide': bool(byte_val & 0x01),
    }


# 01h:145.6-5, all four codes defined and all RO/Required. The grouping
# "applies to each group of 8 lanes (each Bank)" on a wider module.
TX_INPUT_CLOCKING = {
    0: 'lanes 1-8 synchronous',
    1: 'lanes 1-4 and 5-8 synchronous',
    2: 'lanes 1-2, 3-4, 5-6, 7-8 synchronous',
    3: 'lanes may be asynchronous',
}


# Table 8-39, the whole of it: one letter per near end host lane, naming the
# far end module that lane is cabled to. The letter is "depending on the
# lowest lane number in the group", so a group starting on lane 3 is 'c' -
# which is what makes this transcription checkable rather than trusted, and
# there is a test that checks it.
#
# "For modules with more than 8 lanes, the topology defined for 8 lanes
# repeats in each group of 8 lanes."
FAR_END_LANE_GROUPS = {
    1:  'abcdefgh', 2:  'aaaaaaaa', 3:  'aaaaeeee', 4:  'abcdeeee',
    5:  'abcceeee', 6:  'aacdeeee', 7:  'aacceeee', 8:  'aaaaefgh',
    9:  'aaaaefgg', 10: 'aaaaeegh', 11: 'aaaaeegg', 12: 'aacceegg',
    13: 'abcceegg', 14: 'aacdeegg', 15: 'abcdeegg', 16: 'aaccefgg',
    17: 'abccefgg', 18: 'aacdefgg', 19: 'abcdefgg', 20: 'aacceegh',
    21: 'abcceegh', 22: 'aacdeegh', 23: 'abcdeegh', 24: 'aaccefgh',
    25: 'abccefgh', 26: 'aacdefgh',
}

# Table 8-38 marks five of those codes as a uniform breakout, which is the
# shape a cable is usually ordered by.
FAR_END_UNIFORM = {1: '1-lane', 12: '2-lane', 3: '4-lane', 2: '8-lane',
                   27: '16-lane'}


# Table 8-35. The note beneath it: "when the module advertises itself as a
# PCIe application, the cable attenuation fields above are reported for
# frequencies 2.5, 4.0, 8.0, 16.0, 32.0 GHz" instead - the same five bytes,
# a different set of frequencies.
CU_ATTENUATION_GHZ = (5.0, 7.0, 12.9, 25.8, 53.125)
CU_ATTENUATION_GHZ_PCIE = (2.5, 4.0, 8.0, 16.0, 32.0)


NAD_SIZE = 8
NADS_PER_BANK = 15


def interface_uid(gid: int, code: int) -> str:
    """An Interface UID printed the way the specification thinks of it.

    6.2.1.6.2: Normalized Application Descriptors exist "to support
    interfaces identified by a 12-bit Interface Unique ID (UID)". The UID is
    a 4-bit GID and an 8-bit ID, and the SFF-8024 tables this tool resolves
    names from are the GID 0 namespace only.
    """
    return '0x%02X' % code if not gid else '%d:0x%02X' % (gid, code)


# Table 8-55 byte 163.3-0. The codes are not a page count: 0-4 happen to
# equal one, and then 5, 6 and 7 mean 8, 12 and 16 EPL pages.
CDB_EPL_PAGES = {
    0: (0, 'none',    0),
    1: (1, 'A0h',     128),
    2: (2, 'A0h-A1h', 256),
    3: (3, 'A0h-A2h', 384),
    4: (4, 'A0h-A3h', 512),
    5: (8, 'A0h-A7h', 1024),
    6: (12, 'A0h-ABh', 1536),
    7: (16, 'A0h-AFh', 2048),
}


def parse_cdb_advertisement(data: bytes) -> dict:
    """01h:163-166 (Table 8-55), how the module does CDB messaging.

    163.7-6 gates the rest: every other field in this table is RO Cnd., and
    zero here is "CDB functionality not supported" rather than one instance.

    Two things a host gets operationally wrong without this byte:

    163.5 CdbBackgroundModeSupported. Clear means the module "will hold off
    ACCESS to any register until CDB command processing is completed" - the
    whole management interface stops answering for the duration, not just the
    CDB pages. A tool that keeps polling monitors through a firmware update on
    such a module is not being slow, it is being ignored.

    And the maximum CDB busy time, which is how long a host must be prepared
    to wait before deciding a module has stopped. See cdb_max_busy_ms.
    """
    instances = (data[0] >> 6) & 0x03
    if instances == 0:
        return {'supported': False, 'instances': 0}
    epl_code = data[0] & 0x0F
    pages, page_range, epl_bytes = CDB_EPL_PAGES[epl_code]
    k = data[1]
    busy = cdb_max_busy_ms(data[2], data[3])
    return {
        'supported': True,
        'instances': instances,
        # "3: Reserved" - a count the standard set aside, not two instances
        # and not a code this tool failed to recognise.
        'instances_text': ('Reserved (3)' if instances == 3
                           else '%d' % instances),
        'background_mode': bool(data[0] & 0x20),
        'auto_paging': bool(data[0] & 0x10),
        'epl_code': epl_code,
        'epl_pages': pages,
        'epl_page_range': page_range,
        'epl_bytes': epl_bytes,
        # One byte, two limits. k extends an access in units of 8 bytes, but
        # Page 9Fh holds 120 bytes of LPL and the EPL pages hold 2048, so the
        # same k caps at 128 on one and 2048 on the other. Reporting a single
        # number would promise a 2048-byte write to a page that takes 128.
        'rw_extension': k,
        'max_access_epl': 8 * (1 + k),
        'max_access_lpl': min(8 * (1 + k), 128),
        'trigger_on_stop': bool(data[2] & 0x80),
        **busy,
    }


def cdb_max_busy_ms(byte_165: int, byte_166: int) -> dict:
    """The maximum CDB busy time TCDBB, and why it comes with a caveat.

    Table 8-55 gives two encodings and a bit to choose between them, and the
    three rows do not agree on which value of the bit chooses which:

      166.7  CdbMaxBusySpecMethod - "0: ... specified via CdbMaxBusyTime
             (01h:166.6-0)", "1: ... specified via CdbExtMaxBusyTime
             (01h:165.4-0)"
      165.4-0 CdbExtMaxBusyTime - "... in a range of 160 ms to 4960 ms when
             CdbMaxBusySpecMethod=0b"
      166.6-0 CdbMaxBusyTime - "... in a range of 0 ms to 80 ms when
             CdbMaxBusySpecMethod=1b"

    The selector row and the two value rows are exactly inverted, and nothing
    else in OIF-CMIS-05.4 settles it. The two readings are 0-80 ms and
    160-4960 ms, so choosing silently is the difference between waiting and
    calling a working module dead.

    This follows 166.7, the row that defines what the bit means, and reports
    the other reading beside it rather than presenting one number as settled.
    """
    ext = max(1, byte_165 & 0x1F) * 160
    plain = max(0, 80 - (byte_166 & 0x7F))
    method = (byte_166 >> 7) & 0x01
    return {
        'busy_method': method,
        'max_busy_ms': ext if method else plain,
        'max_busy_field': 'CdbExtMaxBusyTime' if method else 'CdbMaxBusyTime',
        'max_busy_ms_alt': plain if method else ext,
        'max_busy_field_alt': ('CdbMaxBusyTime' if method
                               else 'CdbExtMaxBusyTime'),
    }


def parse_nad(raw: bytes, app_number: int = 0, media_type: int = 0x02) -> dict:
    """One Normalized Application Descriptor, Table 8-173.

    Eight contiguous bytes, which is the point of them: the basic descriptor
    for the same Application is scattered across Lower Memory and Page 01h.

    Byte 6 is the half a basic descriptor cannot carry - the GIDs of the two
    Interface UIDs. When either is non-zero the Application's identity does
    not fit in eight bits, and 6.2.1.6.2 says the module puts BEh in the
    basic descriptor instead. The tool has been printing "See NAD (0xBE)"
    there and had nothing to send the reader to.

    Byte 5 bit 7 says this descriptor may not be an Application in the
    ordinary sense at all: a Network Path is a different thing from a Data
    Path (8.19.5.3) and listing one as the other would offer the reader a
    provisioning move that does not exist.
    """
    host_gid = (raw[6] >> 4) & 0x0F
    media_gid = raw[6] & 0x0F
    host_n, host_txt = lane_count_field((raw[2] >> 4) & 0x0F)
    media_n, media_txt = lane_count_field(raw[2] & 0x0F)
    return {
        'app_number':   app_number,
        'host_if_id':   raw[0],
        'media_if_id':  raw[1],
        'host_gid':     host_gid,
        'media_gid':    media_gid,
        'host_uid':     interface_uid(host_gid, raw[0]),
        'media_uid':    interface_uid(media_gid, raw[1]),
        # A name only where there is a table to read it from. A non-zero GID
        # is a different registry, not a code this tool failed to recognise.
        'host_if_name': (host_interface_name(raw[0]) if not host_gid
                         else 'GID %d - not an SFF-8024 interface ID'
                              % host_gid),
        'media_if_name': (media_interface_name(raw[1], media_type)
                          if not media_gid
                          else 'GID %d - not an SFF-8024 interface ID'
                               % media_gid),
        'host_lanes':        host_n,
        'host_lanes_text':   host_txt,
        'media_lanes':       media_n,
        'media_lanes_text':  media_txt,
        'host_lane_assign_mask':  raw[3],
        'media_lane_assign_mask': raw[4],
        'network_path': bool(raw[5] & 0x80),
    }


def parse_nad_block(raw: bytes, bank: int, media_type: int = 0x02) -> list:
    """One Bank of Page 1Ch, Table 8-174.

    "NAD<i>, with AppSel code = <i> = 1..15, accessed on Bank Index <j> ... is
    the Normalized Application Descriptor for Application Number
    AN = 15*<j> + <i>", and selecting it needs both halves: AppSelCode = i
    and NADBlockIndex = j. A list numbered 1..60 with no pair beside it
    cannot be acted on.

    An unused descriptor carries HostInterfaceID FFh, the same terminator the
    basic list uses (8.2.13), and is dropped rather than listed as an
    Application with no interface.
    """
    out = []
    for i in range(NADS_PER_BANK):
        chunk = raw[i * NAD_SIZE:(i + 1) * NAD_SIZE]
        if len(chunk) < NAD_SIZE or chunk[0] == 0xFF:
            continue
        nad = parse_nad(chunk, NADS_PER_BANK * bank + i + 1, media_type)
        nad['app_sel'] = i + 1
        nad['nad_block_index'] = bank
        out.append(nad)
    return out


def nad_mirror_mismatches(bank0: list, basic: list) -> list:
    """Where Bank 0 and the basic Application Descriptors disagree.

    6.2.1.6.2 makes this a requirement, not a convention: "the content of the
    first 15 Normalized Application Descriptor (NAD) instances must be
    mirrored into the basic Application Advertisement registers".

    With one substitution built into the rule, which is the whole reason this
    cannot be a plain equality check: "if a NAD cannot be represented
    correctly in a basic Application Descriptor because of a UID being
    greater than 255, the module will change the offending interface ID in
    the relevant basic Application Descriptor to the special value ... BEh".
    So a basic descriptor reading BEh opposite a NAD with a non-zero GID is
    the module doing exactly what it was told, and flagging it would report
    every correctly built module as broken.
    """
    out = []
    by_sel = {b.get('app_sel'): b for b in basic}
    for nad in bank0:
        b = by_sel.get(nad['app_sel'])
        if b is None:
            out.append({'app_sel': nad['app_sel'], 'field': 'descriptor',
                        'nad': nad['host_uid'], 'basic': 'absent'})
            continue
        for side in ('host', 'media'):
            gid = nad[side + '_gid']
            want = NAD_INTERFACE_ID if gid else nad[side + '_if_id']
            got = b.get(side + '_if_id')
            if got != want:
                out.append({'app_sel': nad['app_sel'],
                            'field': side + ' interface ID',
                            'nad': ('0x%02X (GID %d, so BEh is required here)'
                                    % (nad[side + '_if_id'], gid)) if gid
                                   else '0x%02X' % want,
                            'basic': '0x%02X' % got if got is not None
                                     else 'absent'})
        for side in ('host', 'media'):
            if b.get(side + '_lanes') != nad[side + '_lanes']:
                out.append({'app_sel': nad['app_sel'],
                            'field': side + ' lane count',
                            'nad': nad[side + '_lanes_text'],
                            'basic': str(b.get(side + '_lanes'))})
    return out


def parse_dp_latency(raw: bytes) -> list:
    """Table 8-141: eight U16 latencies in nanoseconds, by host lane.

    "Data Path Rx Latency and Data Path Tx Latency convey the total delay thru
    the module, in nanoseconds, and are reported by host lane."

    Plain unsigned, and deliberately no escape value. Table 8-35 two pages
    earlier defines 0 as "this characteristic is not available" and this table
    defines nothing of the sort, so a zero here is zero nanoseconds as far as
    the specification is concerned. Reading the neighbouring table's rule into
    this one would hide a real answer behind "unknown"; the honest caveat is a
    different one and belongs beside it - 8.18 says the accuracy "is not
    specified in this version of CMIS", and that for modules updating these
    dynamically "it is currently undefined as to when the values in these
    registers are guaranteed to be valid".
    """
    return [struct.unpack_from('>H', raw, i * 2)[0]
            for i in range(min(8, len(raw) // 2))]


def latency_disagreements(groups: list, latency: dict) -> list:
    """Data Paths whose lanes do not all report the same latency.

    8.18: "For Data Paths with multiple lanes, all lanes shall report the same
    latency." A module that reports different numbers across one Data Path has
    contradicted itself, and showing the column without checking leaves the
    reader to notice - on a sixteen lane module, across two banks, in a table
    of thirty-two numbers.

    The rule is about Data Paths "with multiple lanes", and no guard is
    needed for that: one lane yields one value, and one value is never more
    than one distinct value.

    groups is lane numbers, one list per Data Path, as the datapath payload
    reports them. Returns one entry per disagreeing Data Path.
    """
    out = []
    for g in groups:
        for kind in ('rx', 'tx'):
            vals = [latency[kind][n - 1] for n in g
                    if 0 < n <= len(latency.get(kind, []))]
            if len(set(vals)) > 1:
                out.append({'lanes': list(g), 'kind': kind,
                            'values': vals})
    return out


def is_copper_media(code) -> bool:
    """Media types 03h and 04h (Table 8-20) are cable assemblies.

    8.3.6 gates the cable attenuation block on being a copper cable; 05h
    BASE-T is copper but is not a cable assembly with a loss figure at
    53 GHz, and 01h/02h are fibre. 04h "Active Cable assembly" covers active
    optical as well as active copper - an active optical cable is left to
    answer the block with zeros, which the specification defines as "not
    available (not relevant or otherwise unknown)".
    """
    return code in (0x03, 0x04)


def parse_cu_attenuation(data: bytes) -> list:
    """00h:204-208 (Table 8-35): cable attenuation in whole dB.

    "A value of 0 dB indicates that this characteristic is not available (not
    relevant or otherwise unknown)", so a zero is an absent figure rather than
    a cable with no loss - which at 53 GHz would be a remarkable cable.

    Byte 209 is Reserved and is not one of these; the register was declared
    six bytes long, which would have put it on the panel as a sixth
    attenuation at no stated frequency.

    "For active linear copper cables with host-programmable gain, the
    characteristics are reported for the 0 dB gain setting."
    """
    out = []
    for i, ghz in enumerate(CU_ATTENUATION_GHZ):
        db = data[i] if i < len(data) else 0
        out.append({'ghz': ghz, 'db': db or None})
    return out


def parse_firmware_revision(major: int, minor: int) -> dict:
    """8.2.9 defines two combinations that are not version numbers.

      Major = 0 and Minor = 0      the module does not have any firmware
      Major = FFh and Minor = FFh  the active firmware load is invalid
      anything else                the firmware version

    Both were printed as "major.minor", so a module reporting an invalid
    firmware load showed 255.255 - a fault condition dressed as a plausible
    version, and the one value a reader would not question.

    Table 8-44 gives the inactive firmware fields "the same encoding", and
    adds that "a module without inactive firmware clears these fields" - so
    0.0 there is the ordinary case rather than a module with no firmware at
    all. The caller says which field it is reading; this says what the numbers
    mean.
    """
    invalid = major == 0xFF and minor == 0xFF
    absent = major == 0 and minor == 0
    return {
        'major': major,
        'minor': minor,
        'invalid': invalid,
        'absent': absent,
        'version': None if (invalid or absent) else '%d.%d' % (major, minor),
    }


def parse_far_end_config(byte_211: int) -> dict:
    """00h:211.4-0 (Table 8-37): how a cable assembly's far end breaks out.

    The byte was declared in this file and never read, sitting between 00h:210
    and 00h:212, both of which are read. On a breakout cable it is the only
    place that says which host lanes go to which far end module - the
    Application descriptors say how the module is configured, not what it is
    plugged into.

    Code 0 is "Undefined. Module with detachable media", and Table 8-37 says
    the byte "is cleared" for such a module, so zero is not a missing answer
    on an optical module - it is the right one.
    """
    code = byte_211 & 0x1F
    out = {'code': code, 'reserved': 28 <= code <= 30, 'custom': code == 31,
           'groups': None, 'uniform': FAR_END_UNIFORM.get(code)}
    if code == 0:
        out['summary'] = 'Undefined - module with detachable media'
    elif code == 27:
        out['summary'] = 'Far end breakout with 16-lane connector(s)'
    elif 28 <= code <= 30:
        out['summary'] = 'Reserved (%d)' % code
    elif code == 31:
        out['summary'] = 'Custom'
    else:
        letters = FAR_END_LANE_GROUPS[code]
        groups = []
        for i, ch in enumerate(letters):
            if not groups or letters[i - 1] != ch:
                groups.append([i + 1])
            else:
                groups[-1].append(i + 1)
        out['groups'] = groups
        out['summary'] = '%d far end module%s: %s' % (
            len(groups), '' if len(groups) == 1 else 's',
            ', '.join(_lane_run(g) for g in groups))
    return out


def _lane_run(lanes: list) -> str:
    """"3-4" rather than "3, 4"; a single lane stays a number."""
    return str(lanes[0]) if len(lanes) == 1 else '%d-%d' % (lanes[0], lanes[-1])


def parse_aux_observables(byte_val: int) -> dict:
    """01h:145 (Table 8-50), RO and Required: seven fields, not three.

    What each Aux monitor measures is the part this was written for - the
    value registers are plain S16, and without the advertisement the number
    has no unit: Aux2 is degrees Celsius or a percentage of the maximum TEC
    current depending on one bit.

    The rest of the byte was being dropped, and none of it is Reserved:

      7    CoolingImplemented          parsed, and then never shown
      6-5  TxInputClockingCapabilities which Tx input lanes must be frequency
                                       synchronous, in groups - a constraint
                                       on how a host may lay Data Paths out
      4    ePPSSupported               the Enhanced Pulse Per Second signal
      3    TimingPage15hSupported      whether Page 15h exists at all

    The byte is already read at connect, so none of this costs a transaction.
    """
    clocking = (byte_val >> 5) & 0x03
    return {
        'cooled_transmitter': bool(byte_val & 0x80),
        'tx_input_clocking_code': clocking,
        'tx_input_clocking': TX_INPUT_CLOCKING[clocking],
        'epps_supported': bool(byte_val & 0x10),
        'timing_page_15h': bool(byte_val & 0x08),
        'aux1': 'tec_current'       if byte_val & 0x01 else 'custom',
        'aux2': 'tec_current'       if byte_val & 0x02 else 'laser_temperature',
        'aux3': 'vcc2'              if byte_val & 0x04 else 'laser_temperature',
    }


def parse_aux_value(raw: bytes, observable: str):
    """One Aux monitor as (value, unit), decoded for what it actually is.

    TEC current is a signed percentage of the module's maximum TEC current
    magnitude, not an absolute current: +100 % is full heating, -100 % full
    cooling. There is no register giving that maximum, so no ampere figure can
    honestly be derived from it (Table 8-10, Lower Memory 18-23).
    """
    v = struct.unpack('>h', raw[:2])[0]
    if observable == 'laser_temperature':
        return round(v / 256.0, 4), 'degC'
    if observable == 'tec_current':
        return round(v * 100.0 / 32767.0, 3), '%'
    if observable == 'vcc2':
        return round(v * 0.0001, 4), 'V'
    return v, ''            # custom: the vendor defines it, so claim no unit


def parse_aux_thresholds(data: bytes, observables: dict) -> dict:
    """02h:144-175 (Table 8-64), RO and Conditional.

    Eight bytes per monitor - high alarm, low alarm, high warning, low
    warning - for Aux1, Aux2, Aux3 and then the Custom monitor. Each is the
    same raw S16 as the monitor it belongs to, so it has to be decoded as
    whatever 01h:145 says that monitor observes: reading a TEC current
    threshold as a temperature gives a number in the right range and the
    wrong units.
    """
    out = {}
    for idx, off in ((1, 0), (2, 8), (3, 16)):
        key = 'aux%d' % idx
        observable = observables.get(key, 'custom')
        levels = {'index': idx, 'observable': observable,
                  'name': AUX_OBSERVABLE_NAMES[observable][0]}
        for name, k in (('high_alarm', 0), ('low_alarm', 2),
                        ('high_warn', 4), ('low_warn', 6)):
            raw = data[off + k:off + k + 2]
            if len(raw) < 2:
                continue
            value, unit = parse_aux_value(raw, observable)
            levels[name] = value
            levels['unit'] = unit
        out[key] = levels
    return out


# Table 8-9, Lower Memory 9-11. The six module-level monitors carry their
# threshold Flags in three bytes that share one layout: bits 3-0 are the
# high alarm, low alarm, high warning and low warning of one monitor and
# bits 7-4 the same four of the next. Only byte 9 used to be decoded, so
# the Aux and Custom monitor Flags were read - which is what clears them -
# and dropped.
MODULE_MONITOR_FLAG_BYTES = (
    (0x09, 'temp', 'vcc'),
    (0x0A, 'aux1', 'aux2'),
    (0x0B, 'aux3', 'custom'),
)

MONITOR_FLAG_LEVELS = ('high_alarm', 'low_alarm', 'high_warn', 'low_warn')


# Which Mask byte belongs to which Flag byte.
#
# The specification defines Interrupt in one sentence: it "is asserted as
# long as any Flag is set with its associated Mask cleared". So the state
# of the Interrupt line is not a separate thing to read - it is this
# pairing applied to the Flag bytes, and a Flag that is set while Interrupt
# stays deasserted is a Flag whose Mask is set.
#
# (flag page, first flag byte, mask page, first mask byte, count)
FLAG_MASK_BLOCKS = (
    (None, 0x08, None, 0x1F, 6),    # Lower 8-13 <- Lower 31-36 (Table 8-12)
    (0x11, 0x86, 0x10, 0xD5, 20),   # 11h:134-153 <- 10h:213-232
    # Eight, not the eighteen both overview tables imply. Table 8-138
    # defines the Flags at 132-139 and stops; Table 8-133 defines the Masks
    # at 206-213 and marks 214-223 Reserved[10]. Table 8-135's own row says
    # "132-139" in the byte column and "18" in the size column, and the row
    # under it marks 140-149 Reserved[10] - so that table contradicts itself
    # and its Reserved row agrees with the detail table. A byte the
    # specification names Reserved is not a Flag with a Mask.
    (0x14, 0x84, 0x13, 0xCE, 8),    # 14h:132-139 <- 13h:206-213
    # 12h:231-238 <- 12h:239-246, the laser tuning Flags. The odd one out
    # twice over: the Masks share the page with their Flags rather than
    # living on the paired control page, and Table 8-109 gives every bit
    # "Default: 1", so this is the one block where a Flag that never reaches
    # the Interrupt line is the module's shipped behaviour rather than
    # something a host chose.
    (0x12, 0xE7, 0x12, 0xEF, 8),    # 12h:231-238 <- 12h:239-246
)


# Table 8-8, Lower 4-7: one byte per Bank 0-3, bits 0-3 saying "at least one
# Flag is set" on Page 11h, 12h, 14h and 2Ch of that Bank. (bit, page, whether
# this tool has a panel that shows that page's Flags.) Page 2Ch holds the VDM
# Flags, which this tool does not read - so a Flag there asserts Interrupt
# with nothing on any tab to say why, and the summary is the only pointer.
FLAGS_SUMMARY_PAGES = ((0, 0x11, True), (1, 0x12, True), (2, 0x14, True),
                       (3, 0x2C, False))


def parse_flags_summary(raw: bytes) -> list:
    """Lower 4-7 (Table 8-8), RO: [{'bank', 'page', 'shown'}] per bit set.
    Plain status, not a Flag - "To clear a summarized Flag, the Flag itself
    must be read from the relevant Page on the appropriate Bank"."""
    out = []
    for bank, byte_val in enumerate(raw[:4]):
        for bit, page, shown in FLAGS_SUMMARY_PAGES:
            if (byte_val >> bit) & 1:
                out.append({'bank': bank, 'page': '%02Xh' % page,
                            'shown': shown})
    return out


# Table 8-9, Lower 8 bits 1-3: the firmware Flags, RO/COR like the rest of
# the byte. ModuleStateChangedFlag (bit 0) has its own place in the status
# reply, and CdbCmdCompleteFlag1/2 (bits 6-7) theirs below.
# (bit, key, register name)
MODULE_FIRMWARE_FLAGS = (
    (1, 'module_firmware_error', 'ModuleFirmwareErrorFlag'),
    (2, 'datapath_firmware_error', 'DataPathFirmwareErrorFlag'),
    # New in CMIS 5.4; advertised by 0Ch:194.4 AbnormalIndicationSupported.
    (3, 'abnormal_fw_flag', 'AbnormalFwIndicationFlag'),
)


def parse_module_firmware_flags(byte8: int) -> dict:
    """Lower 8 bits 1-3 (Table 8-9), or their Masks at Lower 31 (Table
    8-12) - the Mask byte mirrors the Flag byte bit for bit."""
    return {key: bool((byte8 >> bit) & 1)
            for bit, key, _name in MODULE_FIRMWARE_FLAGS}


# Lower 8 bits 6-7 (Table 8-9): "The module indicates command completion by
# setting Flag 00h:8.6 (CdbCmdCompleteFlag1)" (7.2.5.2), one per CDB instance.
# The status poll reads the byte and clears them with the rest. They were
# left out because this tool sends no CDB command - but its register panel
# does, and the next poll took the completion before anyone could read it.
# (bit, key, register name)
MODULE_CDB_FLAGS = (
    (6, 'cdb_complete_1', 'CdbCmdCompleteFlag1'),
    (7, 'cdb_complete_2', 'CdbCmdCompleteFlag2'),
)


def parse_cdb_complete_flags(byte8: int, instances: int) -> dict:
    """Lower 8 bits 6-7 (Table 8-9), or their Masks at Lower 31.

    `instances` is 01h:163.7-6 (Table 8-55); a flag for an instance the
    module does not have is None. 3 is Reserved - no count at all - and
    reports both bits as read rather than guessing either away."""
    return {key: (bool((byte8 >> bit) & 1) if i < instances else None)
            for i, (bit, key, _name) in enumerate(MODULE_CDB_FLAGS)}


def parse_module_monitor_flags(data: bytes, first: int = 0x08) -> dict:
    """Lower Memory 9-11 (Table 8-9), RO/COR.

    `data` is the Flag block starting at `first`, normally Lower 8-13.

    The Aux and Custom monitor Flags share the block with the temperature
    and Vcc ones, so the read that reports either clears both. Decoding
    part of the block does not leave the rest for the next reader.
    """
    out = {}
    for addr, first_mon, second_mon in MODULE_MONITOR_FLAG_BYTES:
        idx = addr - first
        byte_val = data[idx] if 0 <= idx < len(data) else 0
        for half, prefix in ((0, first_mon), (4, second_mon)):
            for bit, level in enumerate(MONITOR_FLAG_LEVELS):
                out['%s_%s' % (prefix, level)] = bool(
                    byte_val & (1 << (half + bit)))
    return out


def parse_supported_flags(data: bytes) -> dict:
    """01h:157-158 (Table 8-52).

    A Flag the module does not implement reads 0, which is exactly what a
    healthy lane reads. Without this the two are indistinguishable.
    """
    b157 = data[0] if len(data) > 0 else 0
    b158 = data[1] if len(data) > 1 else 0
    return {
        'tx_adaptive_eq_fail': bool(b157 & 0x08),
        'tx_cdr_lol':          bool(b157 & 0x04),
        'tx_los':              bool(b157 & 0x02),
        'tx_fault':            bool(b157 & 0x01),
        'rx_cdr_lol':          bool(b158 & 0x04),
        'rx_los':              bool(b158 & 0x02),
    }


def parse_supported_monitors(data: bytes) -> dict:
    """01h:159-160 (Table 8-53), including the Tx bias scaling factor."""
    b159 = data[0] if len(data) > 0 else 0
    b160 = data[1] if len(data) > 1 else 0
    scale_code = (b160 >> 3) & 0x03
    return {
        'custom':          bool(b159 & 0x20),
        'aux3':            bool(b159 & 0x10),
        'aux2':            bool(b159 & 0x08),
        'aux1':            bool(b159 & 0x04),
        'vcc':             bool(b159 & 0x02),
        'temperature':     bool(b159 & 0x01),
        'rx_optical_power': bool(b160 & 0x04),
        'tx_optical_power': bool(b160 & 0x02),
        'tx_bias':         bool(b160 & 0x01),
        # 11b is reserved, and 8.1.3.8 (CMIS 5.4, M11) says a host "should
        # interpret this value as unknown, unavailable, or out of the known
        # range". Reading it as x1 printed every bias reading and threshold
        # in a unit nobody stated - off by up to 4x with nothing to say so.
        # None: the scale, and so every bias figure, is unknown.
        'tx_bias_scale':   {0: 1, 1: 2, 2: 4}.get(scale_code),
        'tx_bias_scale_code': scale_code,
    }


def parse_loopback_caps(byte_val: int) -> dict:
    """13h:128 (Table 8-111)."""
    return {
        'media_side_output': bool(byte_val & 0x01),
        'media_side_input':  bool(byte_val & 0x02),
        'host_side_output':  bool(byte_val & 0x04),
        'host_side_input':   bool(byte_val & 0x08),
        'per_lane_host':     bool(byte_val & 0x10),
        'per_lane_media':    bool(byte_val & 0x20),
        'simultaneous_host_and_media': bool(byte_val & 0x40),
    }


def parse_diag_meas_caps(byte_val: int) -> dict:
    """13h:129 (Table 8-112)."""
    return {
        'gating_support': (byte_val >> 6) & 0x03,
        'gating_results': bool(byte_val & 0x20),
        'periodic_updates': bool(byte_val & 0x10),
        'per_lane_gating_timers': bool(byte_val & 0x08),
        'auto_restart_gating': bool(byte_val & 0x04),
    }


def parse_diag_reporting_caps(byte_val: int) -> dict:
    """13h:130 (Table 8-113), RO and Required.

    Each bit says whether a DiagnosticsSelector value is supported at all. A
    module that does not support one still answers a read of Page 14h - with
    whatever is in that window - so asking without checking here produces a
    number that looks like a measurement and is not one.
    """
    return {
        'media_side_fec':  bool(byte_val & 0x80),
        'host_side_fec':   bool(byte_val & 0x40),
        'media_side_snr':  bool(byte_val & 0x20),   # selector 06h
        'host_side_snr':   bool(byte_val & 0x10),   # selector 06h
        'bits_and_errors': bool(byte_val & 0x02),   # selectors 02h-05h
        'bit_error_ratio': bool(byte_val & 0x01),   # selector 01h
    }


MEASUREMENT_TIMES = {0: None, 1: 5.0, 2: 10.0, 3: 30.0, 4: 60.0,
                     5: 120.0, 6: 300.0}


def user_pattern_max_bytes(b140: int) -> int:
    """13h:140 bits 3-0 (Table 8-118), RO.

    UserPatternLengthSupported: "the field value n encodes L as L=2(n+1),
    i.e. 0000b: 2 bytes, ..., 1111b: 32 bytes". A module may take far less
    than the 32 bytes Table 8-134 reserves.
    """
    return 2 * ((b140 & 0x0F) + 1)


def parse_measurement_controls(b177: int) -> dict:
    """13h:177 (Table 8-127), RW.

    What window the free-running error statistics cover. MeasurementTime 000b
    is "ungated, counters accrue indefinitely", which makes a BER reading a
    total since whenever ResetErrorInformation was last toggled rather than a
    rate over any stated period.
    """
    code = (b177 >> 1) & 0x07
    seconds = MEASUREMENT_TIMES.get(code)
    return {
        'start_stop_is_global': bool(b177 & 0x80),
        'reset_error_information': bool(b177 & 0x20),
        'auto_restart_gating': bool(b177 & 0x10),
        'measurement_time_code': code,
        # None where the module is not gating at all, and where the gate time
        # is the vendor's own (111b) rather than one of the coded intervals.
        'gate_seconds': seconds,
        'gated': code != 0,
        'custom_gate': code == 7,
        'update_period_s': 5.0 if b177 & 0x01 else 1.0,
    }


def parse_clock_sources(b176: int, b178: int) -> dict:
    """13h:176 and 13h:178 (Table 8-127), both RW and Optional.

    Each pattern generator and checker takes its clock from one of three
    places: the module's internal clock, a reference clock, or a clock
    recovered from the traffic. The four fields are not coded alike - the
    host generator counts reference clocks by media lane and the media
    generator by host lane, with an extra "all lanes use Reference Clock"
    value the host generator does not have.
    """
    out = {}

    code = (b176 >> 4) & 0x0F
    if code == 0:
        name, ref = 'Internal clock', False
    elif 1 <= code <= 8:
        name, ref = 'Reference clock, media lane %d' % code, True
    elif code == 15:
        name, ref = 'Recovered clock per media lane or Data Path', False
    else:
        name, ref = 'Reserved (%d)' % code, False
    out['host_gen'] = {'code': code, 'name': name, 'uses_reference': ref}

    code = b176 & 0x0F
    if code == 0:
        name, ref = 'Internal clock', False
    elif code == 1:
        name, ref = 'Reference clock', True
    elif 2 <= code <= 9:
        name, ref = 'Reference clock, host lane %d' % (code - 1), True
    elif code == 15:
        name, ref = 'Recovered clock per host lane or Data Path', False
    else:
        name, ref = 'Reserved (%d)' % code, False
    out['media_gen'] = {'code': code, 'name': name, 'uses_reference': ref}

    for key, shift, side in (('host_chk', 2, 'host'), ('media_chk', 0, 'media')):
        code = (b178 >> shift) & 0x03
        name, ref = {
            0: ('Recovered clock from the %s lane or Data Path' % side, False),
            1: ('Internal clock', False),
            2: ('Reference clock', True),
            3: ('Reserved (3)', False),
        }[code]
        out[key] = {'code': code, 'name': name, 'uses_reference': ref}
    return out


def parse_pattern_locations(b131: int) -> dict:
    """13h:131 (Table 8-114), RO and Required.

    Which of the four pattern engines the module has, and where each one sits
    relative to its FEC. Both bits clear means the engine is not there at all:
    13h:144/152/160/168 each name a bit pair of this byte as the
    advertisement for their own Enable byte. With only one bit set the
    Pre/PostFECEnable control has exactly one legal value, because the other
    location has no engine to run in.
    """
    roles = (('media_gen', 7, 6), ('media_chk', 5, 4),
             ('host_gen', 3, 2), ('host_chk', 1, 0))
    out = {}
    for name, pre_bit, post_bit in roles:
        pre = bool((b131 >> pre_bit) & 1)
        post = bool((b131 >> post_bit) & 1)
        out[name] = {
            'pre_fec': pre,
            'post_fec': post,
            'present': pre or post,
            'bits': '%d-%d' % (pre_bit, post_bit),
        }
    return out


def parse_pattern_control_caps(b141: int, b142: int) -> dict:
    """13h:141-142 (Table 8-118 continuation), both RO and Required.

    Two questions per role that the pattern tables answer wrongly without
    them. 141 says whether the DataInvert and SwapSymbolBits bytes exist at
    all - offering those columns on a module without them is a control that
    silently does nothing. 142 says whether Enable and PatternSelect are per
    lane: with the bit clear, enabling lane i "enables lane i (or all lanes of
    the Bank)", and "Lane 1 pattern ... is used for all lanes", so seven of
    the eight rows are decoration.
    """
    roles = (('host_gen', 0, 1), ('host_chk', 2, 3),
             ('media_gen', 4, 5), ('media_chk', 6, 7))
    out = {}
    for name, inv_bit, swap_bit in roles:
        out[name] = {
            'data_invert': bool((b141 >> inv_bit) & 1),
            'data_swap':   bool((b141 >> swap_bit) & 1),
        }
    # 142 pairs them the other way round: pattern in the low bit of each
    # pair, enable in the high one.
    for name, pat_bit, en_bit in (('host_gen', 0, 1), ('host_chk', 2, 3),
                                  ('media_gen', 4, 5), ('media_chk', 6, 7)):
        out[name]['per_lane_pattern'] = bool((b142 >> pat_bit) & 1)
        out[name]['per_lane_enable'] = bool((b142 >> en_bit) & 1)
    return out


def parse_pattern_caps(data: bytes) -> dict:
    """13h:132-139 (Tables 8-116, 8-117), little endian, two bytes per role.

    Returns the supported pattern IDs for each generator and checker.
    """
    roles = (('host_gen', 0), ('media_gen', 2), ('host_chk', 4), ('media_chk', 6))
    out = {}
    for name, off in roles:
        ids = []
        if off + 2 <= len(data):
            low, high = data[off], data[off + 1]
            for bit in range(8):
                if (low >> bit) & 1:
                    ids.append(bit)
                if (high >> bit) & 1:
                    ids.append(bit + 8)
        out[name] = sorted(ids)
    return out


# Table 8-68: how each grid numbers its channels, as (THz per unit of n,
# offset added to n, n must be a multiple of). Every grid counts from 193.1
# THz, and most in units of their own spacing - but not three of them: "the
# offset is defined in units of a third, a sixth, or a 24th of the grid
# resolution" for 75, 150 and 300 GHz (8.7), and the last two are not even
# anchored at n = 0.
GRID_CHANNEL_RULES = {
    0: (0.003125, 0, 1),     # 193.1 + n x 0.003125
    1: (0.00625, 0, 1),      # 193.1 + n x 0.00625
    2: (0.0125, 0, 1),       # 193.1 + n x 0.0125
    3: (0.025, 0, 1),        # 193.1 + n x 0.025
    4: (0.05, 0, 1),         # 193.1 + n x 0.05
    5: (0.1, 0, 1),          # 193.1 + n x 0.1
    6: (0.1 / 3, 0, 1),      # 193.1 + n x 0.1/3
    7: (0.025, 0, 3),        # 193.1 + n x 0.025, n a multiple of 3
    8: (0.025, 3, 6),        # 193.1 + (n+3) x 0.025, n a multiple of 6
    9: (0.0125, -9, 24),     # 193.1 + (n-9) x 0.0125, n a multiple of 24
}


def grid_channel_multiple(code: int) -> int:
    """What channel numbers on this grid have to be a multiple of."""
    return GRID_CHANNEL_RULES.get(code, (None, 0, 1))[2]


def grid_channel_frequency_thz(code: int, n: int):
    """The frequency Table 8-68 gives channel n of this grid, or None for a
    grid code it does not define or an n the grid does not number."""
    rule = GRID_CHANNEL_RULES.get(code)
    if rule is None or n % rule[2]:
        return None
    step, offset, _mult = rule
    return round(193.1 + (n + offset) * step, 6)


GRID_CODES = {0: '3.125 GHz', 1: '6.25 GHz', 2: '12.5 GHz', 3: '25 GHz',
              4: '50 GHz', 5: '100 GHz', 6: '33 GHz', 7: '75 GHz',
              8: '150 GHz', 9: '300 GHz', 15: 'Not available'}


# The pages CMIS 5.4 defines under a "Banked Page" heading: 10h-19h (section
# 8.13 onward), the ranges 1Ah-1Bh, 1Ch, 1Dh, 1Eh-1Fh, 20h-2Fh (VDM),
# 30h-4Fh (C-CMIS) and 50h-5Fh (CMIS-LT) - contiguous from 10h to 5Fh - then
# 60h, 61h, 62h, 6Dh, 9Fh and A0h-AFh.
#
# On one of these a page number alone does not name a register: Bank b holds
# the next eight lanes at the same addresses, so a read that names only the
# page always answers from Bank 0 whatever the host meant.
_BANKED_PAGES = (tuple(range(0x10, 0x60)) + (0x60, 0x61, 0x62, 0x6D, 0x9F)
                 + tuple(range(0xA0, 0xB0)))


def is_banked_page(page: int) -> bool:
    """True if this page is one CMIS defines as Banked."""
    return page in _BANKED_PAGES


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


def default_polarity_scope(lanes: int, page_60h: bool) -> str:
    """Which lanes the eight bits of 01h:171-172 describe (section 8.4.13).

    "When the module supports more than eight lanes, the lane polarities
    defined here are applicable in each group of eight lanes, unless the
    module advertises (see Table 8-58) support for per-lane specifications on
    Page 60h."

    So the same eight bits mean one of three things, and which one depends on
    an advertisement rather than on the register:

      'module'           eight lanes or fewer - they are the module's lanes
      'first_lane_group' wider, with Page 60h: they describe lanes 1-8 only,
                         and 8.30.1 makes them redundant with bank 0 there
      'every_lane_group' wider, without Page 60h: lane 1's bit is also lane
                         9's and lane 17's, and Page 60h is not there to say
                         otherwise
    """
    if lanes <= 8:
        return 'module'
    return 'first_lane_group' if page_60h else 'every_lane_group'


def default_polarity_lanes(entries, lanes: int, page_60h: bool) -> list:
    """The advertisement laid out over the lanes it actually describes.

    Only the repeating case changes anything: eight entries become one per
    lane, so a 16-lane module wired inverted on lane 1 reports lane 9 as
    well. Reporting eight lanes there says nothing about the other eight,
    which is not what the module said.
    """
    if (default_polarity_scope(lanes, page_60h) != 'every_lane_group'
            or len(entries) < 8):
        return entries
    return [dict(entries[i % 8], lane=i + 1) for i in range(lanes)]


# Table 8-18, Lower 56. Each entry is (text, MSM, DPSM, NPSM); None where
# the code does not say.
_CMIS_SM_SUPPORT = {
    0: ('Not stated (pre-CMIS 5.3)', None, None, None),
    1: ('None - e.g. a passive cable', False, False, False),
    2: ('MSM only - Resource Module or fixed transceiver', True, False, False),
    3: ('MSM + DPSM - programmable transceiver', True, True, False),
    4: ('MSM + DPSM + NPSM - Muxceiver', True, True, True),
}


def parse_state_machines(byte_56: int, byte_57: int) -> dict:
    """Lower 56-57 (Table 8-18): which state machines this module runs.

    The tool's whole Data Path tab is about the DPSM - states, Apply, DPInit,
    DPInitPending - and code 1 or 2 says there is no DPSM to be in a state.
    A module can say so while having a full paged memory: code 2 is "MSM only
    (Resource Module or fixed transceiver)", which no memory-model check
    catches.

    Code 0 is the trap. It is not "no state machines": "when undefined (prior
    to CMIS 5.3), the type of module is implicit but can usually be determined
    from MemoryModel (00h:2) and from other advertisements". Reading it as an
    absence would take the Data Path tab away from every module built before
    5.3. So it reports None - not stated - and the memory model stays the
    fallback the specification names.

    Codes 5-FF are Reserved, which is also not an absence.
    """
    text, msm, dpsm, npsm = _CMIS_SM_SUPPORT.get(
        byte_56, ('Reserved (%d)' % byte_56, None, None, None))
    if byte_57 == 0:
        fn = 'Transmission Module'
    elif byte_57 == 1:
        fn = 'ELSFP Resource Module'
    elif byte_57 < 128:
        fn = 'Reserved (%d)' % byte_57
    else:
        fn = 'Custom (%d)' % byte_57
    return {
        'sm_code': byte_56,
        'sm_text': text,
        'sm_stated': byte_56 in _CMIS_SM_SUPPORT and byte_56 != 0,
        'msm': msm,
        'dpsm': dpsm,
        'npsm': npsm,
        'function_type': byte_57,
        'function_text': fn,
    }


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


# Table 8-62 codes every field in this byte the same way, and 00b is not
# "no": it means the module predates CMIS 5.3 and has not been asked.
_TRISTATE = {0: 'unknown', 1: 'not supported', 2: 'supported', 3: 'reserved'}


# Table 8-49. Each code is a range rather than a number, and the upper bound
# is what a host waits on - a state that has run past it has gone wrong.
_STATE_DURATIONS = [
    (0.001, 'under 1 ms'), (0.005, '1-5 ms'), (0.010, '5-10 ms'),
    (0.050, '10-50 ms'), (0.100, '50-100 ms'), (0.500, '100-500 ms'),
    (1.0, '500 ms - 1 s'), (5.0, '1-5 s'), (10.0, '5-10 s'),
    (60.0, '10 s - 1 min'), (300.0, '1-5 min'), (600.0, '5-10 min'),
    (3000.0, '10-50 min'), (None, '50 min or more'),
]


def state_duration(code: int) -> dict:
    """Table 8-49, the encoding shared by every MaxDuration* field."""
    if 0 <= code < len(_STATE_DURATIONS):
        limit, label = _STATE_DURATIONS[code]
    else:
        limit, label = None, 'Reserved (%d)' % code
    return {'code': code, 'max_seconds': limit, 'label': label}


# Table 10-8, Note 1: "Values specified here place an upper limit on
# advertised timings like MaxDurationDPTxTurnOff and MaxDurationDPTxTurnOn
# (01h:168)" - ton_txdis, 100 ms for a Tx output to go off, and toff_txdis,
# 400 ms for it to come on. An advertisement past them is the module's own
# statement that it does not keep to the table.
DP_STATE_SPEC_CEILING = {
    'dp_tx_turn_off': (0.100, 'ton_txdis'),
    'dp_tx_turn_on': (0.400, 'toff_txdis'),
}


def _with_ceiling(field: str, d: dict) -> dict:
    """Add the Table 10-8 ceiling to a DPTxTurnOn/Off advertisement, and say
    whether the advertised range lies wholly above it.

    A Table 8-49 code is a range - "100-500 ms" - so it only certainly
    exceeds the ceiling when its lower end does. Code 0 and the reserved
    codes claim nothing, and count from zero.
    """
    ceiling, symbol = DP_STATE_SPEC_CEILING[field]
    code = d['code']
    lower = (_STATE_DURATIONS[code - 1][0]
             if 0 < code < len(_STATE_DURATIONS) else 0.0)
    return dict(d, spec_ceiling_s=ceiling, spec_symbol=symbol,
                exceeds_spec=lower >= ceiling)


def is_multi_wavelength(media_lane_map) -> bool:
    """Whether a module carries more than one media wavelength.

    Table 8-46 defines the wavelength fields for single wavelength modules
    and leaves their meaning undefined otherwise, so this decides whether
    that advertisement can be read as "the wavelength of this module".

    Counting lanes instead of distinct wavelengths would call a parallel
    module multi-wavelength: eight lanes on eight fibres all carry the same
    one.
    """
    seen = {lane['tx']['wavelength'] for lane in (media_lane_map or [])
            if lane.get('tx', {}).get('wavelength')}
    return len(seen) > 1


# The static pages each carry a checksum "that can be used to verify that the
# read-only static data ... is valid" (section 8.3.11 and the page overviews).
# Three different ranges, and Page 01h is the odd one: it starts at 130
# because "the firmware version bytes 128-129 are intentionally excluded from
# the Page Checksum to avoid requiring a Memory Map update when firmware is
# updated".
PAGE_CHECKSUMS = (
    (0x00, 222, 128, 221),
    (0x01, 255, 130, 254),
    (0x02, 255, 128, 254),
    (0x04, 255, 128, 254),
)


def page_checksum(data: bytes, first: int, last: int, base: int = 128) -> int:
    """The low order 8 bits of the arithmetic sum of the covered bytes.

    `data` starts at `base`, so the caller can pass a whole upper page.
    """
    lo = first - base
    hi = last - base + 1
    if lo < 0 or hi > len(data):
        raise ValueError('checksum range %d-%d not covered by %d bytes'
                         % (first, last, len(data)))
    return sum(data[lo:hi]) & 0xFF


def parse_nad_support(byte_175: int) -> dict:
    """01h:175 (Table 8-59), the Normalized Application Descriptor banks.

    Zero means the module advertises its Applications the classical way and
    the basic descriptors are all there is. A non-zero n means up to n*15
    Applications live on n banks of Page 1Ch, and the fifteen a host can see
    without reading them are a prefix rather than the set.

    The field widened in CMIS 5.4, and the specification says why that
    matters: an older host reads n > 15 as n mod 16 and "may even fall back
    to seeing only the first 15 Applications". Reading the whole byte is the
    difference between knowing there are more and not.
    """
    banks = byte_175 & 0xFF
    return {
        'banks': banks,
        'supported': banks > 0,
        'max_applications': banks * 15,
    }


def parse_wavelength_info(data: bytes) -> dict:
    """01h:138-141 (Table 8-46), RO and Conditional.

    Two different scales: the nominal wavelength counts 0.05 nm and the
    tolerance 0.005 nm, so one factor for both is wrong by ten on whichever
    it is not. Zero is not a wavelength any module emits, so it reads as the
    field not being provided.

    Defined "for single wavelength modules". A multi-wavelength module may
    fill it in for one wavelength or for the whole range, and the
    specification says the interpretation is not uniquely defined and a host
    may ignore it - so the caller has to know which kind of module it has
    before showing this as the wavelength.
    """
    if len(data) < 4:
        return {'nominal_nm': None, 'tolerance_nm': None}
    nominal = (data[0] << 8) | data[1]
    tolerance = (data[2] << 8) | data[3]
    return {
        'nominal_nm': round(nominal * 0.05, 3) if nominal else None,
        'tolerance_nm': round(tolerance * 0.005, 4) if tolerance else None,
    }


def parse_module_limits(data: bytes) -> dict:
    """01h:146-150 (Table 8-50), RO and Conditional.

    "ModuleTempMax = ModuleTempMin = 0 indicates 'not specified'", and the
    propagation delay and minimum voltage each use zero for the same thing -
    so a module that has not been told its own limits says so rather than
    claiming to run from 0 V at 0 C.
    """
    if len(data) < 5:
        return {'temp_max_c': None, 'temp_min_c': None,
                'propagation_delay_ns': None, 'voltage_min_v': None}
    t_max = data[0] - 256 if data[0] > 127 else data[0]
    t_min = data[1] - 256 if data[1] > 127 else data[1]
    delay = (data[2] << 8) | data[3]
    volts = data[4]
    return {
        # Both zero is the specification's "not specified"; either one alone
        # being zero is a real limit at 0 C.
        'temp_max_c': None if (t_max == 0 and t_min == 0) else t_max,
        'temp_min_c': None if (t_max == 0 and t_min == 0) else t_min,
        'propagation_delay_ns': (delay * 10) if delay else None,
        'voltage_min_v': round(volts * 0.02, 2) if volts else None,
    }


def parse_durations(b143: int, b144: int, ext: bytes = b'') -> dict:
    """01h:143-144 and 167-169 (Tables 8-48 and 8-56).

    ModSelWaitTime is a small floating point value in microseconds, m*2^e,
    with 00h meaning no data available. The MaxDuration fields all share the
    Table 8-49 encoding.
    """
    mantissa = b143 & 0x1F
    exponent = (b143 >> 5) & 0x07
    out = {
        'modsel_wait_us': (mantissa << exponent) if b143 else None,
        'dp_init': state_duration(b144 & 0x0F),
        'dp_deinit': state_duration((b144 >> 4) & 0x0F),
    }
    if len(ext) >= 3:
        out['module_pwr_up'] = state_duration(ext[0] & 0x0F)
        out['module_pwr_dn'] = state_duration((ext[0] >> 4) & 0x0F)
        out['dp_tx_turn_on'] = _with_ceiling(
            'dp_tx_turn_on', state_duration(ext[1] & 0x0F))
        out['dp_tx_turn_off'] = _with_ceiling(
            'dp_tx_turn_off', state_duration((ext[1] >> 4) & 0x0F))
        # tBPC is 10 ms; the module may need only tBPC / 2^i of it.
        bpc = ext[2] & 0x0F
        out['bpc_shift'] = bpc
        out['bpc_seconds'] = 0.010 / (2 ** bpc)
    return out


def parse_misc_features(byte_251: int) -> dict:
    """01h:251 (Table 8-62), RO and Required.

    Note the two ways the specification refers to the same fields: section
    8.16.13 calls the scratchpad advertisement "01h:251.7" and section
    5.2.2.1 calls full page read "01h:251.4", while this table places them at
    bits 7-6 and 1-0. The table is the register definition, so it wins.
    """
    out = {}
    for name, shift in (('scratch_pad', 6), ('password_entry', 4),
                        ('password_entry_result', 2), ('full_page_read', 0)):
        code = (byte_251 >> shift) & 0x03
        out[name] = _TRISTATE[code]
        out[name + '_code'] = code
    return out


def max_read_bytes(byte_251: int) -> int:
    """Section 5.2.2.1: "By default, Nmax = 8. When full page read is
    supported ... then Nmax = 128"."""
    return 128 if ((byte_251 & 0x03) == 2) else 8


def parse_misc_caps(byte_252: int) -> dict:
    """01h:252 (Table 8-62). Bit 5 is the 5.4 media lane switching
    advertisement, bit 7 the host lane switching one (Page 1Dh)."""
    return {'media_lane_switching_supported': bool((byte_252 >> 5) & 1),
            'host_lane_switching_supported': bool((byte_252 >> 7) & 1)}


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


# ---------------------------------------------------------------------------
# CMIS 5.4 optional pages
# ---------------------------------------------------------------------------
# Presence is advertised in 01h:173-174; none of these may be read blindly.
REG_SUPPORTED_PAGES_MAP = (0x0C, 0x80, 32)   # 0Ch:128-159 U8[32] page bitmap
# Page 0Dh (8.12, Tables 8-75 and 8-76): the firmware banks in registers,
# "without forcing the host to use CDB messaging".
REG_FW_MGMT_CAPS     = (0x0D, 0x80, 1)    # 0Dh:128 CapabilitiesRegister
REG_FW_LOADS_STATUS  = (0x0D, 0x88, 1)    # 0Dh:136 LoadsStatusRegister
REG_FW_LOAD_VERSIONS = {'A': (0x0D, 0x94, 36),      # 148-183 VersionLoadA
                        'B': (0x0D, 0xB8, 36),      # 184-219 VersionLoadB
                        'Fixed': (0x0D, 0xDC, 36)}  # 220-255 VersionFixedLoad
REG_CONSOLIDATED_PM     = (0x0C, 0xA0, 2)    # 0Ch:160-161 FeatureAdvertisement
# The other named feature in Table 8-72, and the same structure.
REG_LOAD_MANAGEMENT     = (0x0C, 0xA2, 2)    # 0Ch:162-163
# 192-195 (Table 8-73), the details each named feature's compliance
# claim is measured against. 196-223 is Reserved.
REG_FEATURE_DETAILS     = (0x0C, 0xC0, 4)    # 0Ch:192-195
REG_POLARITY_STATUS     = (0x60, 0x80, 2)    # 60h:128-129 actual lane polarity
REG_ACQ_COUNTER_ADV     = (0x60, 0x82, 1)    # 60h:130 counter support (bank 0)
REG_RESET_ACQ_RX        = (0x60, 0xC0, 1)    # 60h:192 WO bitmask, media lanes
REG_RESET_ACQ_TX        = (0x60, 0xC1, 1)    # 60h:193 WO bitmask, host lanes
REG_ACQ_COUNTERS        = (0x61, 0x80, 64)   # 61h:128-191 four U16[8] arrays
REG_LANE_PWR_THRESHOLDS = (0x62, 0x80, 64)   # 62h:128-191 8B quad per lane
REG_MLS_ADVERT          = (0x6D, 0x80, 1)    # 6Dh:128 commit duration code
REG_MLS_REDIRECTION     = (0x6D, 0x88, 8)    # 6Dh:136-143 target lane per lane
REG_MLS_ENABLE          = (0x6D, 0x98, 1)    # 6Dh:152 bit0 enable
REG_MLS_COMMIT          = (0x6D, 0xA0, 1)    # 6Dh:160 bit0 commit (WO/SC)
REG_MLS_RESULT          = (0x6D, 0xA8, 8)    # 6Dh:168-175 per-lane commit result
REG_MLS_STATUS          = (0x6D, 0xB8, 8)    # 6Dh:184-191 committed mapping (RO)
# Page 1Dh, the host lane switch (8.25, Table 8-176), laid out like 6Dh:
# RedirectionOfLane<i> / RedirectStatusOfLane<i> name the *nominal* host lane
# that electrical lane <i> is connected to.
REG_HLS_ADVERT          = (0x1D, 0x80, 1)    # 1Dh:128 commit duration code
REG_HLS_REDIRECTION     = (0x1D, 0x88, 8)    # 1Dh:136-143 provisioned
REG_HLS_ENABLE          = (0x1D, 0x98, 1)    # 1Dh:152 bit0 enable
REG_HLS_RESULT          = (0x1D, 0xA8, 8)    # 1Dh:168-175 per-lane commit result
REG_HLS_STATUS          = (0x1D, 0xB8, 8)    # 1Dh:184-191 committed mapping (RO)


def parse_supported_pages_map(raw: bytes) -> list:
    """Page indices a module says it has, from the 0Ch bitmap (Table 8-70).

    Bit k of byte 128+n means page n*8+k. Returned as page numbers rather than
    a bitfield because that is the only form anyone reads it in.
    """
    pages = []
    for n, byte in enumerate(raw[:32]):
        for k in range(8):
            if (byte >> k) & 1:
                pages.append(n * 8 + k)
    return pages


# Table 8-73. (bit, name, value required for full support, description).
# Each row in the specification states the value that full support wants, so
# the required pattern can be built from the rows rather than retyped.
NA_SUPPORT_BITS = (
    (7, 'NaSupported', 1, 'NA values are supported for observable samples'),
    (6, 'NaFeedsSupervision', 0,
     'NA values not fed to / ignored by threshold detectors'),
    (5, 'NaFeedsRangeStats', 1, 'NA values feed range statistics min and max'),
    (4, 'NaFeedsAverages', 1, 'NA values feed average computations'),
    (3, 'NaFeedsCounterStats', 1, 'NA values feed event counter stats'),
    (2, 'NaSaturatesTotals', 1, 'NA values saturate non-random totals'),
    (1, 'NaFeedsDiagnostics', 0,
     'NA not fed to diagnostics statistics observables'),
)

FW_SUPPORT_BITS = (
    (7, 'UniqueLoadVersionSupported', 1,
     'Version information is characteristic of entire load'),
    (6, 'DualBankSupported', 1, 'Banks A and B are supported'),
    (5, 'FirmwareLoadTagSupported', 1,
     'Persistent Firmware Load Tag is supported'),
    (4, 'AbnormalIndicationSupported', 1,
     'Abnormal Firmware Indication is supported'),
    (3, 'TransferIsHarmless', 1,
     'Transfer does not impact mission integrity and quality'),
    (2, 'RejectUnsupportedActivation', 1,
     'Module rejects if activation has unwanted side effects'),
)


def _required_from_bits(bits) -> int:
    """The byte the per-bit rows together ask for."""
    out = 0
    for bit, _name, want, _desc in bits:
        out |= (want & 1) << bit
    return out


# What "fully supported" requires, from each place the specification says it.
#
# For the PM register all three agree on 1011 1100b. For the firmware one they
# do not, and the disagreement is three-way:
#
#   Table 8-72 cross-reference  "set to 11110000b"          0xF0
#   Table 8-73 header row       "1111 1000b: fully ..."     0xF8
#   Table 8-73 per-bit rows     bits 7-2 each state 1b      0xFC
#
# Checked against the rendered page, not only the extracted text. Nothing
# else in OIF-CMIS-05.4 settles it, so no single value is used as the answer.
NA_FULL_PATTERNS = {
    'Table 8-72': 0xBC,
    'Table 8-73 header': 0xBC,
    'Table 8-73 bit rows': _required_from_bits(NA_SUPPORT_BITS),
}

FW_FULL_PATTERNS = {
    'Table 8-72': 0xF0,
    'Table 8-73 header': 0xF8,
    'Table 8-73 bit rows': _required_from_bits(FW_SUPPORT_BITS),
}


def parse_support_details(byte_val: int, bits, full_patterns: dict,
                          partial_mask: int) -> dict:
    """One of the two Named Feature Details registers (Table 8-73).

    `partial_mask` is the pattern the header gives for partial support: bit 7
    alone for the PM register, bits 7 and 3 for the firmware one.

    "Fully supported" is reported per source rather than as one verdict,
    because the specification gives the firmware register three different
    required values and settles on none of them.
    """
    fields = []
    for bit, name, want, desc in bits:
        got = (byte_val >> bit) & 1
        fields.append({
            'bit': bit, 'name': name, 'value': got,
            'wanted_for_full': want, 'meets': got == want,
            'description': desc,
        })
    matches = sorted(k for k, v in full_patterns.items() if v == byte_val)
    return {
        'raw': byte_val,
        'fields': fields,
        'full_patterns': dict(full_patterns),
        # Which of the specification's statements this byte satisfies. Empty
        # is not "not supported" on its own - see partial below.
        'full_per_source': matches,
        'full': bool(matches),
        'partial': (byte_val & partial_mask) == partial_mask,
    }


def feature_claim_conflicts(feature: dict, details: dict, label: str) -> list:
    """Where a module's compliance claim and its own details disagree.

    8.12: "when a module declares full support of a named feature, the
    following feature details advertisements must conform to the options
    profile defined by that named feature". So a module advertising
    OptionsProfileCompliance 3 whose details byte satisfies none of the
    required patterns has contradicted itself.

    Flagged only when the byte matches none of them. Where the specification
    gives several values, matching any one is the module following the
    specification as written somewhere.
    """
    if not feature or not feature.get('supported') or not details:
        return []
    if feature.get('options_profile_compliance') != 3:
        return []
    if details['full_per_source']:
        return []
    return [{
        'feature': label,
        'raw': details['raw'],
        'expected': dict(details['full_patterns']),
        'failing': [f['name'] for f in details['fields'] if not f['meets']],
    }]


# Table 7-8 (7.10.2): each basic performance monitor has a pseudo-NA value,
# "a special sample value ... representing the situation when the relevant
# monitor cannot provide a valid sample, for any reason". A module says it
# uses them with NaSupported (0Ch:192.7, Table 8-73), under the Consolidated
# PM feature (0Ch:160-161). Raw register values, before any scaling.
NA_TEMPERATURE = -32768                 # TempMon, S16
NA_VCC = 0                              # VccMon, U16
NA_AUX = {'tec_current': -32768,        # S16
          'laser_temperature': -32768,  # S16
          'vcc2': 0}                    # Aux3 additional voltage
NA_TX_POWER = 0                         # OpticalPowerTx, U16
NA_TX_BIAS = 0                          # LaserBiasTx, U16
# "Optical Power is the only monitor where static and dynamic reasons of
# unavailability are distinguished" (note 3): 0 for a lane not in use, 1 for
# a lane in use that has no valid sample.
NA_RX_POWER = {0: 'lane not in use', 1: 'no valid sample'}
NA_LASER_FREQ = 0                       # LaserFrequencyTx, U32
NA_SNR = 0                              # SNR, U16
NA_BER = 0.5                            # Pattern BER, F16
NA_ERROR_COUNT = 2 ** 64 - 1            # Pattern bit errors, MAX(U64)


def is_na_ber(value: float) -> bool:
    """F16 has more than one encoding of 0.5 (500e-3, 5e-1), so the NA is
    recognised by its value."""
    return abs(value - NA_BER) < 1e-12


def na_values_advertised(pm_adv: bytes, details_byte: int) -> bool:
    """Whether this module reports Table 7-8's NA values: Consolidated PM
    advertised (Table 8-71), and NaSupported set in its details (Table 8-73).
    "When a module advertises that a named feature is not supported, the
    feature details of that feature ... should be ignored by the host"."""
    return (parse_feature_advertisement(pm_adv)['supported']
            and bool(details_byte & 0x80))


def parse_feature_advertisement(raw: bytes) -> dict:
    """Table 8-71. Byte 0 is the CMIS revision the feature is defined by, and
    zero there means the feature is absent - not "revision 0.0"."""
    if len(raw) < 2:
        return {'supported': False}
    rev, comp = raw[0], raw[1]
    opts, reqs = (comp >> 4) & 0x0F, comp & 0x0F
    return {
        'supported': rev != 0,
        'defined_in': f'{(rev >> 4) & 0x0F}.{rev & 0x0F}' if rev else '',
        'options_profile_compliance': opts,
        'requirements_compliance': reqs,
        # COMPLIANCE_NAMES existed and nothing used it, so the panel printed
        # the raw nibbles - two numbers where the specification has four
        # named levels, one of which is "not answered".
        'options_profile_compliance_name': compliance_name(opts),
        'requirements_compliance_name': compliance_name(reqs),
    }


# Table 8-71, the low and high nibbles of byte 1. The specification defines
# four codes and stops there.
COMPLIANCE_NAMES = {0: 'undefined, unknown', 1: 'noncompliant',
                    2: 'partially compliant, with exceptions',
                    3: 'fully compliant'}


def compliance_name(code: int) -> str:
    """One of the four codes, or a code Table 8-71 does not define.

    Zero is the one that matters: "undefined, unknown", not a bottom score.
    Printed as a bare 0 beside a 3 it reads as the worse of two results,
    when it is the module saying it has not answered.
    """
    if code in COMPLIANCE_NAMES:
        return COMPLIANCE_NAMES[code]
    return 'Undefined code (%d)' % code


def parse_polarity_status(raw: bytes) -> list:
    """60h:128-129 (Table 8-188): the polarity actually in effect.

    Distinct from 01h:171-172, which only advertises power-up polarity. A
    module with programmable inversion reflects the live state here and leaves
    the Page 01h advertisement alone, so the two can legitimately disagree.
    """
    if len(raw) < 2:
        return []
    tx, rx = raw[0], raw[1]
    return [{'lane': i + 1,
             'input_tx_inverted': bool((tx >> i) & 1),
             'output_rx_inverted': bool((rx >> i) & 1)} for i in range(8)]


def parse_acquisition_counters(raw: bytes) -> list:
    """61h:128-191 (Table 8-191): four U16[8] arrays, lane 1 first.

    Saturating counters of how often a receiver re-acquired lock since the
    data path was commissioned; the module clears them itself on DPInit. A
    rising count on a link that looks up is the signal worth having.
    """
    if len(raw) < 64:
        return []
    def u16(base, i):
        off = base + i * 2
        return (raw[off] << 8) | raw[off + 1]
    # Table 8-191 orders the four arrays Rx first: 61h:128-143 per Rx media
    # lane, 144-159 per Tx host lane, then the same order for the Data Path
    # counters. Reading them Tx-first sends the user to the wrong side of the
    # module - a media receiver losing lock would show up under "Lane Tx".
    return [{'lane': i + 1,
             'acq_rx': u16(0, i), 'acq_tx': u16(16, i),
             'dp_acq_rx': u16(32, i), 'dp_acq_tx': u16(48, i)}
            for i in range(8)]


def parse_lane_power_thresholds(raw: bytes) -> list:
    """62h:128-191 (Tables 8-193/8-194): per media lane, a quad of S16 in
    0.01 dBm - hi alarm, lo alarm, hi warning, lo warning, in that order.

    These are the absolute thresholds that result once a lane switches to
    power-relative supervision, which is why they are per lane while the
    offsets that produce them (12h:216-217) are not.
    """
    out = []
    for i in range(8):
        off = i * 8
        if off + 8 > len(raw):
            break
        vals = struct.unpack('>4h', raw[off:off + 8])
        out.append({'lane': i + 1,
                    'hi_alarm_dbm': vals[0] * 0.01,
                    'lo_alarm_dbm': vals[1] * 0.01,
                    'hi_warn_dbm': vals[2] * 0.01,
                    'lo_warn_dbm': vals[3] * 0.01})
    return out


# Table 8-196, 6Dh:168-175. Success is 1 and in-progress is 2, not the other
# way round: reporting a commit still running as "Success" invites the operator
# to move traffic onto a switch configuration that is not in effect yet.
MLS_RESULT_NAMES = {
    0: 'No status',
    1: 'Success',
    2: 'In progress',
    3: 'Rejected: validation failure',
    4: 'Rejected: not a permutation',
    5: 'Rejected: conflicts with active DataPath',
    6: 'Rejected: lane ordering unsupported',
}

# The same table read as classes, which is what a reader scanning the column
# needs: four of the seven codes are rejections, and ">6 Reserved" is a code
# the specification does not define rather than one more kind of result.
# Printed as words in one neutral style, "Rejected: conflicts with active
# DataPath" sat beside "Success" looking like the same sort of answer.
MLS_RESULT_REJECTED = frozenset({3, 4, 5, 6})


def mls_result_kind(code: int) -> str:
    """'none', 'success', 'in_progress', 'rejected' or 'reserved'."""
    if code in MLS_RESULT_REJECTED:
        return 'rejected'
    return {0: 'none', 1: 'success', 2: 'in_progress'}.get(code, 'reserved')


def mls_group_valid(group, present=None) -> bool:
    """Section 7.9.3: one group's RedirectionOfMediaLane<i> values.

    There are always eight external media lanes, and n internal ones - "the
    number of media lanes per bank advertised as being supported by the
    module" (7.9.1, via 00h:210). Only the first n locations hold a target
    in {1, ..., 8}; if n is less than 8 the rest "will be populated with 0".
    A valid configuration is "a one-to-one mapping of the n internal media
    lanes to n of the 8 external media lanes" (7.9.4) - a permutation only
    when the module has all eight.

    `present` says which of the group's internal media lanes the module has;
    left out, all of them.
    """
    present = list(present) if present is not None else [True] * len(group)
    have = [t for t, p in zip(group, present) if p]
    return (all(1 <= t <= 8 for t in have) and len(set(have)) == len(have)
            and all(t == 0 for t, p in zip(group, present) if not p))


def mls_result_name(code: int) -> str:
    return MLS_RESULT_NAMES.get(code, 'Reserved (%d)' % code)


def mls_disabled_groups(enables) -> list:
    """Groups of eight lanes (from 1) whose redirection is disabled.

    Table 8-196, EnableMediaLaneRedirection: "0b: disabled: commit command is
    without effect". Not rejected - without effect: the module changes
    nothing and writes no RedirectionCommitResult to say so. A commit sent
    there is swallowed whole, so it is the host that has to notice.
    """
    return [b + 1 for b, e in enumerate(enables) if not (e & 1)]


def parse_media_lane_switching(advert: int, redirection: bytes,
                               enable, result: bytes,
                               status: bytes = b'', lanes_total: int = 8,
                               present=None) -> dict:
    """6Dh (Table 8-196): which external media lane each internal one feeds.

    Table 8-196 keeps two arrays apart on purpose: 136-143 is what the host has
    staged (RW) and 184-191 is what the switch is actually doing (RO). They
    differ whenever a commit was rejected or never issued - and the spec notes
    enabling alone does not commit - so showing only the staged one would report
    a mapping the hardware is not using.

    A valid redirection is a permutation, so a duplicate or a zero here is the
    module reporting something the host should not commit; the UI shows the raw
    mapping rather than tidying it, because tidying would hide exactly that.

    Page 6Dh is banked, and section 8.33 says each Bank "provides space for
    media lane switching functionality within a group of 8 lanes". Every field
    here is numbered {1, ..., 8}, so a target is a lane of its own group and
    the switch cannot move traffic across groups. `redirection`, `result` and
    `status` are the banks concatenated, eight bytes each; `enable` is one
    value per bank, or a single value for a module that has one bank.

    A raw target therefore means different lanes in different banks: 3 in bank
    1 is lane 11. Both are reported - `redirected_to` is the absolute lane, so
    the table can be read straight down, and `redirected_to_raw` is what the
    register holds.

    `present` is per lane: whether the module has that internal media lane
    (00h:210). Where it does not, 7.9.3 has the register hold 0, and that is
    the right answer rather than a broken mapping.
    """
    enables = list(enable) if isinstance(enable, (list, tuple)) else [enable]
    present = (list(present) if present is not None
               else [True] * min(lanes_total, len(redirection)))
    lanes = []
    for i in range(min(lanes_total, len(redirection))):
        bank, within = divmod(i, 8)
        raw = redirection[i]
        act = status[i] if i < len(status) else None
        res = result[i] if i < len(result) else 0
        lanes.append({
            'lane': i + 1,
            'bank': bank,
            'lane_in_bank': within + 1,
            # A target outside 1-8 is the module reporting something invalid,
            # and turning it into an absolute lane would invent a lane number
            # for it. Left as-is so the row still shows what was read.
            'redirected_to': bank * 8 + raw if 1 <= raw <= 8 else raw,
            'redirected_to_raw': raw,
            'active_target': (bank * 8 + act if act is not None and 1 <= act <= 8
                              else act),
            'active_target_raw': act,
            'commit_result': res,
            'commit_result_name': mls_result_name(res),
            'commit_result_kind': mls_result_kind(res),
            'media_lane_present': bool(present[i]) if i < len(present) else True,
        })
    # The permutation has to hold inside each group, not across the module:
    # a target is a lane of its own group, so eight lanes redirected to 1-8 in
    # bank 1 is valid and would fail a check run over the whole list.
    banks_ok = []
    valid_banks = []
    enabled_banks = []
    for bank in range(0, (len(lanes) + 7) // 8):
        group = [l['redirected_to_raw'] for l in lanes[bank * 8:bank * 8 + 8]]
        banks_ok.append(sorted(group) == list(range(1, len(group) + 1)))
        valid_banks.append(mls_group_valid(
            group, [l['media_lane_present'] for l in lanes[bank * 8:bank * 8 + 8]]))
        enabled_banks.append(bool((enables[bank] if bank < len(enables)
                                   else 0) & 1))
    return {
        'commit_duration_code': (advert >> 4) & 0x0F,
        'commit_duration_label': state_duration(
            (advert >> 4) & 0x0F).get('label'),
        # One checkbox, so it may only read enabled when every group is: a
        # module with one group enabled and one not is switching half its
        # lanes, which is neither of the two states the box can draw.
        'enabled': bool(enabled_banks) and all(enabled_banks),
        'enabled_banks': enabled_banks,
        'lanes': lanes,
        # Called out rather than corrected: a non-permutation is a module bug
        # or an unfinished commit, and committing it would be the wrong move.
        'is_permutation': all(banks_ok),
        'permutation_banks': banks_ok,
        # What the specification asks for (7.9.3), which is a permutation
        # only on a module with all eight media lanes.
        'mapping_valid': all(valid_banks),
        'mapping_valid_banks': valid_banks,
        # True only when every lane's staged target is the one in effect.
        'committed': bool(status) and all(
            l['active_target'] == l['redirected_to'] for l in lanes),
        # Result code 2 is "Command execution in progress", and a commit that
        # is still running is not a commit that failed to happen. Reported
        # apart from `committed` because the two need opposite advice: one
        # says wait, the other says press the button.
        'commit_in_progress': any(l['commit_result'] == 2 for l in lanes),
    }


# ---------------------------------------------------------------------------
# SFF-8024 code tables
# ---------------------------------------------------------------------------
# CMIS defines none of these itself: it stores an Interface ID and points at
# SFF-8024 for the meaning, so without these tables the UI can only show a
# number. Transcribed from SFF-8024 Rev 4.14; CMIS 5.4 cites rev 4.10, and the
# later revision only adds codes, so a module built against either still reads
# correctly here.
#
# All of these are the GID=0 tables. CMIS 5.4 widened Interface IDs to a 12-bit
# UID whose top four bits select a different table, but every UID defined so
# far has GID 0, and a non-zero one can only be advertised through a Normalized
# Application Descriptor, which this tool does not read.
HOST_INTERFACE_IDS = {
    0x01: '1000BASE-CX',
    0x02: 'XAUI',
    0x03: 'XFI',
    0x04: 'SFI',
    0x05: '25GAUI C2M',
    0x06: 'XLAUI C2M',
    0x07: 'XLPPI',
    0x08: 'LAUI-2 C2M',
    0x09: '50GAUI-2 C2M',
    0x0A: '50GAUI-1 C2M',
    0x0B: 'CAUI-4 C2M (Annex 83E)1',
    0x0C: '100GAUI-4 C2M',
    0x0D: '100GAUI-2 C2M',
    0x0E: '200GAUI-8 C2M',
    0x0F: '200GAUI-4 C2M',
    0x10: '400GAUI-16 C2M',
    0x11: '400GAUI-8 C2M',
    0x13: '10GBASE-CX4',
    0x14: '25GBASE-CR CA-25G-L',
    0x15: '25GBASE-CR or 25GBASE-CR-S',
    0x16: '25GBASE-CR or 25GBASE-CR-S',
    0x17: '40GBASE-CR4',
    0x18: '50GBASE-CR',
    0x19: '100GBASE-CR10',
    0x1A: '100GBASE-CR4',
    0x1B: '100GBASE-CR2',
    0x1C: '200GBASE-CR4',
    0x1D: '400G CR8 (Ethernet Technology',
    0x1E: '200GBASE-CR1',
    0x1F: '400GBASE-CR2',
    0x20: 'LEI-100G-PAM4-1',
    0x21: 'LEI-200G-PAM4-2',
    0x22: 'LEI-400G-PAM4-4',
    0x23: 'LEI-800G-PAM4-8',
    0x2C: 'codes',
    0x2D: 'IB DDR',
    0x2E: 'IB QDR',
    0x2F: 'IB FDR',
    0x33: 'E.96',
    0x35: 'E.119',
    0x36: 'E.238',
    0x37: 'OTL3.4 (ITU-T G.709/Y.1331',
    0x38: 'OTL4.10 (ITU-T G.709/Y.1331',
    0x39: 'OTL4.4 (ITU-T G.709/Y.1331',
    0x3A: 'OTLC.4 (ITU-T G.709.1/Y.1331',
    0x3B: 'FOIC1.4-MFI (ITU-T',
    0x3C: 'FOIC1.2-MFI (ITU-T',
    0x3D: 'FOIC2.8-MFI (ITU-T',
    0x3E: 'FOIC2.4-MFI (ITU-T',
    0x3F: 'FOIC4.16-MFI (ITU-T',
    0x40: 'FOIC4.8-MFI (ITU-T',
    0x41: 'CAUI-4 C2M (Annex 83E) without',
    0x42: 'CAUI-4 C2M (Annex 83E) with',
    0x43: '50GBASE-CR2 (Ethernet Technology',
    0x44: '50GBASE-CR2 (Ethernet Technology',
    0x45: '50GBASE-CR2 (Ethernet Technology',
    0x46: '100GBASE-CR1',
    0x47: '200GBASE-CR2',
    0x48: '400GBASE-CR4',
    0x49: '800G-ETC-CR8 or 800GBASE-CR8',
    0x4B: '100GAUI-1-S C2M',
    0x4C: '100GAUI-1-L C2M',
    0x4D: '200GAUI-2-S C2M',
    0x4E: '200GAUI-2-L C2M',
    0x4F: '400GAUI-4-S C2M',
    0x50: '400GAUI-4-L C2M',
    0x51: '800GAUI-8 S C2M',
    0x52: '800GAUI-8 L C2M',
    0x53: 'OTL',
    0x55: '1.6TAUI-16-S C2M',
    0x56: '1.6TAUI-16-L C2M',
    0x57: '800GBASE-CR4',
    0x58: '1.6TBASE-CR8',
    0x70: 'PCIe',
    0x71: 'PCIe',
    0x72: 'PCIe',
    0x73: 'PCIe',
    0x74: 'CEI-112G-LINEAR-PAM4',
    0x80: '200GAUI-1 C2M',
    0x81: '400GAUI-2 C2M',
    0x82: '800GAUI-4 C2M',
    0x83: '1.6TAUI-8 C2M',
    0x90: 'EEI-100G-RTLR-1-S',
    0x91: 'EEI-100G-RTLR-1-L',
    0x92: 'EEI-200G-RTLR-2-S',
    0x93: 'EEI-200G-RTLR-2-L',
    0x94: 'EEI-400G-RTLR-4-S',
    0x95: 'EEI-400G-RTLR-4-L',
    0x96: 'EEI-800G-RTLR-8-S',
    0x97: 'EEI-800G-RTLR-8-L',
    0x98: 'EEI-200G-RTLR-1',
    0x99: 'EEI-400G-RTLR-2',
    0x9A: 'EEI-800G-RTLR-4',
    0x9B: 'EEI-1.6T-RTLR-8',
    0xA0: 'IB XDR',
    0xB0: 'FOIC1.1-MFI (ITU-T',
    0xB1: 'FOIC4.4-MFI (ITU-T',
    0xB2: 'FOIC8.8-MFI (ITU-T',
    0xB3: 'FOIC1e.1-MFI (ITU-T',
    0xB4: 'FOIC4e.4-MFI (ITU-T',
    0xB5: 'FOIC1o.1-MFI.',
    0xB6: 'FOIC4o.4-MFI',
    0xB7: 'ITU-T G.',
}

MEDIA_INTERFACE_IDS_MMF = {
    0x01: '10GBASE-SW',
    0x02: '10GBASE-SR',
    0x03: '25GBASE-SR',
    0x04: '40GBASE-SR4',
    0x05: '40GE SWDM4 MSA Spec',
    0x06: '40GE BiDi',
    0x07: '50GBASE-SR',
    0x08: '100GBASE-SR10',
    0x09: '100GBASE-SR4',
    0x0A: '100GE SWDM4 MSA Spec',
    0x0B: '100GE BiDi',
    0x0C: '100GBASE-SR2',
    0x0D: '100GBASE-SR1',
    0x0E: '200GBASE-SR4',
    0x0F: '400GBASE-SR16',
    0x10: '400GBASE-SR8',
    0x11: '400GBASE-SR4',
    0x12: '800GBASE-SR8',
    0x13: '8GFC-MM',
    0x14: '10GFC-MM',
    0x15: '16GFC-MM',
    0x16: '32GFC-MM',
    0x17: '64GFC-MM',
    0x18: '128GFC-MM4',
    0x19: '256GFC-MM4',
    0x1A: '400GBASE-SR4.2',
    0x1B: '200GBASE-SR2',
    0x1C: '128GFC-MM',
    0x1D: '100GBASE-VR1',
    0x1E: '200GBASE-VR2',
    0x1F: '400GBASE-VR4',
    0x20: '800GBASE-VR8',
    0x21: '800G-VR',
    0x22: '800G-SR',
    0x23: '1.6T-VR',
    0x24: '1.6T-SR',
}

MEDIA_INTERFACE_IDS_SMF = {
    0x01: '10GBASE-LW',
    0x02: '10GBASE-EW',
    0x03: '10G-ZW',
    0x04: '10GBASE-LR',
    0x05: '10GBASE-ER',
    0x06: '10G-ZR',
    0x07: '25GBASE-LR',
    0x08: '25GBASE-ER',
    0x09: '40GBASE-LR4',
    0x0A: '40GBASE-FR',
    0x0B: '50GBASE-FR',
    0x0C: '50GBASE-LR',
    0x0D: '100GBASE-LR4',
    0x0E: '100GBASE-ER4',
    0x0F: '100G PSM4 MSA Spec',
    0x10: '100G CWDM4 MSA Spec',
    0x11: '100G 4WDM-10 MSA Spec',
    0x12: '100G 4WDM-20 MSA Spec',
    0x13: '100G 4WDM-40 MSA Spec',
    0x14: '100GBASE-DR',
    0x15: '100G-FR MSA spec2/100GBASE-',
    0x16: '100G-LR MSA spec2/100GBASE-LR1',
    0x17: '200GBASE-DR4',
    0x18: '200GBASE-FR4',
    0x19: '200GBASE-LR4',
    0x1A: '400GBASE-FR8',
    0x1B: '400GBASE-LR8',
    0x1C: '400GBASE-DR4',
    0x1D: '400G-FR4 MSA spec2/400GBASE-',
    0x1E: '400G-LR4-10 MSA Spec2',
    0x1F: '8GFC-SM',
    0x20: '10GFC-SM',
    0x21: '16GFC-SM',
    0x22: '32GFC-SM',
    0x23: '64GFC-SM',
    0x24: '128GFC-PSM4',
    0x2C: '4I1-9D1F',
    0x2D: '4L1-9C1F',
    0x2E: '4L1-9D1F',
    0x2F: 'C4S1-9D1F',
    0x30: 'C4S1-4D1F',
    0x31: '4I1-4D1F',
    0x32: '8R1-4D1F',
    0x33: '8I1-4D1F',
    0x34: '100G CWDM4-OCP',
    0x35: 'ZR400-OFEC-16QAM-HA',
    0x36: 'ZR400-OFEC-16QAM-HB',
    0x37: 'ZR400-OFEC-8QAM-HA',
    0x38: '10G-SR',
    0x39: '10G-LR',
    0x3A: '25G-SR',
    0x3B: '25G-LR',
    0x3C: '10G-LR-BiDi',
    0x3D: '25G-LR-BiDi',
    0x3E: '400ZR (0x01, 0x03), DWDM,',
    0x3F: '400ZR (0x02), Single Wavelength,',
    0x40: '50GBASE-ER',
    0x41: '200GBASE-ER4',
    0x42: '400GBASE-ER8',
    0x43: '400GBASE-LR4-6',
    0x44: '100GBASE-ZR',
    0x45: '128GFC-SM',
    0x46: 'ZR400-OFEC-16QAM',
    0x47: 'ZR300-OFEC-8QAM',
    0x48: 'ZR200-OFEC-QPSK',
    0x49: 'ZR100-OFEC-QPSK',
    0x4A: '100G-LR1-20 MSA Spec2',
    0x4B: '100G-ER1-30 MSA Spec2',
    0x4C: '100G-ER1-40 MSA Spec2',
    0x4D: '400GBASE-ZR',
    0x4E: '10GBASE-BR (Clause 158)1',
    0x4F: '25GBASE-BR (Clause 159)1',
    0x50: '50GBASE-BR (Clause 160)1',
    0x52: 'FOIC2.8-DO (G.709.3/Y.1331.3)3 252.557871 1',
    0x53: 'FOIC4.8-DO (G.709.3/Y.1331.3)3 505.115743 1',
    0x54: 'FOIC2.4-DO (G.709.3/Y.1331.3)3 252.557871 1',
    0x55: '400GBASE-DR4-2',
    0x56: '800GBASE-DR8',
    0x57: '800GBASE-DR8-2',
    0x58: 'ZR400-OFEC-8QAM-HB',
    0x59: 'ZR300-OFEC-8QAM-HA',
    0x5A: 'ZR300-OFEC-8QAM-HB',
    0x5B: 'ZR200-OFEC-QPSK-HA',
    0x5C: 'ZR200-OFEC-QPSK-HB',
    0x5D: 'ZR100-OFEC-QPSK-HA',
    0x5E: 'ZR100-OFEC-QPSK-HB',
    0x5F: 'FLEXO-4-DO-16QAM/FOIC4.8-DO 505.115743 1',
    0x60: 'FLEXO-3-DO-8QAM/FOIC3.6-DO 378.836807 1',
    0x61: 'FLEXO-2-DO-QPSK/FOIC2.4-DO',
    0x62: 'FLEXO-2-DO-16QAM/FOIC2.8-DO 252.557871 1',
    0x63: 'FLEXO-1-DO-QPSK/FOIC1.4-DO',
    0x64: 'FLEXO-4e-DO-QPSK/FOIC4e.4-DO 472.8134024 1',
    0x65: '03',
    0x66: '12',
    0x67: '14',
    0x68: 'FLEXO-4-DO-QPSK/FOIC4.4-DO',
    0x69: '03',
    0x6A: '56',
    0x6B: '14',
    0x6C: '800ZR-A (0x01), 150 GHz DWDM, 945.626,804, 1',
    0x6D: '800ZR-B (0x02), 150 GHz DWDM, 945.626,804, 1',
    0x6E: '800ZR-C (0x03), 150 GHz DWDM, 945.626,804, 1',
    0x6F: '400G-ER4-30 MSA Spec2',
    0x70: '1I1-5D1F',
    0x71: '1R1-5D1F',
    0x72: 'FOIC1.1-RS (G.709.1/Y.1331.58)3 126.278935 1',
    0x73: '200GBASE-DR1',
    0x74: '200GBASE-DR1-2',
    0x75: '400GBASE-DR2',
    0x76: '400GBASE-DR2-2',
    0x77: '800GBASE-DR4',
    0x78: '800GBASE-DR4-2',
    0x79: '800GBASE-FR4-500',
    0x7A: '800GBASE-FR4',
    0x7B: '800GBASE-LR4',
    0x7C: '800GBASE-LR1',
    0x7D: '800GBASE-ER1-20',
    0x7E: '800GBASE-ER1',
    0x7F: '1.6TBASE-DR8',
    0x80: '1.6TBASE-DR8-2',
    0x81: 'XR400-16QAM',
    0x82: 'XR300-8QAM',
    0x83: 'XR200-QPSK',
    0x84: 'XR200-16QAM',
    0x85: 'XR100-QPSK',
    0x86: 'XR100-16QAM',
    0x87: 'XR400-WS-16QAM',
    0x88: 'XR200-WS-QPSK',
    0x89: 'XR200-WS-16QAM',
    0x8A: 'XR100-WS-QPSK',
    0x8B: 'XR100-WS-16QAM',
    0x8C: 'XR200-WS-BIDI-16QAM',
    0x8D: 'XR100-WS-BIDI-QPSK',
    0x8E: 'XR100-WS-BIDI-16QAM',
    0x8F: '100G-DR1-LPO',
    0x90: '200G-DR2-LPO',
    0x91: '400G-DR4-LPO',
    0x92: '800G-DR8-LPO',
    0x93: '400G-FR4-LPO',
    0xBF: 'Passive Loopback module',
}


# Table 4-13. Anything above 3 is reserved.
HEATSINK_TYPES = {
    0: 'Unknown or unspecified',
    1: 'RHS — Riding Heatsink',
    2: 'IHS — Integrated Heatsink, Open Top',
    3: 'IHS — Integrated Heatsink, Closed Top',
}

# Table 4-12.
FIBER_FACE_TYPES = {
    0: 'Unknown or unspecified',
    1: 'PC/UPC (Physical/Ultra Physical contact)',
    2: 'APC (Angled Physical Contact)',
}


# Table 8-22: "BEh: the GID of the Host Interface UID is non-zero and hence
# the corresponding NAD must be consulted", and the same for the media side.
# It is a real encoding, not a gap in the tables below.
NAD_INTERFACE_ID = 0xBE


def host_interface_name(code: int) -> str:
    """SFF-8024 name for a Host Electrical Interface ID, or a marked unknown.

    Unknown codes are reported as such rather than blanked: a module using a
    code newer than this table is a fact worth seeing, and an empty cell reads
    like the module said nothing.

    BEh is not one of those. Table 8-22 defines it: the Interface GID is
    non-zero, so the name is not in any SFF-8024 ID table and the Normalized
    Application Descriptor has to be read instead. Calling that Unknown hid a
    fact the tool already reports on the same panel - how many banks of NADs
    the module advertises.
    """
    if code == NAD_INTERFACE_ID:
        return 'See NAD (0xBE)'
    return HOST_INTERFACE_IDS.get(code, f'Unknown (0x{code:02X})')


def media_interface_name(code: int, media_type: int = 0x02) -> str:
    """SFF-8024 name for a Media Interface ID.

    Which table applies depends on the module's global Media Type - the same
    code means different things on MMF and SMF - so the caller has to say
    which, and 0x01 is the MMF encoding.

    BEh means the same here as on the host side: the GID is non-zero and the
    Normalized Application Descriptor is where the name lives.
    """
    if code == NAD_INTERFACE_ID:
        return 'See NAD (0xBE)'
    table = MEDIA_INTERFACE_IDS_MMF if media_type == 0x01 else MEDIA_INTERFACE_IDS_SMF
    return table.get(code, f'Unknown (0x{code:02X})')
