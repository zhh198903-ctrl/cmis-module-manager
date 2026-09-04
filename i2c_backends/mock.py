"""Mock/simulation I2C backends for seven QSFP-DD / OSFP-XD module types.

All register addresses match the CMIS 5.4 spec (OIF-CMIS-05.4.pdf).
Profile-driven design: a single MockBackend base class reads `self.PROFILE`
(class attribute) to customize vendor info, capabilities, optical parameters,
and application descriptors. Seven subclasses register different profiles:

  - mock_coherent        : 800GBASE-LR1 coherent lite (IEEE P802.3dj Clause 185)
  - mock_coherent_zr     : 800G Coherent tunable (C-band DWDM, ZR-class)
  - mock_dr8             : 800GBASE-DR8 (8×100G PAM4, SMF 500m, EML 1310nm)
  - mock_sr8             : 800GBASE-SR8 (8×100G PAM4, OM4 100m, VCSEL 850nm)
  - mock_fr4x2           : 2× 400GBASE-FR4 (CWDM4, SMF 2km, EML 1310nm)
  - mock_1600g_dr8       : 1.6TBASE-DR8 (IEEE P802.3dj Clause 180)
  - mock_1600g_16lane    : 1.6T over 16 host lanes (1.6TAUI-16 C2M)

Dynamic behavior (state machine, ApplyDataPath, Reset, LowPwr, TxDisable,
PRBS LOL, BER/SNR, counters, laser tuning) is shared across all profiles.
"""
import math
import struct
import time

from i2c_interface import I2CInterface, register_backend
import cmis_registers as cmis


def _dbm_to_raw(dbm):
    """dBm -> the 16-bit optical power register encoding (units of 0.1 uW)."""
    raw = int(round(10 ** (dbm / 10.0) * 10000))
    # The register is 16 bits, so anything above about +8.2 dBm has no
    # encoding. Truncating would turn a high alarm into a plausible-looking
    # low one, so refuse instead of storing a lie.
    if not 0 <= raw <= 0xFFFF:
        raise ValueError('%.2f dBm does not fit the 16-bit power register' % dbm)
    return raw


def _raw_to_dbm_centi(raw):
    """The same encoding -> hundredths of a dBm, as Page 62h states it."""
    return int(round(10 * math.log10(raw / 10000.0) * 100)) if raw else -32768


# ============================================================================
# Module Profile Definitions
# Each profile is a dict of every field that differs between module types.
# ============================================================================

# The datacenter "coherent lite" PMD of IEEE P802.3dj/D3.1: single wavelength,
# O-band, 10 km. Everything optical below is Clause 185:
#
#   Table 185-4  operating range 2 m to 10 km
#   Table 185-5  123.6364 GBd DP-16QAM, carrier 228.675 THz +/- 20 GHz
#                (~1311 nm), launch power -11.2 to -6 dBm
#   Table 185-6  average receive power tolerance -17.5 to -4 dBm
#   185.2        BLER 1.45e-11 at the Inner FEC with BERadded 6.4e-5
#
# The carrier frequency is a fixed point with a tolerance, not a tuning range:
# LR1 has no grid and no tunable laser, which is most of what separates
# coherent lite from a ZR module. The tunable C-band profile this one used to
# be still exists below as _ZR_800G.
_COHERENT_800G = {
    'display':         '800GBASE-LR1 coherent lite (DP-16QAM, SMF 10km, 802.3dj)',
    'vendor_name':     b"OPENCMIS DEMO   ",
    'vendor_pn':       b"DEMO-LR1-800GQDD",
    'vendor_sn':       b"DEMO000000001   ",
    'vendor_rev':      b"A1",
    'vendor_oui':      (0x00, 0x00, 0x00),     # Unprogrammed OUI - simulated module
    'date_code':       b"26010100",
    'clei':            b"DEMOCLEI00",
    'media_type':          0x02,             # SMF
    'connector_type':      0x07,             # LC: one fibre pair, one wavelength
    'media_if_tech':       0x04,             # 1310 nm DFB - O-band, fixed carrier
    'power_class_bits':    0x80,             # Class 5
    'max_power_0_25w':     0x34,             # 52 x 0.25 = 13.0 W (demo: dj says nothing about module power)
    'tunable':             False,
    # Per-lane nominal optical values, both inside their Clause 185 windows
    'tx_power_uw_nom':     158,              # -8.0 dBm
    'rx_power_uw_nom':     63,               # -12.0 dBm
    'tx_bias_ma_nom':      60.0,             # MZM / coherent driver
    'temperature_c_nom':   62.0,             # DSP+TEC
    # Table 174A-1 covers PHYs with an Inner FEC and allocates the PMD-to-PMD
    # link 2.28e-4 of the path's 2.921e-4, measured after Inner FEC decoding.
    # The raw line-side ratio before that FEC is far larger and is not what
    # this table budgets, so it is not what this models.
    'base_ber':            1.2e-4,
    'snr_db_nom':          18.0,             # demo: dj sets no optical SNR limit
    # Application Descriptors: (HostIfID, MediaIfID, LaneCount[7:4]H[3:0]M, HostLaneAssignMask)
    # One wavelength means one media lane, whichever host width drives it.
    'app_descriptors': [
        (0x51, 0x7C, 0x81, 0x01),            # AppSel 1: 800GAUI-8 S C2M -> 800GBASE-LR1 (8H/1M)
        (0x82, 0x7C, 0x41, 0x01),            # AppSel 2: 800GAUI-4 C2M -> 800GBASE-LR1 (4H/1M)
    ],
    # Table 185-4: 2 m to 10 km. Multiplier 01b counts 1 km per step.
    'link_lengths': {'smf_len_byte': 0x4A},  # 01b << 6 | 10 = 10 km
    'power_thresholds_dbm': {
        # Alarms are the Clause 185 operating limits; the warnings half a dB
        # inside them are a demo choice, as 802.3dj has no warning level.
        'tx': (-6.0, -11.2, -6.5, -10.7),
        'rx': (-4.0, -17.5, -4.5, -17.0),
    },
}

# The C-band tunable coherent module the profile above used to be. Not an
# 802.3dj PMD - ZR-class optics are specified by OIF - but it is the only
# profile with a tunable laser, so Pages 04h and 12h would otherwise have
# nothing to demonstrate.
_ZR_800G = {
    'display':         '800G Coherent tunable (C-band DWDM, ZR-class)',
    'vendor_name':     b"OPENCMIS DEMO   ",
    'vendor_pn':       b"DEMO-DP800G-QDD ",
    'vendor_sn':       b"DEMO000000007   ",
    'vendor_rev':      b"A1",
    'vendor_oui':      (0x00, 0x00, 0x00),
    'date_code':       b"24010100",
    'clei':            b"DEMOCLEI07",
    'media_type':          0x02,             # SMF
    'connector_type':      0x07,             # LC
    'media_if_tech':       0x10,             # C-band tunable laser
    'power_class_bits':    0x80,             # Class 5
    'max_power_0_25w':     0x40,             # 64 x 0.25 = 16.0 W
    'tunable':             True,
    'tx_power_uw_nom':     1000,             # 0 dBm
    'rx_power_uw_nom':     158,              # -8 dBm
    'tx_bias_ma_nom':      60.0,
    'temperature_c_nom':   62.0,
    'base_ber':            1.0e-9,
    'snr_db_nom':          18.0,
    'app_descriptors': [
        (0x51, 0x48, 0x81, 0x01),            # AppSel 1: 800GAUI-8 S C2M -> ZR200-OFEC-QPSK (8H/1M)
        (0x4F, 0x41, 0x41, 0x01),            # AppSel 2: 400GAUI-4-S C2M -> 200GBASE-ER4 (4H/1M)
    ],
    'link_lengths': {},                      # no cable length advertised
}

_DR8_800G = {
    'display':         '800GBASE-DR8 (SMF 500m, EML 1310nm)',
    'vendor_name':     b"OPENCMIS DEMO   ",
    'vendor_pn':       b"DEMO-DR8-800GQDD",
    'vendor_sn':       b"DEMO000000002   ",
    'vendor_rev':      b"B1",
    'vendor_oui':      (0x00, 0x00, 0x00),     # Unprogrammed OUI - simulated module
    'date_code':       b"24010200",
    'clei':            b"DEMOCLEI00",
    'media_type':          0x02,             # SMF
    'connector_type':      0x28,             # MPO 1×16 (16 fibers, 8 pairs)
    'media_if_tech':       0x06,             # 1310 nm EML
    'power_class_bits':    0xC0,             # Class 7 (110b << 5)
    'max_power_0_25w':     0x38,             # 56 × 0.25 = 14.0 W
    'tunable':             False,
    'tx_power_uw_nom':     1260,             # +1 dBm
    'rx_power_uw_nom':     500,              # -3.0 dBm: -1 dBm sat on the
                                            # generic high warning
    'tx_bias_ma_nom':      70.0,             # EML driver
    'temperature_c_nom':   55.0,
    'base_ber':            5.0e-6,           # KP4 FEC operating
    'snr_db_nom':          22.0,
    'app_descriptors': [
        (0x51, 0x56, 0x88, 0x01),            # AppSel 1: 800GAUI-8 → 800GBASE-DR8 (8H/8M)
        (0x4F, 0x1C, 0x44, 0x11),            # AppSel 2: 400GAUI-4 → 400GBASE-DR4 (4H/4M)
    ],
    'link_lengths': {'smf_len_byte': 0x05},   # 5 × 0.1 km = 500 m
}

# ---------------------------------------------------------------------------
# 1.6T profiles
# ---------------------------------------------------------------------------
# Two shapes reach 1.6 Tb/s and they exercise different code paths here: eight
# lanes at 200G fits one bank, sixteen lanes at 100G needs two. Both advertise
# CMIS 5.4, because the 256-lane escape and the optional pages are what a module
# this wide has reason to use.
#
# Interface ID codes are the real ones, checked against SFF-8024 Rev 4.14.
# The optical and error-ratio numbers of the eight-lane profile come from
# IEEE P802.3dj/D3.1 (4 June 2026), which is where 1.6 Tb/s Ethernet is defined:
#
#   Clause 180   200GBASE-DR1 / 400GBASE-DR2 / 800GBASE-DR4 / 1.6TBASE-DR8
#   Table 180-6  operating range 2 m to 500 m
#   Table 180-7  106.25 GBd PAM4 per lane, 1304.5-1317.5 nm, launch power
#                per lane -3.1 to +4 dBm
#   Table 180-8  average receive power per lane -6.1 to +4 dBm
#   174A.6       pre-FEC BER (BERtotal) for a 1.6TBASE-R PHY must stay under
#                2.921e-4, the ratio the RS-FEC can still clean up
#   Table 174A-1 how that 2.921e-4 is divided: 2.28e-4 to the PMD-to-PMD link
#                and 0.08e-4 / 0.24e-4 to each AUI either side of it
#
# Careful with the 6.4e-5 that 180.2 names: BERadded is the budget for every
# OTHER link in the path (174A.9), the noise a PMD test injects to stand in for
# them - not the PMD's own share. Reading it as the PMD's allocation understates
# a conformant 1.6T module by a factor of three and makes it look broken.
#
# The spec name is 1.6TBASE-DR8, not "1600GBASE-DR8" -- 802.3dj spells every
# 1.6 Tb/s PHY type with the 1.6T prefix, and SFF-8024 follows it.
#
# What 802.3dj does NOT specify is anything about the module as a package:
# power class, case temperature and the CMIS SNR diagnostic are demo values
# chosen to look plausible, not limits read out of a spec.
#
# The sixteen-lane profile's 802.3dj anchor is its host interface only:
# 1.6TAUI-16 C2M lives in Annex 120G, the 100 Gb/s per lane C2M annex. Its
# optics run at 100G per lane, so they are 802.3df PMDs rather than Clause 180
# ones, and its thresholds stay at the generic mock values. It also cannot
# advertise 0x55 1.6TAUI-16-S C2M as an Application, wide as it is: CMIS 5.4
# caps one Application at eight lanes, so a 16-lane host interface can only
# appear as two eight-lane instances.
_DR8_1600G = {
    'display':         '1.6TBASE-DR8 (8 × 106.25 GBd PAM4, SMF 500m, 802.3dj)',
    'vendor_name':     b"OPENCMIS DEMO   ",
    'vendor_pn':       b"DEMO-1600G-DR8  ",
    'vendor_sn':       b"DEMO000000005   ",
    'vendor_rev':      b"A0",
    'vendor_oui':      (0x00, 0x00, 0x00),
    'date_code':       b"26010100",
    'clei':            b"DEMOCLEI16",
    'media_type':          0x02,             # SMF
    'connector_type':      0x28,             # MPO 1×16
    'media_if_tech':       0x06,             # 1310 nm EML
    'power_class_bits':    0xE0,             # Class 8 (111b << 5)
    'max_power_0_25w':     0x68,             # 104 × 0.25 = 26.0 W
    'tunable':             False,
    'tx_power_uw_nom':     1585,             # +2.0 dBm, inside Table 180-7
    'rx_power_uw_nom':     631,              # -2.0 dBm, inside Table 180-8
    'tx_bias_ma_nom':      85.0,
    'temperature_c_nom':   64.0,             # 1.6T optics run hot
    # 200G/lane PAM4 leans on much stronger FEC than 100G/lane did, so a
    # healthy pre-FEC BER here is orders of magnitude worse than on an 800G
    # module and must not be read as a fault. Table 174A-1 allocates 2.28e-4
    # of the path's 2.921e-4 budget (174A.6) to the PMD-to-PMD link itself,
    # so this sits comfortably inside a conformant module's own share.
    'base_ber':            1.5e-4,
    'snr_db_nom':          17.5,             # demo value: 802.3dj has no optical SNR limit
    'app_descriptors': [
        (0x83, 0x7F, 0x88, 0x01),            # AppSel 1: 1.6TAUI-8 C2M → 1.6TBASE-DR8 (8H/8M)
        # Half the optic, same 200G lanes: 800GBASE-DR4 sits in the very same
        # Clause 180 table, so a DR8 can break out into two of them. The old
        # second Application here was 800GBASE-DR8, which would have meant
        # 100G media lanes on a module whose lasers only run at 200G.
        (0x82, 0x77, 0x44, 0x11),            # AppSel 2: 800GAUI-4 C2M → 800GBASE-DR4 (4H/4M)
    ],
    # Table 180-6: 2 m to 500 m. Byte 132 is 0.1 km per count under multiplier 00b.
    'link_lengths': {'smf_len_byte': 0x05},   # 0.5 km
    # Alarm levels are the operating limits of Table 180-7 (launch) and
    # Table 180-8 (receive), per lane. The WARNING levels are not in either
    # table - 802.3dj has no such concept - so they are set half a dB inside
    # the alarms, which is a demo choice and not a spec limit.
    # Order: hi alarm, lo alarm, hi warning, lo warning, in dBm.
    'power_thresholds_dbm': {
        'tx': (4.0, -3.1, 3.5, -2.6),
        'rx': (4.0, -6.1, 3.5, -5.6),
    },
    'cmis_rev':            0x54,
    'lanes':               8,
    'default_polarity_tx': 0x00,
    'default_polarity_rx': 0x00,
    # Bit 7 is Page 0Ch. Bit 6 would claim Page 0Dh (firmware management),
    # which this mock does not serve, so it stays clear.
    'pages_ext_173':       0b10000000,       # Page 0Ch
    'pages_ext_174':       0b11100000,       # Pages 60h, 61h, 62h
    'misc_caps_252':       0b00100000,       # MediaLaneSwitchingSupported
    'module_subtype':      0x01,
    'heatsink_fiber':      0x30,
}

_XD16_1600G = {
    'display':         '1.6T 16×100G host (1.6TAUI-16 C2M, two banks)',
    'vendor_name':     b"OPENCMIS DEMO   ",
    'vendor_pn':       b"DEMO-1600G-XD16 ",
    'vendor_sn':       b"DEMO000000006   ",
    'vendor_rev':      b"A0",
    'vendor_oui':      (0x00, 0x00, 0x00),
    'date_code':       b"26010100",
    'clei':            b"DEMOCLEI17",
    'media_type':          0x02,             # SMF
    'connector_type':      0x28,             # MPO
    'media_if_tech':       0x06,             # 1310 nm EML
    'power_class_bits':    0xE0,             # Class 8
    'max_power_0_25w':     0x70,             # 112 × 0.25 = 28.0 W
    'tunable':             False,
    'tx_power_uw_nom':     1259,             # +1.0 dBm per lane
    'rx_power_uw_nom':     500,              # -3.0 dBm per lane, clear of the warning
    'tx_bias_ma_nom':      72.0,
    'temperature_c_nom':   66.0,
    'base_ber':            8.0e-6,           # 100G/lane PAM4, KP4 territory
    'snr_db_nom':          20.5,
    'app_descriptors': [
        # An Application is capped at eight lanes (5.4 section 6.4.1), so a
        # 16-lane module does not advertise a 16-lane Application: it
        # advertises ones that fit in a lane group and instantiates them per
        # group. HostLaneAssignmentOptions is a bitmap of permissible starting
        # lanes within the group, hence 0x11 for lanes 1 and 5.
        (0x51, 0x56, 0x88, 0x01),            # AppSel 1: 800GAUI-8 S C2M → 800GBASE-DR8 (8H/8M)
        (0x4F, 0x1C, 0x44, 0x11),            # AppSel 2: 400GAUI-4-S C2M → 400GBASE-DR4 (4H/4M)
    ],
    'link_lengths': {'smf_len_byte': 0x05},   # 0.5 km, the DR reach of its optics
    'cmis_rev':            0x54,
    'lanes':               16,               # two banks; 01h:142.1-0 = 01b
    'default_polarity_tx': 0b00000101,       # lanes 1 and 3 wired inverted
    'default_polarity_rx': 0b00000010,       # lane 2 wired inverted
    'pages_ext_173':       0b10000000,       # Page 0Ch only, as above
    'pages_ext_174':       0b11100000,
    'misc_caps_252':       0b00100000,
    'module_subtype':      0x01,
    'heatsink_fiber':      0x30,
}

_SR8_800G = {
    'display':         '800GBASE-SR8 (OM4 100m, VCSEL 850nm)',
    'vendor_name':     b"OPENCMIS DEMO   ",
    'vendor_pn':       b"DEMO-SR8-800GQDD",
    'vendor_sn':       b"DEMO000000003   ",
    'vendor_rev':      b"C1",
    'vendor_oui':      (0x00, 0x00, 0x00),     # Unprogrammed OUI - simulated module
    'date_code':       b"24010300",
    'clei':            b"DEMOCLEI00",
    'media_type':          0x01,             # MMF
    'connector_type':      0x28,             # MPO 1×16
    'media_if_tech':       0x00,             # 850 nm VCSEL
    'power_class_bits':    0xC0,             # Class 7
    'max_power_0_25w':     0x30,             # 48 × 0.25 = 12.0 W
    'tunable':             False,
    'tx_power_uw_nom':     1000,             # 0 dBm
    'rx_power_uw_nom':     631,              # -2 dBm
    'tx_bias_ma_nom':      8.0,              # VCSEL bias
    'temperature_c_nom':   50.0,
    'base_ber':            1.0e-5,
    'snr_db_nom':          19.0,
    'app_descriptors': [
        (0x51, 0x12, 0x88, 0x01),            # AppSel 1: 800GAUI-8 S C2M → 800GBASE-SR8 (8H/8M)
        # 400GBASE-SR8 is eight media lanes of 50G, which this 4H/4M breakout
        # cannot be. Four of the module's own 100G lanes is 400GBASE-SR4.
        (0x4F, 0x11, 0x44, 0x11),            # AppSel 2: 400GAUI-4-S C2M → 400GBASE-SR4 (4H/4M)
    ],
    'link_lengths': {'om4_len_byte': 0x32},    # 50 × 2 m = 100 m
    # An 850 nm VCSEL runs near 8 mA; the generic EML window starts at 10 mA,
    # so every lane of this module used to sit under its own low alarm.
    # Demo values: no standard sets a module's bias limits.
    'bias_thresholds_ma': (14.0, 4.0, 12.0, 5.0),
}

_FR4X2_800G = {
    'display':         '2× 400GBASE-FR4 (SMF 2km, CWDM4 EML)',
    'vendor_name':     b"OPENCMIS DEMO   ",
    'vendor_pn':       b"DEMO-FR4X2-800G ",
    'vendor_sn':       b"DEMO000000004   ",
    'vendor_rev':      b"A2",
    'vendor_oui':      (0x00, 0x00, 0x00),     # Unprogrammed OUI - simulated module
    'date_code':       b"24010400",
    'clei':            b"DEMOCLEI00",
    'media_type':          0x02,             # SMF
    'connector_type':      0x07,             # LC (dual LC for 2× 400G)
    'media_if_tech':       0x06,             # 1310 nm EML
    'power_class_bits':    0xC0,             # Class 7
    'max_power_0_25w':     0x3C,             # 60 × 0.25 = 15.0 W
    'tunable':             False,
    'tx_power_uw_nom':     1260,             # +1 dBm
    'rx_power_uw_nom':     500,              # -3.0 dBm: -1 dBm sat on the
                                            # generic high warning
    'tx_bias_ma_nom':      70.0,
    'temperature_c_nom':   58.0,
    'base_ber':            5.0e-6,
    'snr_db_nom':          22.0,
    'app_descriptors': [
        # Two 400GBASE-FR4 apps: first on host lanes 1-4, second on host lanes 5-8
        (0x4F, 0x1D, 0x44, 0x01),            # AppSel 1: 400GAUI-4 → 400G-FR4 (4H/4M), host lane 1
        (0x4F, 0x1D, 0x44, 0x10),            # AppSel 2: 400GAUI-4 → 400G-FR4 (4H/4M), host lane 5
    ],
    'link_lengths': {'smf_len_byte': 0x14},   # 20 × 0.1 km = 2 km
}


# ============================================================================
# Base Mock Backend (profile-driven)
# ============================================================================

class MockBackend(I2CInterface):
    """Profile-driven CMIS 5.3 optical module simulator.

    Subclasses set `PROFILE = <profile dict>` at class level. The default
    PROFILE is 800G coherent tunable, but this class is NOT registered directly.
    Subclasses below register 4 specific profiles.
    """

    PROFILE = _COHERENT_800G  # default (overridden by subclasses)

    def __init__(self):
        self._profile = self.PROFILE
        self._connected = False
        self._current_page = 0x00
        self._current_bank = 0x00
        self._start_time = time.time()
        # State machine tracking
        self._module_state = 0b011      # ModuleReady
        self._reset_time = 0.0
        self._lp_request_time = 0.0
        self._apply_time = 0.0
        self._dp_lane_states = [0x4] * 8  # all Activated
        self._tx_disable_mask = 0x00
        self._prbs_enable_times = {'hg': 0, 'mg': 0, 'hc': 0, 'mc': 0}
        self._error_counts = [0] * 8
        self._bit_counts = [0] * 8
        self._last_counter_time = 0.0
        self._registers = self._build_initial_registers()

    @classmethod
    def probe_availability(cls) -> dict:
        return {
            'available': True,
            'description': f'Mock simulation: {cls.PROFILE["display"]}',
        }

    # ------------------------------------------------------------------
    def _build_initial_registers(self) -> dict:
        p = self._profile
        regs = {}

        # ==== Lower Memory ====
        lower = {}
        lower[0x00] = 0x1E                  # QSFP-DD CMIS
        lower[0x01] = p.get('cmis_rev', 0x53)   # 0x54 = CMIS 5.4
        lower[0x02] = 0x00                  # MemoryModel: paged
        lower[0x03] = (0b011 << 1)          # ModuleReady, interrupt deasserted
        for a in range(0x04, 0x0E): lower[a] = 0x00
        # Temperature
        temp_raw = int(p['temperature_c_nom'] * 256) & 0xFFFF
        lower[0x0E] = (temp_raw >> 8) & 0xFF
        lower[0x0F] = temp_raw & 0xFF
        # Voltage 3.3 V
        lower[0x10] = 0x80; lower[0x11] = 0xE8
        # Aux monitors (generic)
        lower[0x12] = 0x0C; lower[0x13] = 0xCD   # Aux1
        lower[0x14] = 0x37; lower[0x15] = 0x00   # Aux2
        lower[0x16] = 0x80; lower[0x17] = 0xE8   # Aux3
        lower[0x1A] = 0x40                  # ModuleControl: AllowLPHW=1
        lower[0x27] = 2; lower[0x28] = 5    # Active FW 2.5
        lower[0x55] = p['media_type']       # Media Type

        # Application Descriptors (lower 0x56-0x75)
        appdesc = list(p['app_descriptors']) + [(0xFF, 0, 0, 0)] * 8
        for i, (h, m, lc, hla) in enumerate(appdesc[:8]):
            base = 0x56 + i * 4
            lower[base] = h
            lower[base + 1] = m
            lower[base + 2] = lc
            lower[base + 3] = hla

        lower[0x7F] = 0x00                  # Page select
        regs[None] = lower

        # ==== Page 00h — Administrative Information ====
        p00 = {}
        p00[0x80] = 0x1E                    # Identifier copy
        # Vendor Name 0x81-0x90 (16 bytes)
        for i, b in enumerate(p['vendor_name'][:16]):
            p00[0x81 + i] = b
        # Vendor OUI 0x91-0x93 (3 bytes)
        p00[0x91] = p['vendor_oui'][0]
        p00[0x92] = p['vendor_oui'][1]
        p00[0x93] = p['vendor_oui'][2]
        # Vendor PN 0x94-0xA3 (16 bytes)
        for i, b in enumerate(p['vendor_pn'][:16]):
            p00[0x94 + i] = b
        # Vendor Rev 0xA4-0xA5 (2 bytes)
        for i, b in enumerate(p['vendor_rev'][:2]):
            p00[0xA4 + i] = b
        # Vendor SN 0xA6-0xB5 (16 bytes)
        for i, b in enumerate(p['vendor_sn'][:16]):
            p00[0xA6 + i] = b
        # Date Code 0xB6-0xBD (8 bytes)
        for i, b in enumerate(p['date_code'][:8]):
            p00[0xB6 + i] = b
        # CLEI 0xBE-0xC7 (10 bytes)
        for i, b in enumerate(p['clei'][:10]):
            p00[0xBE + i] = b
        # Power Class & Max Power
        p00[0xC8] = p['power_class_bits']
        p00[0xC9] = p['max_power_0_25w']
        p00[0xCA] = 0x00                    # Cable length = 0 (transceiver)
        p00[0xCB] = p['connector_type']
        for a in range(0xCC, 0xD2): p00[a] = 0x00   # Cu attenuation = 0
        p00[0xD2] = 0x00                    # MediaLaneInformation
        p00[0xD3] = 0x00                    # FarEndConfig
        p00[0xD4] = p['media_if_tech']      # Media Interface Technology
        lower[0x3C] = p.get('module_subtype', 0x00)          # 60
        lower[0x3D] = p.get('heatsink_fiber', 0x00)          # 61 (5.4 heatsink type)
        regs[0x00] = p00

        # ==== Page 01h — Advertising ====
        p01 = {}
        p01[0x80] = 1; p01[0x81] = 0        # Inactive FW 1.0
        p01[0x82] = 1; p01[0x83] = 2        # HW Rev 1.2
        # Link lengths (profile-dependent). The keys say "len" rather than a
        # unit on purpose: these are the encoded bytes, and byte 132 counts
        # 0.1 km per step while 133-135 count 2 m - reading them as km and m
        # is what produced the reaches this code used to advertise.
        ll = p['link_lengths']
        p01[0x84] = ll.get('smf_len_byte', 0x00)
        p01[0x85] = ll.get('om5_len_byte', 0x00)
        p01[0x86] = ll.get('om4_len_byte', 0x00)
        p01[0x87] = ll.get('om3_len_byte', 0x00)
        p01[0x88] = ll.get('om2_len_byte', 0x00)
        # BanksSupported: 00b/01b/10b are 8/16/32 lanes; 11b is the CMIS 5.4
        # escape that sends the host to 01h:174 for the real count.
        lanes = p.get('lanes', 8)
        if lanes % 8:
            raise ValueError('lane counts come in groups of eight; %d does not'
                             % lanes)

        # The monitor registers are 16 bits. A nominal that does not fit used
        # to wrap silently and report a smaller, entirely plausible number -
        # 200 mA of bias came back as 68.9 - so a profile that cannot be
        # represented is refused here rather than misreported forever. The
        # margin covers the few percent the readings swing by.
        for key, scale, unit in (('tx_power_uw_nom', 10, 'uW'),
                                 ('rx_power_uw_nom', 10, 'uW'),
                                 ('tx_bias_ma_nom', 1 / 0.002, 'mA')):
            nominal = p.get(key, 0)
            if nominal * scale * 1.05 > 0xFFFF:
                raise ValueError(
                    '%s = %g %s does not fit its 16-bit register (max about '
                    '%g %s)' % (key, nominal, unit, 0xFFFF / scale / 1.05, unit))
        if lanes == 8:
            p01[0x8E] = 0x00
        elif lanes == 16:
            p01[0x8E] = 0x01
        elif lanes == 32:
            p01[0x8E] = 0x02
        else:
            # The legacy field encodes one, two or four banks and nothing else,
            # so a 24-lane module has no legacy spelling: rounding it up to 32
            # advertises eight lanes that do not exist. The 5.4 escape states
            # the count exactly, which is what it is for.
            p01[0x8E] = 0x03
            p01[0xAE] = (lanes // 8 - 1) & 0x1F
        # MediaLaneAssignmentOptions (01h:176-183, Table 8-60) is stored apart
        # from the first four descriptor bytes and is required on a paged
        # module. An Application that uses m of the eight media lanes can start
        # on every m-th one, which is what a breakout Application needs the
        # host to know.
        for i, desc in enumerate(p['app_descriptors'][:8]):
            media_lanes = desc[2] & 0x0F
            if media_lanes:
                p01[0xB0 + i] = sum(1 << (k * media_lanes)
                                    for k in range(8 // media_lanes))

        if p.get('cmis_rev', 0x53) >= 0x54:
            p01[0xAB] = p.get('default_polarity_tx', 0x00)   # 171 (5.4)
            p01[0xAC] = p.get('default_polarity_rx', 0x00)   # 172 (5.4)
            p01[0xAD] = p.get('pages_ext_173', 0x00)         # 173 (5.4)
            p01[0xAE] = p01.get(0xAE, 0) | p.get('pages_ext_174', 0x00)
            p01[0xFC] = p.get('misc_caps_252', 0x00)         # 252 (5.4)
        regs[0x01] = p01

        # ==== Page 02h — Thresholds ====
        # Optical power limits come from the profile when it knows the PMD it is
        # modelling, so a module built to a standard alarms where that standard
        # says it should. The generic values below stand in otherwise.
        thr = p.get('power_thresholds_dbm', {})
        # Bias limits belong to the laser, not to a PMD standard - a VCSEL runs
        # at a fraction of an EML's current, so one generic quad cannot serve
        # both. A profile whose nominal sits outside them alarms on connect.
        bias = p.get('bias_thresholds_ma')
        bias_thr = ([int(round(v / 0.002)) for v in bias] if bias
                    else [0xEA60, 0x1388, 0xC350, 0x2710])
        tx_thr = [_dbm_to_raw(v) for v in thr['tx']] if 'tx' in thr else             [0x7B84, 0x062C, 0x6220, 0x09CE]
        rx_thr = [_dbm_to_raw(v) for v in thr['rx']] if 'rx' in thr else             [0x2710, 0x0064, 0x1F04, 0x00A0]
        p02 = {}
        for addr, val in [
            (0x80, 0x5000), (0x82, 0x0000), (0x84, 0x4B00), (0x86, 0x0500),  # Temp
            (0x88, 0x8CA0), (0x8A, 0x7530), (0x8C, 0x88B8), (0x8E, 0x7918),  # Vcc
            (0xB0, tx_thr[0]), (0xB2, tx_thr[1]),                            # TxPwr
            (0xB4, tx_thr[2]), (0xB6, tx_thr[3]),
            (0xB8, bias_thr[0]), (0xBA, bias_thr[1]),                        # TxBias
            (0xBC, bias_thr[2]), (0xBE, bias_thr[3]),
            (0xC0, rx_thr[0]), (0xC2, rx_thr[1]),                            # RxPwr
            (0xC4, rx_thr[2]), (0xC6, rx_thr[3]),
        ]:
            p02[addr] = (val >> 8) & 0xFF
            p02[addr + 1] = val & 0xFF
        regs[0x02] = p02

        # ==== Page 04h — Laser Capabilities (ONLY for tunable profiles) ====
        if p['tunable']:
            p04 = {}
            p04[0x80] = 0xB0                # 75/100/50 GHz grids
            p04[0x81] = 0x80                # FineTuningSupported
            for a in range(0x82, 0xA6): p04[a] = 0x00
            # 50 GHz grid channel range ±80
            p04[0x92] = 0xFF; p04[0x93] = 0xB0    # -80
            p04[0x94] = 0x00; p04[0x95] = 0x50    # +80
            # 100 GHz grid channel range ±40
            p04[0x96] = 0xFF; p04[0x97] = 0xD8    # -40
            p04[0x98] = 0x00; p04[0x99] = 0x28    # +40
            # Fine tuning: 1 MHz resolution, ±12.5 GHz
            p04[0xBE] = 0x00; p04[0xBF] = 0x01
            v = struct.pack(">h", -12500)
            p04[0xC0] = v[0]; p04[0xC1] = v[1]
            v = struct.pack(">h", 12500)
            p04[0xC2] = v[0]; p04[0xC3] = v[1]
            # Programmable output power range
            v = struct.pack(">h", -1000)
            p04[0xC6] = v[0]; p04[0xC7] = v[1]
            v = struct.pack(">h", 300)
            p04[0xC8] = v[0]; p04[0xC9] = v[1]
            regs[0x04] = p04

        # ==== Page 10h — DataPath Configuration ====
        p10 = {}
        p10[0x80] = 0x00                            # 128 DataPathDeinit all clear
        for a in range(0x81, 0x91): p10[a] = 0x00   # 129-144 lane controls + Apply*
        for i in range(8): p10[0x91 + i] = 0x10     # 145-152 DPConfigLane: AppSel=1
        regs[0x10] = p10

        # ==== Page 11h — DataPath Status & Monitoring ====
        p11 = {}
        for a in range(0x80, 0x84): p11[a] = 0x44   # DP State: Activated
        for a in range(0x84, 0x99): p11.setdefault(a, 0x00)
        # Per-lane Tx Power
        tx_raw = int(p['tx_power_uw_nom'] * 10) & 0xFFFF
        for i in range(8):
            a = 0x9A + i * 2
            p11[a] = (tx_raw >> 8) & 0xFF
            p11[a + 1] = tx_raw & 0xFF
        # Per-lane Tx Bias
        bias_raw = int(p['tx_bias_ma_nom'] / 0.002) & 0xFFFF
        for i in range(8):
            a = 0xAA + i * 2
            p11[a] = (bias_raw >> 8) & 0xFF
            p11[a + 1] = bias_raw & 0xFF
        # Per-lane Rx Power
        rx_raw = int(p['rx_power_uw_nom'] * 10) & 0xFFFF
        for i in range(8):
            a = 0xBA + i * 2
            p11[a] = (rx_raw >> 8) & 0xFF
            p11[a + 1] = rx_raw & 0xFF
        # ConfigStatus: all Success
        for a in range(0xCA, 0xCE): p11[a] = 0x11
        # DPConfigLane: AppSel=1
        for i in range(8): p11[0xCE + i] = 0x10
        regs[0x11] = p11

        # ==== Page 12h — Laser Tuning Control/Status (ONLY for tunable) ====
        if p['tunable']:
            p12 = {}
            for i in range(8): p12[0x80 + i] = 0x50    # 100 GHz grid per lane
            for i in range(16): p12[0x88 + i] = 0x00   # channel = 0
            for i in range(16): p12[0x98 + i] = 0x00   # fine offset = 0
            # CurrentLaserFrequency U32 = 193100000 (193.1 THz × 10^6 kHz)
            freq_u32 = 193_100_000
            for i in range(8):
                a = 0xA8 + i * 4
                p12[a] = (freq_u32 >> 24) & 0xFF
                p12[a + 1] = (freq_u32 >> 16) & 0xFF
                p12[a + 2] = (freq_u32 >> 8) & 0xFF
                p12[a + 3] = freq_u32 & 0xFF
            for i in range(16): p12[0xC8 + i] = 0x00   # target power = 0
            for i in range(8): p12[0xDE + i] = 0x00    # status: locked, not tuning
            for i in range(8): p12[0xE7 + i] = 0x00    # flags clear
            regs[0x12] = p12

        # ==== Page 13h — Diagnostic Controls ====
        p13 = {}
        for base in [0x90, 0x98, 0xA0, 0xA8]:
            for off in range(8): p13[base + off] = 0x00
        p13[0xB4] = 0; p13[0xB5] = 0; p13[0xB6] = 0; p13[0xB7] = 0
        regs[0x13] = p13

        # ==== Page 14h — Diagnostic Results ====
        p14 = {}
        p14[0x80] = 0x00
        p14[0x8A] = 0x00; p14[0x8B] = 0x00
        for lane in range(8):
            w = cmis.encode_f16_ber(p['base_ber'])
            p14[0xC0 + lane * 2] = (w >> 8) & 0xFF
            p14[0xC0 + lane * 2 + 1] = w & 0xFF
            p14[0xD0 + lane * 2] = (w >> 8) & 0xFF
            p14[0xD0 + lane * 2 + 1] = w & 0xFF
        regs[0x14] = p14

        # ==== CMIS 5.4 optional pages, only for profiles that advertise them ====
        if p.get('cmis_rev', 0x53) >= 0x54 and p.get('pages_ext_173', 0):
            p0c = {}
            # Page map: mark the pages this mock actually serves. Built from
            # regs rather than from a fixed list, because Page 0Ch exists to
            # end the disagreement between scattered advertisements and what
            # the module answers - a hardcoded list here would recreate it
            # (04h and 12h only exist on tunable profiles).
            p0c[0xA0] = 0x54          # ConsolidatedPM defined in CMIS 5.4
            p0c[0xA1] = 0x33          # fully compliant on both counts
            regs[0x0C] = p0c          # filled in below, once every page exists

            p60 = {0x80: p.get('default_polarity_tx', 0),
                   0x81: p.get('default_polarity_rx', 0),
                   # Table 8-188: Rx/Tx/DpRx/DpTx supported are bits 7-4;
                   # bits 3-0 are reserved, so 0x0F advertised nothing at all.
                   0x82: 0xF0}
            regs[0x60] = p60

            p61 = {}
            for lane in range(8):
                # Four distinct seeds: identical ones would let a parser that
                # crosses Rx with Tx read back as if it were correct.
                for base, seed in ((0x80, 1), (0x90, 2), (0xA0, 3), (0xB0, 4)):
                    v = seed + lane
                    p61[base + lane * 2] = (v >> 8) & 0xFF
                    p61[base + lane * 2 + 1] = v & 0xFF
            regs[0x61] = p61

            p62 = {}
            # Page 62h carries the per-lane Tx thresholds in 0.01 dBm. Once a
            # lane switches to power-relative supervision (5.4 section 7.5.3)
            # these supersede Page 02h; no lane here has, so they agree with it
            # - and they are derived from the same raw values so that they
            # cannot drift, whether or not the profile names a PMD.
            lane_thr = tuple(_raw_to_dbm_centi(v) for v in tx_thr)
            for lane in range(8):
                off = 0x80 + lane * 8
                # hi alarm, lo alarm, hi warn, lo warn in 0.01 dBm
                for k, val in enumerate(lane_thr):
                    v = val & 0xFFFF
                    p62[off + k * 2] = (v >> 8) & 0xFF
                    p62[off + k * 2 + 1] = v & 0xFF
            regs[0x62] = p62

            if p.get('misc_caps_252', 0) & 0x20:
                p6d = {0x80: 0x30}                     # commit duration code 3
                for lane in range(8):
                    p6d[0x88 + lane] = lane + 1        # staged (RW), identity
                    p6d[0xA8 + lane] = 0               # no commit result yet
                    # 6Dh:184-191 is what the switch is actually doing. The
                    # spec says it starts unpermuted and that enabling alone
                    # does not commit, so it only moves on a commit command.
                    p6d[0xB8 + lane] = lane + 1
                p6d[0x98] = 0x00                       # redirection disabled
                regs[0x6D] = p6d

        # Lane-banked pages for modules with more than eight lanes. Bank b
        # holds lanes 8b+1..8b+8 at the same addresses, so each extra bank is
        # a copy of bank 0 - nudged, so a bug that silently serves bank 0 for
        # every bank shows up as identical readings instead of hiding.
        lane_count = p.get('lanes', 8)
        if lane_count > 8:
            for bank in range(1, (lane_count + 7) // 8):
                for page in (0x10, 0x11, 0x12, 0x13, 0x14, 0x60, 0x61, 0x62, 0x6D):
                    if page not in regs:
                        continue
                    copy = dict(regs[page])
                    if page == 0x11:
                        for lane in range(8):
                            for base, step in ((0x9A, 40), (0xBA, -30), (0xAA, 90)):
                                a = base + lane * 2
                                if a in copy:
                                    v = ((copy[a] << 8) | copy.get(a + 1, 0))
                                    v = max(0, min(0xFFFF, v + bank * step))
                                    copy[a] = (v >> 8) & 0xFF
                                    copy[a + 1] = v & 0xFF
                    regs[(page, bank)] = copy

        # Page 0Ch's map, filled in last so it describes what was actually
        # built rather than what someone meant to build.
        if 0x0C in regs:
            for page in regs:
                if isinstance(page, int):
                    a = 0x80 + page // 8
                    regs[0x0C][a] = regs[0x0C].get(a, 0) | (1 << (page % 8))

        return regs

    # ------------------------------------------------------------------
    # State machine update
    # ------------------------------------------------------------------
    def _update_state_machine(self):
        now = time.time()

        # Module state machine (Reset / LowPwr)
        if self._reset_time > 0:
            dt = now - self._reset_time
            if dt < 0.3:
                self._module_state = 0b001
            elif dt < 0.8:
                self._module_state = 0b010
            else:
                self._module_state = 0b011
                self._reset_time = 0
                self._dp_lane_states = [0x4] * 8
                # Every lane came back through DPInit after the reset.
                self._registers[0x11][0x86] = 0xFF
                self._apply_time = 0
        elif self._lp_request_time > 0:
            self._module_state = 0b001
            self._dp_lane_states = [0x1] * 8
        else:
            if self._module_state not in (0b011,):
                self._module_state = 0b011

        self._registers[None][0x03] = (self._module_state << 1) | 0x01

        # DataPath state machine (ApplyDataPath)
        if self._apply_time > 0 and self._reset_time == 0:
            dt = now - self._apply_time
            if dt < 0.2:
                for i in range(8):
                    if not ((self._tx_disable_mask >> i) & 1):
                        self._dp_lane_states[i] = 0x2
            elif dt < 0.5:
                for i in range(8):
                    if not ((self._tx_disable_mask >> i) & 1):
                        self._dp_lane_states[i] = 0x5
            else:
                for i in range(8):
                    if not ((self._tx_disable_mask >> i) & 1):
                        self._dp_lane_states[i] = 0x4
                    else:
                        self._dp_lane_states[i] = 0x1
                    # 6.3.3: the Flag is set on entry to a lasting steady state
                    # reached through a significant transient - which is what
                    # has just happened, since the path went through DPInit and
                    # DPTxTurnOn to get here. It is a Flag, so it latches until
                    # read: this is the module's record that the path bounced.
                    self._registers[0x11][0x86] =                         self._registers[0x11].get(0x86, 0) | (1 << i)
                self._apply_time = 0
                for a in range(0xCA, 0xCE):
                    self._registers[0x11][a] = 0x11

        # Write DP states back to Page 11h:0x80-0x83
        for i in range(8):
            byte_idx = i // 2
            nibble_pos = (i % 2) * 4
            addr = 0x80 + byte_idx
            old = self._registers[0x11].get(addr, 0)
            mask = 0x0F << nibble_pos
            self._registers[0x11][addr] = (old & ~mask) | ((self._dp_lane_states[i] & 0x0F) << nibble_pos)

        # PRBS LOL flags (lock after 0.3 s)
        for key, lol_addr in [('hc', 0x8A), ('mc', 0x8B)]:
            t_en = self._prbs_enable_times.get(key, 0)
            if t_en > 0:
                self._registers[0x14][lol_addr] = 0xFF if (now - t_en) < 0.3 else 0x00

    # ------------------------------------------------------------------
    def _update_dynamic_values(self):
        self._update_state_machine()
        p = self._profile
        t = time.time() - self._start_time

        # Temperature: nominal ± 3°C, 90s period
        temp_c = p['temperature_c_nom'] + 3.0 * math.sin(2 * math.pi * t / 90.0)
        raw = int(temp_c * 256) & 0xFFFF
        self._registers[None][0x0E] = (raw >> 8) & 0xFF
        self._registers[None][0x0F] = raw & 0xFF

        # Per-lane monitors
        for lane in range(8):
            phase = lane * math.pi / 4
            tx_disabled = bool((self._tx_disable_mask >> lane) & 1)
            dp_active = self._dp_lane_states[lane] == 0x4

            # Tx Power: 0 if disabled or not Activated, else nominal ± 3%
            if tx_disabled or not dp_active:
                tx_uw = 0.0
            else:
                tx_uw = p['tx_power_uw_nom'] * (1.0 + 0.03 * math.sin(2 * math.pi * t / 60.0 + phase))
            tx_val = int(tx_uw * 10) & 0xFFFF
            a = 0x9A + lane * 2
            self._registers[0x11][a] = (tx_val >> 8) & 0xFF
            self._registers[0x11][a + 1] = tx_val & 0xFF

            # Tx Fault flag if disabled
            if tx_disabled:
                self._registers[0x11][0x87] |= (1 << lane)
            else:
                self._registers[0x11][0x87] &= ~(1 << lane)

            # Tx Bias
            if dp_active:
                bias_ma = p['tx_bias_ma_nom'] * (1.0 + 0.033 * math.sin(2 * math.pi * t / 120.0 + phase))
            else:
                bias_ma = 0.0
            bias_val = int(bias_ma / 0.002) & 0xFFFF
            a = 0xAA + lane * 2
            self._registers[0x11][a] = (bias_val >> 8) & 0xFF
            self._registers[0x11][a + 1] = bias_val & 0xFF

            # Rx Power
            rx_uw = p['rx_power_uw_nom'] * (1.0 + 0.05 * math.sin(2 * math.pi * t / 45.0 + phase))
            rx_val = int(rx_uw * 10) & 0xFFFF
            a = 0xBA + lane * 2
            self._registers[0x11][a] = (rx_val >> 8) & 0xFF
            self._registers[0x11][a + 1] = rx_val & 0xFF

            self._set_lane_flags(lane, tx_uw if not tx_disabled else 0.0,
                                 bias_ma, rx_uw)

        self._set_module_flags(temp_c)

        # CDR-LOL simulation on lane 8
        self._registers[0x11][0x89] = 0x80 if (int(t / 60) % 2) else 0x00
        self._registers[0x11][0x94] = 0x80 if (int(t / 75) % 2) else 0x00

        # Diagnostic selector-dependent updates
        sel = self._registers[0x14].get(0x80, 0)
        base_ber = p['base_ber']

        for lane in range(8):
            phase = lane * math.pi / 4

            if sel == 0x01 or sel == 0x11:
                h_ber = base_ber * (1.0 + 0.20 * math.sin(2 * math.pi * t / 30.0 + phase))
                m_ber = base_ber * (1.0 + 0.25 * math.sin(2 * math.pi * t / 35.0 + phase))
                w = cmis.encode_f16_ber(h_ber)
                a = 0xC0 + lane * 2
                self._registers[0x14][a] = (w >> 8) & 0xFF
                self._registers[0x14][a + 1] = w & 0xFF
                w = cmis.encode_f16_ber(m_ber)
                a = 0xD0 + lane * 2
                self._registers[0x14][a] = (w >> 8) & 0xFF
                self._registers[0x14][a + 1] = w & 0xFF

            elif sel == 0x06:
                snr_db = p['snr_db_nom'] + 2.0 * math.sin(2 * math.pi * t / 40.0 + phase)
                snr_val = int(snr_db * 256) & 0xFFFF
                a_h = 0xD0 + lane * 2
                self._registers[0x14][a_h] = snr_val & 0xFF
                self._registers[0x14][a_h + 1] = (snr_val >> 8) & 0xFF
                a_m = 0xF0 + lane * 2
                self._registers[0x14][a_m] = snr_val & 0xFF
                self._registers[0x14][a_m + 1] = (snr_val >> 8) & 0xFF

        # Error/Bit counters (selectors 0x02-0x05, 0x12-0x15)
        if sel in (0x02, 0x03, 0x04, 0x05, 0x12, 0x13, 0x14, 0x15):
            now_t = time.time()
            dt = now_t - self._last_counter_time if self._last_counter_time > 0 else 0.1
            self._last_counter_time = now_t
            bits_per_sec = int(100e9)   # 100 Gbps per lane
            is_high = sel in (0x03, 0x05, 0x13, 0x15)
            lane_start = 4 if is_high else 0
            for li in range(4):
                lane = lane_start + li
                new_bits = int(bits_per_sec * dt)
                new_errors = int(new_bits * base_ber * (1.0 + 0.2 * math.sin(t + lane)))
                self._bit_counts[lane] += new_bits
                self._error_counts[lane] += max(new_errors, 0)
                off = 0xC0 + li * 16
                ec = self._error_counts[lane]
                bc = self._bit_counts[lane] & ~1    # PSL=0 in LSB
                for j in range(8):
                    self._registers[0x14][off + j] = (ec >> (j * 8)) & 0xFF
                for j in range(8):
                    self._registers[0x14][off + 8 + j] = (bc >> (j * 8)) & 0xFF

        # Laser tuning: update CurrentLaserFrequency from Page 12h control values (tunable only)
        if p['tunable'] and 0x12 in self._registers:
            p12 = self._registers[0x12]
            for lane in range(8):
                grid_byte = p12.get(0x80 + lane, 0x50)
                grid_code = (grid_byte >> 4) & 0x0F
                grid_steps = {0: 0.003125, 1: 0.00625, 2: 0.0125, 3: 0.025,
                              4: 0.05, 5: 0.1, 6: 1.0/30, 7: 0.075, 8: 0.15}
                step_thz = grid_steps.get(grid_code, 0.1)
                ch_hi = p12.get(0x88 + lane * 2, 0)
                ch_lo = p12.get(0x89 + lane * 2, 0)
                ch_n = struct.unpack(">h", bytes([ch_hi, ch_lo]))[0]
                ft_hi = p12.get(0x98 + lane * 2, 0)
                ft_lo = p12.get(0x99 + lane * 2, 0)
                ft_offset = struct.unpack(">h", bytes([ft_hi, ft_lo]))[0]
                fine_ghz = ft_offset * 0.001 if (grid_byte & 0x01) else 0.0
                freq_thz = 193.1 + ch_n * step_thz + fine_ghz / 1000.0
                freq_mhz = int(round(freq_thz * 1e6))
                a = 0xA8 + lane * 4
                p12[a] = (freq_mhz >> 24) & 0xFF
                p12[a + 1] = (freq_mhz >> 16) & 0xFF
                p12[a + 2] = (freq_mhz >> 8) & 0xFF
                p12[a + 3] = freq_mhz & 0xFF
                p12[0xDE + lane] = 0x00

    # ------------------------------------------------------------------
    def _intercept_write(self, register, data):
        """Trigger state-machine transitions; return the bytes to actually store.

        Self-clearing trigger bits are stripped here so a read-back never shows
        them still set, matching how a real module behaves.
        """
        if register < 0x80:
            if register == 0x1A:
                ctrl = data[0]
                if ctrl & 0x08:
                    self._reset_time = time.time()
                    self._module_state = 0b001
                    self._dp_lane_states = [0x1] * 8
                    self._lp_request_time = 0
                    # SoftwareReset is self-clearing (Table 8-10): a real module
                    # never reads it back as 1, so neither may the mock, or the
                    # UI shows "reset in progress" forever.
                    data = bytes([ctrl & ~0x08]) + bytes(data[1:])
                    return data
                if ctrl & 0x10:
                    self._lp_request_time = time.time()
                elif not (ctrl & 0x10) and self._lp_request_time > 0:
                    self._lp_request_time = 0
                    self._dp_lane_states = [0x4] * 8
        elif self._current_page == 0x10:
            # Writes may span several control bytes, so match on the range
            span = range(register, register + len(data))
            if 0x82 in span:                                        # OutputDisableTx
                self._tx_disable_mask = data[0x82 - register]
            if 0x8F in span and data[0x8F - register] == 0xFF:      # ApplyDPInit
                self._apply_time = time.time()
                for a in range(0xCA, 0xCE):
                    self._registers[0x11][a] = 0xCC
        elif self._current_page == 0x13:
            prbs_map = {0x90: 'hg', 0x98: 'mg', 0xA0: 'hc', 0xA8: 'mc'}
            if register in prbs_map and data[0] != 0:
                self._prbs_enable_times[prbs_map[register]] = time.time()
        elif self._current_page == 0x60:
            # 60h:192-193 are write-only bitmasks that zero the per-lane
            # acquisition counters on Page 61h. Storing the mask and leaving
            # the counters alone would let a reset look accepted while every
            # count stayed where it was.
            span = range(register, register + len(data))
            cleared = bytearray(data)
            for addr, base in ((0xC0, 0x80), (0xC1, 0x90)):
                if addr in span:
                    self._clear_acq_counters(data[addr - register], base)
                    cleared[addr - register] = 0        # WO/SC
            data = bytes(cleared)
        elif self._current_page == 0x6D:
            span = range(register, register + len(data))
            if 0xA0 in span and data[0xA0 - register] & 1:
                self._commit_media_lane_redirection()
                # CommitMediaLaneRedirection is WO/SC (Table 8-196).
                buf = bytearray(data)
                buf[0xA0 - register] &= ~1
                data = bytes(buf)
        return data

    # Per-lane flag registers and the Page 02h threshold pair each one watches.
    # Order matters only in that a flag must be paired with the limit a module
    # would actually compare against.
    _FLAG_MAP = (
        # (flag addr hi, flag addr lo, threshold addr hi, threshold addr lo, which)
        (0x8B, 0x8C, 0xB0, 0xB2, 'tx_power'),      # alarms
        (0x8D, 0x8E, 0xB4, 0xB6, 'tx_power'),      # warnings
        (0x8F, 0x90, 0xB8, 0xBA, 'tx_bias'),
        (0x91, 0x92, 0xBC, 0xBE, 'tx_bias'),
        (0x95, 0x96, 0xC0, 0xC2, 'rx_power'),
        (0x97, 0x98, 0xC4, 0xC6, 'rx_power'),
    )

    def _set_module_flags(self, temp_c):
        """Raise the module-level temperature and Vcc flags the readings earn.

        This byte used to flip every thirty seconds on a timer, so a module
        sitting at 55 C in a 0-80 C window announced a temperature alarm twice
        a minute and cleared it again. The first thing anyone looks at is the
        alarm summary, and one that fires at random teaches that none of the
        module's flags are worth reading.
        """
        p02 = self._registers.get(0x02, {})
        lower = self._registers[None]

        def s16(addr):
            raw = (p02.get(addr, 0) << 8) | p02.get(addr + 1, 0)
            return (raw - 0x10000 if raw & 0x8000 else raw) / 256.0

        def u16(addr):
            return ((p02.get(addr, 0) << 8) | p02.get(addr + 1, 0)) * 1e-4

        vcc_v = (((lower.get(0x10, 0) << 8) | lower.get(0x11, 0)) * 1e-4)
        bits = 0
        for shift, hit in enumerate((
                temp_c > s16(0x80), temp_c < s16(0x82),      # alarms
                temp_c > s16(0x84), temp_c < s16(0x86),      # warnings
                vcc_v > u16(0x88), vcc_v < u16(0x8A),
                vcc_v > u16(0x8C), vcc_v < u16(0x8E))):
            if hit:
                bits |= 1 << shift
        # Sticky, like every other Flag: a temperature excursion that has ended
        # is still the thing the operator needs to know about.
        lower[0x09] = lower.get(0x09, 0) | bits

    def _set_lane_flags(self, lane, tx_uw, bias_ma, rx_uw):
        """Raise the flags a module would raise for the values it is reporting.

        These used to be a block of zeros written once, so a mock could report
        a power far below its own low alarm and still say nothing was wrong -
        the display coloured the cell red from the threshold while the module
        insisted it was fine. A demo that cannot show a fault is no use for the
        training the manual describes, and one that contradicts itself teaches
        that the flags are not worth reading.
        """
        p02 = self._registers.get(0x02, {})
        p11 = self._registers[0x11]

        def thr(addr):
            return (p02.get(addr, 0) << 8) | p02.get(addr + 1, 0)

        measured = {
            'tx_power': int(tx_uw * 10),
            'tx_bias': int(bias_ma / 0.002),
            'rx_power': int(rx_uw * 10),
        }
        bit = 1 << lane
        for hi_flag, lo_flag, hi_thr, lo_thr, key in self._FLAG_MAP:
            value = measured[key]
            for addr, over in ((hi_flag, value > thr(hi_thr)),
                               (lo_flag, value < thr(lo_thr))):
                if over:
                    p11[addr] = p11.get(addr, 0) | bit

        # Losing the signal is what a receiver reports when there is nothing
        # to lock to, so tie it to the same limit rather than inventing one.
        if measured['rx_power'] < thr(0xC2):
            p11[0x93] = p11.get(0x93, 0) | bit
            p11[0x94] = p11.get(0x94, 0) | bit

    def _clear_acq_counters(self, mask, base):
        """Zero the lanes named in a 60h reset mask, within the current bank.

        The mask covers the eight lanes of one bank, so lane 9 is bit 0 of
        bank 1 - clearing by absolute lane number would zero lane 1 instead.
        """
        p61 = self._registers.get((0x61, self._current_bank))
        if p61 is None:
            p61 = self._registers.get(0x61)
        if p61 is None:
            return
        for lane in range(8):
            if mask & (1 << lane):
                p61[base + lane * 2] = 0
                p61[base + lane * 2 + 1] = 0

    def _commit_media_lane_redirection(self):
        """Move the staged mapping (6Dh:136-143) into effect (6Dh:184-191).

        The command is validated before execution and nothing changes on a
        validation failure, so a rejected commit leaves the switch where it
        was and says why in the per-lane result codes.
        """
        p6d = self._registers.get((0x6D, self._current_bank))
        if p6d is None:
            p6d = self._registers.get(0x6D)
        if p6d is None or not (p6d.get(0x98, 0) & 1):
            return                              # disabled: commit has no effect
        staged = [p6d.get(0x88 + i, 0) for i in range(8)]
        ok = sorted(staged) == list(range(1, 9))
        for i in range(8):
            p6d[0xA8 + i] = 1 if ok else 4      # success / not a permutation
            if ok:
                p6d[0xB8 + i] = staged[i]

    # ------------------------------------------------------------------
    def connect(self, bus: int, address: int) -> None:
        self._connected = True
        self._start_time = time.time()
        self._last_counter_time = time.time()

    def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_backend_info(self) -> dict:
        return {
            'name': getattr(self, 'BACKEND_NAME', 'mock'),
            'description': f'Mock: {self._profile["display"]}',
            'current_page': self._current_page,
        }

    def read_bytes(self, register: int, length: int) -> bytes:
        if not self._connected:
            raise IOError("Not connected")
        self._update_dynamic_values()
        if register < 0x80:
            page_dict = self._registers.get(None, {})
        else:
            # Bank-specific data when the profile supplies it, otherwise the
            # page as-is: an 8-lane module has only bank 0 and never notices.
            page_dict = self._registers.get(
                (self._current_page, self._current_bank),
                self._registers.get(self._current_page, {}))
        result = bytearray(length)
        for i in range(length):
            result[i] = page_dict.get(register + i, 0x00)
        self._clear_on_read(page_dict, register, length)
        return bytes(result)

    # Flag bytes: 11h:135-152 per lane, and Lower 8-9 module-wide. CMIS 5.4
    # calls these latched with clear-on-read access - "a Flag bit remains set
    # until cleared by a READ of the Byte containing the Flag" - so a module
    # holds a momentary fault until somebody looks, and forgets it once they
    # have. A mock that instead tracked the live value could never show a
    # transient at all, and let the host get away with not remembering.
    _COR_BYTES = {
        None: range(0x08, 0x0A),
        0x11: range(0x86, 0x99),
    }

    def _clear_on_read(self, page_dict, register: int, length: int) -> None:
        page = None if register < 0x80 else self._current_page
        span = self._COR_BYTES.get(page)
        if span is None:
            return
        for addr in range(register, register + length):
            if addr in span:
                page_dict[addr] = 0x00

    def write_bytes(self, register: int, data: bytes) -> None:
        if not self._connected:
            raise IOError("Not connected")
        data = self._intercept_write(register, data)
        if register < 0x80:
            page_dict = self._registers.setdefault(None, {})
            for i, b in enumerate(data):
                page_dict[register + i] = b
            if register <= 0x7E <= register + len(data) - 1:
                self._current_bank = data[0x7E - register]
            if register <= 0x7F <= register + len(data) - 1:
                self._current_page = data[0x7F - register]
        else:
            key = ((self._current_page, self._current_bank)
                   if (self._current_page, self._current_bank) in self._registers
                   else self._current_page)
            page_dict = self._registers.setdefault(key, {})
            for i, b in enumerate(data):
                page_dict[register + i] = b


# ============================================================================
# Registered Backend Subclasses
# ============================================================================

@register_backend("mock_coherent")
class MockCoherentBackend(MockBackend):
    PROFILE = _COHERENT_800G
    BACKEND_NAME = 'mock_coherent'


@register_backend("mock_dr8")
class MockDR8Backend(MockBackend):
    PROFILE = _DR8_800G
    BACKEND_NAME = 'mock_dr8'


@register_backend("mock_sr8")
class MockSR8Backend(MockBackend):
    PROFILE = _SR8_800G
    BACKEND_NAME = 'mock_sr8'


@register_backend("mock_1600g_dr8")
class Mock1600GDr8Backend(MockBackend):
    PROFILE = _DR8_1600G


@register_backend("mock_1600g_16lane")
class Mock1600G16LaneBackend(MockBackend):
    PROFILE = _XD16_1600G


@register_backend("mock_coherent_zr")
class MockCoherentZRBackend(MockBackend):
    PROFILE = _ZR_800G


@register_backend("mock_fr4x2")
class MockFR4x2Backend(MockBackend):
    PROFILE = _FR4X2_800G
    BACKEND_NAME = 'mock_fr4x2'
