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


def _bias_scale(profile):
    """01h:160.4-3 as a multiplier. 65535 increments of 2 uA stop at 131 mA,
    so a laser biased above that has to advertise x2 or x4."""
    return {0: 1, 1: 2, 2: 4}.get((profile.get('monitors_160', 0x07) >> 3) & 0x03, 1)


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
    # Cooled transmitter: Aux1 = TEC current, Aux2 = laser temperature,
    # Aux3 = Vcc2 (01h:145 = cooled | aux1 TEC | aux3 Vcc2).
    'aux_observable_145': 0x85,
    'monitors_159':       0x1F,
    # Aux3 is S16 at 100 uV/LSB, so it tops out at 3.2767 V - a 3.3 V rail
    # does not fit. 1.8 V is the secondary rail a coherent module reports.
    'aux_values':         (-38.0, 45.0, 1.8),
    'display':         '800GBASE-LR1 coherent lite (DP-16QAM, SMF 10km, 802.3dj)',
    'config_caps_02':  0x00,  # legacy default: hot and regular both supported
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
    # Cooled transmitter: Aux1 = TEC current, Aux2 = laser temperature,
    # Aux3 = Vcc2 (01h:145 = cooled | aux1 TEC | aux3 Vcc2).
    'aux_observable_145': 0x85,
    'monitors_159':       0x1F,
    # Aux3 is S16 at 100 uV/LSB, so it tops out at 3.2767 V - a 3.3 V rail
    # does not fit. 1.8 V is the secondary rail a coherent module reports.
    'aux_values':         (-38.0, 45.0, 1.8),
    'display':         '800G Coherent tunable (C-band DWDM, ZR-class)',
    'config_caps_02':  0x00,  # retuned under traffic, so hot reconfiguration matters
    # Biased past the 131 mA that x1 scaling can express, so 160.4-3 says x2.
    'monitors_160':    0x0F,
    'bias_thresholds_ma': (260.0, 90.0, 240.0, 100.0),
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
    'tx_bias_ma_nom':      180.0,
    'temperature_c_nom':   62.0,
    'base_ber':            1.0e-9,
    'snr_db_nom':          18.0,
    'app_descriptors': [
        (0x51, 0x48, 0x81, 0x01),            # AppSel 1: 800GAUI-8 S C2M -> ZR200-OFEC-QPSK (8H/1M)
        (0x4F, 0x41, 0x41, 0x01),            # AppSel 2: 400GAUI-4-S C2M -> 200GBASE-ER4 (4H/1M)
    ],
    'link_lengths': {},                      # no cable length advertised
    'cmis_rev':            0x54,
    'pages_ext_173':       0b10000000,       # Page 0Ch
    # Page 62h only. 60h and 61h are separate features this module does not
    # claim, and Page 0Ch's map is built from what is actually served, so
    # serving a page it does not advertise would put the two at odds.
    'pages_ext_174':       0b00100000,
    # 04h:196.6: programmable output power is what relative supervision
    # thresholds are relative to, so this is the profile that has them.
    'rel_thr_cap_196':     0x40,
    # 01h:155.5-4 = 10b: a coherent line side squelches by reducing average
    # power, and the method is the module's, not the host's.
    'controls_155':        0x2F,
    # 04h:129.5, the 300 GHz grid CMIS 5.4 added, alongside fine tuning.
    'grid_sup_129':        0xA0,
    # 12h:216-217, U4 halves of a dB: +2.0/+1.5 dB and -2.0/-1.5 dB.
    'rel_thr_offsets_216': (0x32, 0x32),
    # Lanes 1-4 were left switched to relative supervision by whoever
    # configured this module; 5-8 still use the module-wide thresholds.
    'rel_thr_enabled_lanes': (1, 1, 1, 1, 0, 0, 0, 0),
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
    # 01h:155.5-4 = 11b: this one lets the host pick OMA or Pav, which is the
    # only case where the bit at Lower 0x1A.5 decides anything.
    'controls_155':        0x3F,
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
    'config_caps_02':  0x45,  # stepped only, regular; 1 MHz MCI
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

# CMIS 5.4 raised the lane ceiling from 32 to 256 by giving 01h:142.1-0 an
# escape value: 11b means the real bank count is in 01h:174.4-0. Every other
# profile here has a lane count the legacy field can spell (8, 16 or 32), so
# nothing exercised the escape - neither the mock branch that writes it nor
# the host decode that reads it. Twenty-four lanes is the smallest count that
# has no legacy spelling, which is exactly what the escape exists for.
_XD24 = {
    'display':         '24 host lanes, three banks (CMIS 5.4 bank escape)',
    'config_caps_02':  0x45,  # stepped only, regular; 1 MHz MCI
    'vendor_name':     b"OPENCMIS DEMO   ",
    'vendor_pn':       b"DEMO-XD24-3BANK ",
    'vendor_sn':       b"DEMO000000008   ",
    'vendor_rev':      b"A0",
    'vendor_oui':      (0x00, 0x00, 0x00),
    'date_code':       b"26010200",
    'clei':            b"DEMOCLEI24",
    'media_type':          0x02,             # SMF
    'connector_type':      0x28,             # MPO
    'media_if_tech':       0x06,             # 1310 nm EML
    'power_class_bits':    0xE0,             # Class 8
    'max_power_0_25w':     0x78,             # 120 x 0.25 = 30.0 W
    'tunable':             False,
    'tx_power_uw_nom':     1259,             # +1.0 dBm per lane
    'rx_power_uw_nom':     500,              # -3.0 dBm per lane
    'tx_bias_ma_nom':      72.0,
    'temperature_c_nom':   67.0,
    'base_ber':            8.0e-6,
    'snr_db_nom':          20.5,
    'app_descriptors': [
        # An Application is capped at eight lanes (5.4 section 6.4.1), so the
        # module advertises ones that fit a lane group and instantiates them
        # per group - three groups here rather than the two a 16-lane module
        # has. 0x11 is the bitmap of permissible starting lanes, 1 and 5.
        (0x51, 0x56, 0x88, 0x01),            # AppSel 1: 800GAUI-8 S C2M -> 800GBASE-DR8 (8H/8M)
        (0x4F, 0x1C, 0x44, 0x11),            # AppSel 2: 400GAUI-4-S C2M -> 400GBASE-DR4 (4H/4M)
    ],
    'link_lengths': {'smf_len_byte': 0x05},   # 0.5 km
    'cmis_rev':            0x54,
    'lanes':               24,               # three banks; 01h:142.1-0 = 11b
    # 01h:156.7 BankBroadcastSupported. Only a lane-banked module has anything
    # to broadcast to, and this is the widest one here.
    'controls_156':        0x87,
    'default_polarity_tx': 0b00001001,       # lanes 1 and 4 wired inverted
    'default_polarity_rx': 0b00000100,       # lane 3 wired inverted
    'pages_ext_173':       0b10000000,       # Page 0Ch
    'pages_ext_174':       0b11100000,       # Pages 60h, 61h, 62h
    'misc_caps_252':       0b00100000,       # MediaLaneSwitchingSupported
    'module_subtype':      0x01,
    'heatsink_fiber':      0x30,
}

_SR8_800G = {
    'display':         '800GBASE-SR8 (OM4 100m, VCSEL 850nm)',
    # A simpler retimer throughout: no host-controlled Tx input EQ target and
    # only pre-cursor Rx equalization, so the signal integrity table has to
    # drop columns rather than show controls this module does not have.
    'si_161':          0x0B,
    'si_162':          0x0A,
    # No Tx adaptive input EQ fail Flag and no Rx CDR LOL Flag: a VCSEL module
    # with a simpler retimer, and something for the table to mark as not
    # implemented rather than colour green.
    'flags_157':       0x07,
    'flags_158':       0x02,
    # All four loopback types, but no per-lane granularity and not host and
    # media at the same time (13h:128 bits 6-4 clear).
    'loopback_caps':   0x0F,
    # No input SNR measurement on either side, in keeping with the simpler
    # retimer this profile models: BER and error counts only.
    'diag_reporting_130': 0x03,
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
    # No forced Tx squelch and no Rx polarity flip: a simpler module than the
    # others, and something for the panels to grey out. Its Tx disable is also
    # module-wide (151.0), so the per-lane boxes are not per lane at all.
    'rx_tx_151':       0x11,
    'controls_155':    0x17,
    'controls_156':    0x06,
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
    # This profile is the deliberately restricted one (it already declines
    # force-squelch and Rx polarity flip), so it is where the signal integrity
    # limits are real: fewer amplitude codes than the four that exist, and a
    # different ceiling for each equalizer cursor.
    'si_153': 0x33,                          # amplitude codes 0-1; Tx eq max 3
    'si_154': 0x25,                          # post-cursor max 2, pre-cursor max 5
    'scs_rx_amplitude': 0x11,                # code 1 - one this module has
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
        self._last_module_state = None
        self._start_time = time.time()
        # State machine tracking
        self._module_state = 0b011      # ModuleReady
        self._reset_time = 0.0
        self._lp_request_time = 0.0
        self._apply_time = 0.0
        self._config_result = [0x1] * 8   # per-lane ConfigStatus nibble
        self._config_staged = [0x10] * 8  # Staged set as the Apply saw it
        self._apply_mask = 0xFF           # lanes the last Apply selected
        self._apply_hot = False           # ApplyImmediate rather than DPInit
        self._dp_deinit_mask = 0x00       # 10h:128, one bit per host lane
        self._apply_provision_only = False
        self._tuning_accepted = [True] * 8
        self._dp_lane_states = [0x4] * 8  # all Activated
        self._rx_output_valid = 0xFF     # 11h:132, to spot changes
        self._deinit_time = 0.0          # walking a path down 6h -> 3h -> 1h
        self._deinit_mask = 0x00
        self._absolute_tx_thr = None      # 62h quad when no lane is relative
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
        # [7] MemoryModel, [6] SteppedConfigOnly, [5:2] MciMaxSpeed,
        # [1:0] AutoCommissioning. 0x00 is the legacy default that claims
        # every reconfiguration procedure works, which is not what an
        # Ethernet transceiver reports.
        lower[0x02] = p.get('config_caps_02', 0x41)
        lower[0x03] = (0b011 << 1)          # ModuleReady, interrupt deasserted
        for a in range(0x04, 0x0E): lower[a] = 0x00
        # Temperature
        temp_raw = int(p['temperature_c_nom'] * 256) & 0xFFFF
        lower[0x0E] = (temp_raw >> 8) & 0xFF
        lower[0x0F] = temp_raw & 0xFF
        # Voltage 3.3 V
        lower[0x10] = 0x80; lower[0x11] = 0xE8
        # Aux monitors (generic)
        # Aux1-3 (Lower 18-23, Table 8-10). An uncooled module advertises no
        # Aux monitor at all (01h:159.4-2 clear) and these read zero; a cooled
        # one has to encode each in the unit 01h:145 says it is.
        aux = p.get('aux_values')       # (TEC %, laser degC, Vcc2 V) or None
        if aux:
            tec = struct.pack('>h', int(round(aux[0] * 32767 / 100.0)))
            las = struct.pack('>h', int(round(aux[1] * 256)))
            vcc2 = struct.pack('>h', int(round(aux[2] / 0.0001)))
            lower[0x12], lower[0x13] = tec[0], tec[1]     # Aux1 TEC current
            lower[0x14], lower[0x15] = las[0], las[1]     # Aux2 laser temp
            lower[0x16], lower[0x17] = vcc2[0], vcc2[1]   # Aux3 Vcc2
        else:
            for a in range(0x12, 0x18):
                lower[a] = 0x00
        lower[0x1A] = 0x40                  # ModuleControl: AllowLPHW=1
        lower[0x27] = 2; lower[0x28] = 5    # Active FW 2.5
        lower[0x55] = p['media_type']       # Media Type

        # Application Descriptors (lower 0x56-0x75). AppSelCode is four bits
        # wide, and 8.4.17 puts descriptors 9-15 on Page 01h - see the block
        # written at 0xDF below. The terminator is the same either way: the
        # first unused descriptor carries HostInterfaceID FFh.
        appdesc = list(p['app_descriptors']) + [(0xFF, 0, 0, 0)] * 15
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
                                 ('tx_bias_ma_nom', 1 / (0.002 * _bias_scale(p)), 'mA')):
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
        # 142.5 DiagnosticPagesSupported: every profile builds and serves
        # Pages 13h and 14h, so every profile has to say so.
        p01[0x8E] |= 0x20
        # MediaLaneAssignmentOptions (01h:176-183, Table 8-60) is stored apart
        # from the first four descriptor bytes and is required on a paged
        # module. An Application that uses m of the eight media lanes can start
        # on every m-th one, which is what a breakout Application needs the
        # host to know.
        for i, desc in enumerate(p['app_descriptors'][:15]):
            media_lanes = desc[2] & 0x0F
            if media_lanes:
                p01[0xB0 + i] = sum(1 << (k * media_lanes)
                                    for k in range(8 // media_lanes))

        # 223-250 (Table 8-61): the seven Application Descriptors that do not
        # fit in lower memory. A module with eight or fewer never reaches
        # them - its list has already ended at the FFh terminator - so this
        # writes the terminator there too rather than leaving the block blank.
        extra = list(p['app_descriptors'])[8:] + [(0xFF, 0, 0, 0)] * 7
        for i, (h, m, lc, hla) in enumerate(extra[:7]):
            base = 0xDF + i * 4
            p01[base] = h
            p01[base + 1] = m
            p01[base + 2] = lc
            p01[base + 3] = hla

        # 155-156 Supported Controls Advertisement (Table 8-51). Tunability is
        # taken from the profile so the two cannot disagree: 155.6 says Pages
        # 04h and 12h are there, which is exactly what 'tunable' means here.
        controls_155 = p.get('controls_155', 0x1F)   # squelch reduces OMA,
                                                     # forced + auto squelch,
                                                     # output disable, Tx
                                                     # polarity flip
        if p['tunable']:
            controls_155 |= 0x40
        p01[0x9B] = controls_155
        p01[0x9C] = p.get('controls_156', 0x07)      # auto squelch disable Rx,
                                                     # output disable Rx,
                                                     # Rx polarity flip
        # 157-158 Supported Flags, 159-160 Supported Monitors (Tables 8-52,
        # 8-53). Leaving these zero says the module implements no Flags and no
        # monitors at all, while it goes on reporting both.
        # 145 Aux observables (Table 8-50). A module that reports Aux values
        # without this says nothing about what they mean: Aux2 is degrees
        # Celsius or a percentage of TEC current depending on one bit.
        p01[0x91] = p.get('aux_observable_145', 0x00)
        # 151 Rx/Tx characteristics (Table 8-50). Left at zero this says the
        # Rx power monitor reports OMA and Rx LOS responds to OMA, which is
        # not what any of these profiles actually model.
        p01[0x97] = p.get('rx_tx_151', 0x10)   # PIN, average power, OMA LOS
        # 153-154 signal integrity maxima, 161-162 which SI controls exist
        # (Tables 8-53, 8-54). Left at zero a module says it has no CDR, no
        # equalizer control and no output amplitude control at all, which is
        # not what a retimed module is.
        p01[0x99] = p.get('si_153', 0xF7)       # all four Rx output levels,
                                                # Tx input eq max 7
        p01[0x9A] = p.get('si_154', 0x77)       # pre/post cursor max 7
        p01[0xA1] = p.get('si_161', 0x0F)       # adaptive + host-controlled
                                                # Tx input eq, Tx CDR + bypass
        p01[0xA2] = p.get('si_162', 0x1F)       # both cursors, amplitude,
                                                # Rx CDR bypass, staged set 1
        p01[0x9D] = p.get('flags_157', 0x0F)         # Tx adaptive EQ fail,
                                                     # CDR LOL, LOS, fault
        p01[0x9E] = p.get('flags_158', 0x06)         # Rx CDR LOL, Rx LOS
        p01[0x9F] = p.get('monitors_159', 0x03)      # Vcc and temperature
        # 160.4-3 is the Tx bias scaling factor: 65535 increments of 2 uA stop
        # at 131 mA, so a module biased above that has to scale.
        p01[0xA0] = p.get('monitors_160', 0x07)      # Rx and Tx optical power,
                                                     # Tx bias, x1 scaling

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
        bias_step = 0.002 * _bias_scale(p)
        bias_thr = ([int(round(v / bias_step)) for v in bias] if bias
                    else [int(round(v * 0.002 / bias_step))
                          for v in (0xEA60, 0x1388, 0xC350, 0x2710)])
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
            p04[0x81] = p.get('grid_sup_129', 0x80)   # bit7 FineTuningSupported,
                                                     # bit6 150 GHz, bit5 300 GHz
            for a in range(0x82, 0xAA): p04[a] = 0x00
            # 50 GHz grid channel range ±80
            p04[0x92] = 0xFF; p04[0x93] = 0xB0    # -80
            p04[0x94] = 0x00; p04[0x95] = 0x50    # +80
            # 100 GHz grid channel range ±40
            p04[0x96] = 0xFF; p04[0x97] = 0xD8    # -40
            p04[0x98] = 0x00; p04[0x99] = 0x28    # +40
            # 75 GHz grid channel range ±53: the module advertises this grid,
            # so it has to say which channels are legal on it as well.
            p04[0x9E] = 0xFF; p04[0x9F] = 0xCB    # -53
            p04[0xA0] = 0x00; p04[0xA1] = 0x35    # +53
            # 04h:166-169 continues the channel range table with grid code 9.
            # The C-band span the other grids describe is about ±4000 GHz
            # (50 GHz × ±80, 100 GHz × ±40), so a 300 GHz grid spans ±13.
            if p.get('grid_sup_129', 0x80) & 0x20:
                p04[0xA6] = 0xFF; p04[0xA7] = 0xF3    # -13
                p04[0xA8] = 0x00; p04[0xA9] = 0x0D    # +13
            # Fine tuning: 1 MHz resolution, ±12.5 GHz
            p04[0xBE] = 0x00; p04[0xBF] = 0x01
            v = struct.pack(">h", -12500)
            p04[0xC0] = v[0]; p04[0xC1] = v[1]
            v = struct.pack(">h", 12500)
            p04[0xC2] = v[0]; p04[0xC3] = v[1]
            # 04h:196.6 OutputPowerTxRelativeThresholdsSupported (5.4). Only
            # a module with programmable output power has anything to relate
            # the thresholds to.
            p04[0xC4] = p.get('rel_thr_cap_196', 0x00)
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
        # 145-152 DPConfigLane. AppSel 1 on all eight lanes is only a legal
        # set where App 1 is eight lanes wide: on a module carrying two 4-lane
        # ports, App 1 advertises lane 1 as its only starting lane, so lanes
        # 5-8 running it is a configuration the module's own advertisement
        # forbids. Lay the lanes out the way the descriptors allow instead.
        default_sel = self._default_app_select()
        for i in range(8): p10[0x91 + i] = default_sel[i] << 4
        # 153-173 Staged Control Set 0 signal integrity (Tables 8-83, 8-84).
        # Absent, these read as zero - which says every Rx CDR is bypassed on
        # a module that advertises having one, and that is not a default any
        # retimed module ships with.
        p10[0x99] = p.get('scs_adaptive_eq_tx', 0xFF)   # 153 adaptive Tx eq on
        for a in range(0x9A, 0xA1): p10[a] = 0x00       # 154-160 recall, targets
        p10[0xA1] = p.get('scs_cdr_enable_rx', 0xFF)    # 161 Rx CDRs enabled
        for a in range(0xA2, 0xAA): p10[a] = 0x00       # 162-169 eq targets
        for a in range(0xAA, 0xAE):                     # 170-173 amplitude
            p10[a] = p.get('scs_rx_amplitude', 0x22)    # code 2 on every lane
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
        bias_raw = int(p['tx_bias_ma_nom'] / (0.002 * _bias_scale(p))) & 0xFFFF
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
        for i in range(8): p11[0xCE + i] = default_sel[i] << 4
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
            # 12h:216-217 (Table 8-109): the relative supervision offsets, not
            # per lane - 7.5.3 takes the view that a meaningful monitoring
            # window is a property of the optics, not of the lane.
            rel = p.get('rel_thr_offsets_216', None)
            if rel is not None:
                p12[0xD8], p12[0xD9] = rel
            # 12h:128-135.1 enables it per media lane. A module keeps what the
            # host last programmed, so a profile may come up with it already on
            # for some lanes - which is the state worth demonstrating.
            for i, on in enumerate(p.get('rel_thr_enabled_lanes', ())):
                if on:
                    p12[0x80 + i] |= 0x02
            for i in range(8): p12[0xDE + i] = 0x00    # status: locked, not tuning
            for i in range(8): p12[0xE7 + i] = 0x00    # flags clear
            regs[0x12] = p12

        # ==== Page 13h — Diagnostic Controls ====
        p13 = {}
        # 128-142: what this module can actually do. A module that accepts
        # every pattern and every loopback while advertising none of them is
        # not a module anyone can test a host against.
        # A host has to cope with less capable modules too, so one profile
        # advertises the four loopback types without per-lane granularity and
        # without holding a host and a media loopback at once - which is a
        # perfectly ordinary thing for a short-reach module to say.
        p13[0x80] = p.get('loopback_caps', 0x7F)
        p13[0x81] = 0x7C        # gating <=2 ms, results, periodic updates,
                                # per-lane timers, auto-restart
        # 13h:130 (Table 8-113): which DiagnosticsSelector values report
        # anything. 0x00 said this module reports no BER, no error
        # counts and no SNR - while every one of those panels showed
        # numbers read out of the window anyway.
        p13[0x82] = p.get('diag_reporting_130', 0x33)
        p13[0x83] = 0x00        # generation/checking locations
        # Patterns 0,1 (PRBS31Q/31), 6,7 (PRBS13Q/13), 8,9 (PRBS9Q/9),
        # 10,11 (PRBS7Q/7) and 12 (SSPRQ). The 23- and 15-bit patterns are
        # not offered, which is typical and gives the host something to hide.
        for a in (0x84, 0x86, 0x88, 0x8A):      # 132/134/136/138: IDs 0-7
            p13[a] = 0xC3
        for a in (0x85, 0x87, 0x89, 0x8B):      # 133/135/137/139: IDs 8-15
            p13[a] = 0x1F
        for a in range(0x8C, 0x8F): p13[a] = 0x00
        for base in [0x90, 0x98, 0xA0, 0xA8]:
            for off in range(8): p13[base + off] = 0x00
        p13[0xB4] = 0; p13[0xB5] = 0; p13[0xB6] = 0; p13[0xB7] = 0
        regs[0x13] = p13

        # ==== Page 14h — Diagnostic Results ====
        p14 = {}
        p14[0x80] = 0x00
        p14[0x84] = 0x00                    # LossOfReferenceClockFlag
        p14[0x86] = 0x00; p14[0x87] = 0x00  # PatternCheckGatingComplete
        p14[0x88] = 0x00; p14[0x89] = 0x00  # PatternGeneratorLOL
        p14[0x8A] = 0x00; p14[0x8B] = 0x00  # PatternCheckerLOL
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

            adv174 = p.get('pages_ext_174', 0x00)
            p60 = {0x80: p.get('default_polarity_tx', 0),
                   0x81: p.get('default_polarity_rx', 0),
                   # Table 8-188: Rx/Tx/DpRx/DpTx supported are bits 7-4;
                   # bits 3-0 are reserved, so 0x0F advertised nothing at all.
                   0x82: 0xF0}
            # Page 0Ch's map is built from what regs actually holds, so a page
            # served but not advertised in 01h:174 would put the two
            # advertisements at odds - which is the disagreement Page 0Ch
            # exists to end.
            if adv174 & 0x80:
                regs[0x60] = p60

            p61 = {}
            for lane in range(8):
                # Four distinct seeds: identical ones would let a parser that
                # crosses Rx with Tx read back as if it were correct.
                for base, seed in ((0x80, 1), (0x90, 2), (0xA0, 3), (0xB0, 4)):
                    v = seed + lane
                    p61[base + lane * 2] = (v >> 8) & 0xFF
                    p61[base + lane * 2 + 1] = v & 0xFF
            if adv174 & 0x40:
                regs[0x61] = p61

            p62 = {}
            # Page 62h carries the per-lane Tx thresholds in 0.01 dBm. A lane
            # that has not switched to power-relative supervision (5.4 section
            # 7.5.3) shows the module-wide values, derived from the same raw
            # numbers so the two cannot drift; a lane that has gets them
            # recomputed from its own target power in _refresh_lane_thresholds.
            lane_thr = tuple(_raw_to_dbm_centi(v) for v in tx_thr)
            for lane in range(8):
                off = 0x80 + lane * 8
                # hi alarm, lo alarm, hi warn, lo warn in 0.01 dBm
                for k, val in enumerate(lane_thr):
                    v = val & 0xFFFF
                    p62[off + k * 2] = (v >> 8) & 0xFF
                    p62[off + k * 2 + 1] = v & 0xFF
            if adv174 & 0x20:
                regs[0x62] = p62
                self._absolute_tx_thr = lane_thr

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

        # 6.3.2: the module sets ModuleStateChangedFlag on entering a new state.
        # It is a Flag, so it latches until read - a module that reset and came
        # back between two polls is otherwise indistinguishable from one that
        # never moved.
        if self._module_state != self._last_module_state:
            self._registers[None][0x08] =                 self._registers[None].get(0x08, 0) | 0x01
            self._last_module_state = self._module_state
        self._registers[None][0x03] = (self._module_state << 1) | 0x01

        # A path being taken down walks DPTxTurnOff -> DPDeinit ->
        # DPDeactivated rather than arriving at the bottom instantly.
        if self._deinit_time > 0:
            dt = now - self._deinit_time
            state = 0x6 if dt < 0.08 else (0x3 if dt < 0.16 else 0x1)
            for lane in range(8):
                if (self._deinit_mask >> lane) & 1:
                    self._dp_lane_states[lane] = state
            if state == 0x1:
                self._deinit_time, self._deinit_mask = 0.0, 0x00

        # DataPath state machine (ApplyDataPath)
        if self._apply_time > 0 and self._reset_time == 0:
            dt = now - self._apply_time
            if (not self._apply_hot and not self._apply_provision_only
                    and dt >= 0.15):
                # 8.14.7 clears the bits "while in DPSM state DPInit", so
                # what matters is that the transit happened - which is any
                # cycle, whatever Lower 02h says about the intervention-free
                # procedures. Clearing from inside the DPInit branch alone
                # would depend on a read landing in a 150 ms window; miss it
                # and the flag would claim a commissioning was still pending
                # on a Data Path that had already been through DPInit.
                self._clear_dp_init_pending()
            if self._apply_hot:
                # 8.13.3.1: ApplyImmediate is Provision-and-Commission - the
                # staged set goes straight into hardware and the Data Path
                # never leaves the state it is in. No transient, so no
                # DPStateChangedFlag either.
                if dt >= 0.5:
                    self._apply_time = 0
                    self._commit_apply()
            elif self._apply_provision_only:
                # Provision only (Table 6-4): the result is reported and
                # DPInitPending is left set, but no lane changes state.
                if dt >= 0.15:
                    self._apply_time = 0
                    self._commit_apply()
            elif dt < 0.15:
                for i in range(8):
                    if not self._apply_selects(i):
                        continue
                    if not ((self._tx_disable_mask >> i) & 1):
                        self._dp_lane_states[i] = 0x2      # DPInit
            elif dt < 0.3:
                # Figure 6-5: DPInit completes into DPInitialized, a steady
                # state where the path is up but the Tx is not turned on. It
                # sat between DPInit and DPTxTurnOn in the state machine and
                # nowhere at all in this mock, so nothing ever showed it.
                for i in range(8):
                    if not self._apply_selects(i):
                        continue
                    if not ((self._tx_disable_mask >> i) & 1):
                        self._dp_lane_states[i] = 0x7      # DPInitialized
            elif dt < 0.5:
                for i in range(8):
                    if not self._apply_selects(i):
                        continue
                    if not ((self._tx_disable_mask >> i) & 1):
                        self._dp_lane_states[i] = 0x5      # DPTxTurnOn
            else:
                for i in range(8):
                    if not self._apply_selects(i):
                        continue
                    if not ((self._tx_disable_mask >> i) & 1):
                        self._dp_lane_states[i] = 0x4
                    else:
                        self._dp_lane_states[i] = 0x1
                    # 6.3.3: the Flag is set on entry to a lasting steady state
                    # reached through a significant transient - which is what
                    # has just happened, since the path went through DPInit and
                    # DPTxTurnOn to get here. It is a Flag, so it latches until
                    # read: this is the module's record that the path bounced.
                    self._registers[0x11][0x86] = (
                        self._registers[0x11].get(0x86, 0) | (1 << i))
                self._apply_time = 0
                self._commit_apply()

        # Write DP states back to Page 11h:0x80-0x83.
        # 6.2.3.2: AppSel 0000b means the lane "is unused and not part of a
        # Data Path", and "the module always reports a DPDeactivated state for
        # unused lanes". Walking every applied lane up to DPActivated made a
        # lane carrying no Application report a running Data Path - green, and
        # with a tooltip saying the path was up.
        for i in range(8):
            byte_idx = i // 2
            nibble_pos = (i % 2) * 4
            addr = 0x80 + byte_idx
            old = self._registers[0x11].get(addr, 0)
            mask = 0x0F << nibble_pos
            state = 0x1 if self._lane_unused(i) else self._dp_lane_states[i]
            self._registers[0x11][addr] = (old & ~mask) | ((state & 0x0F) << nibble_pos)

        # PRBS LOL flags (lock after 0.3 s). Table 8-138 reports the
        # generators as well as the checkers: a generator that has not locked
        # is not sending the pattern its control registers name, which is a
        # different fault from a checker that cannot find one.
        for key, lol_addr in [('hc', 0x8A), ('mc', 0x8B),
                              ('hg', 0x88), ('mg', 0x89)]:
            t_en = self._prbs_enable_times.get(key, 0)
            if t_en > 0:
                self._registers[0x14][lol_addr] = 0xFF if (now - t_en) < 0.3 else 0x00

    # ------------------------------------------------------------------
    def _refresh_lane_thresholds(self) -> None:
        """7.5.3: a lane using power-relative supervision has its Page 62h
        thresholds derived from its own programmed Tx output power, so they
        move when that power does. A lane that has not enabled it keeps the
        module-wide values, which is why both cases have to be rewritten on
        every pass rather than only the enabled ones.
        """
        p62 = self._registers.get(0x62)
        p12 = self._registers.get(0x12)
        if p62 is None or p12 is None or self._absolute_tx_thr is None:
            return
        # 04h:196.6 not advertised means no lane is under relative
        # supervision, whatever Page 12h happens to hold - so the quads still
        # get rewritten, with the module-wide values. Skipping the pass
        # instead would leave whatever was last computed sitting in Page 62h.
        advertised = bool((self._registers.get(0x04, {}).get(0xC4, 0) >> 6) & 1)
        hi, lo = p12.get(0xD8, 0), p12.get(0xD9, 0)
        # Both offsets are U4 counted from half a dB, so the smallest window a
        # module can express is nominal +/- 0.5 dB.
        d_hi_alarm = (1 + ((hi >> 4) & 0x0F)) * 0.5
        d_hi_warn = (1 + (hi & 0x0F)) * 0.5
        d_lo_alarm = -(1 + ((lo >> 4) & 0x0F)) * 0.5
        d_lo_warn = -(1 + (lo & 0x0F)) * 0.5
        for lane in range(8):
            off = 0x80 + lane * 8
            if advertised and (p12.get(0x80 + lane, 0) >> 1) & 1:
                a = 0xC8 + lane * 2
                tgt = struct.unpack('>h', bytes([p12.get(a, 0),
                                                 p12.get(a + 1, 0)]))[0] * 0.01
                quad = [int(round((tgt + d) * 100)) for d in
                        (d_hi_alarm, d_lo_alarm, d_hi_warn, d_lo_warn)]
            else:
                quad = list(self._absolute_tx_thr)
            for k, val in enumerate(quad):
                v = val & 0xFFFF
                p62[off + k * 2] = (v >> 8) & 0xFF
                p62[off + k * 2 + 1] = v & 0xFF

    def _regular_reconfig(self) -> bool:
        """Lower 02h: whether ApplyDPInit commissions as well as provisions.

        Table 6-3 covers modules that support the intervention-free
        procedures - there ApplyDPInit on a running Data Path is "copy and
        cycle". Table 6-4 covers the ones that do not: ApplyDPInit is accepted
        in any DPSM state but only provisions, and the commissioning transit
        through DPInit waits for the host. Cycling anyway made every module
        look like the first kind.
        """
        raw = self._registers[None].get(0x02, 0)
        if not ((raw >> 6) & 1):
            return True                      # legacy default: both supported
        return (raw & 0x03) == 0b01

    # Where each Active Control Set signal integrity register comes from:
    # (active address, staged address, nibble-packed).
    _ACS_SI_MAP = (
        (0xD6, 0x99, False),   # AdaptiveInputEqEnableTx, 1 bit per lane
        (0xD9, 0x9C, True),    # HostControlledInputEqTargetTx
        (0xDE, 0xA1, False),   # CDREnableRx
        (0xDF, 0xA2, True),    # OutputEqPreCursorTargetRx
        (0xE3, 0xA6, True),    # OutputEqPostCursorTargetRx
        (0xE7, 0xAA, True),    # OutputAmplitudeTargetRx
    )

    def _provision_si(self, lane: int, explicit: bool) -> None:
        """Fill this lane's entry in Tables 8-104 and 8-105.

        The spec makes ExplicitControl decide where the values come from: set,
        and they "originate from corresponding registers in that Staged
        Control Set"; clear, and they "were determined by the module according
        to the selected Application". A host that leaves the bit clear - which
        is what this tool does - is therefore not running the numbers it
        staged, and only these registers say what it is running.
        """
        for active, staged, nibble in self._ACS_SI_MAP:
            if explicit:
                value = self._lane_value(0x10, staged, lane, nibble)
            else:
                value = self._application_si(active, lane)
            self._set_lane_value(0x11, active, lane, nibble, value)

    def _application_si(self, active: int, lane: int) -> int:
        """What this module picks for a lane it was left to configure itself.

        A demo choice, and a deliberately different one from the staged
        defaults: a module that happened to land on the same numbers would
        hide the very distinction these registers exist to report.
        """
        return self._profile.get('acs_si', {
            0xD6: 1,      # adaptive Tx equalization on
            0xD9: 0,      # so the host-controlled target is not in use
            0xDE: 1,      # Rx CDR enabled
            0xDF: 1,      # a pre-cursor the Application asks for
            0xE3: 0,
            0xE7: 1,      # amplitude code 1
        }).get(active, 0)

    def _lane_value(self, page: int, base: int, lane: int, nibble: bool) -> int:
        regs = self._registers.get(page, {})
        if not nibble:
            return (regs.get(base, 0) >> lane) & 1
        raw = regs.get(base + lane // 2, 0)
        return (raw >> 4) & 0x0F if lane % 2 else raw & 0x0F

    def _set_lane_value(self, page: int, base: int, lane: int, nibble: bool,
                        value: int) -> None:
        regs = self._registers.setdefault(page, {})
        if not nibble:
            regs[base] = (regs.get(base, 0) & ~(1 << lane)) | (
                (value & 1) << lane)
            return
        addr = base + lane // 2
        shift = 4 if lane % 2 else 0
        regs[addr] = (regs.get(addr, 0) & ~(0x0F << shift)) | (
            (value & 0x0F) << shift)

    def _clear_dp_init_pending(self) -> None:
        """8.14.7: "the module clears all DPInitPendingLane<i> bits of a Data
        Path while in DPSM state DPInit"."""
        pending = self._registers[0x11].get(0xEB, 0)
        for lane in range(8):
            if self._apply_selects(lane):
                pending &= ~(1 << lane)
        self._registers[0x11][0xEB] = pending

    def _hot_reconfig(self) -> bool:
        """Lower 02h: whether ApplyImmediate does anything on this module."""
        raw = self._registers[None].get(0x02, 0)
        if not ((raw >> 6) & 1):
            return True                      # legacy default: both supported
        return (raw & 0x03) == 0b10

    def _apply_selects(self, lane: int) -> bool:
        if not ((self._apply_mask >> lane) & 1):
            return False                     # this lane was not selected
        if (self._dp_deinit_mask >> lane) & 1:
            return False                     # held deinitialised by 10h:128
        return self._config_result[lane] == 0x1   # validation failed: no execution

    def _commit_apply(self):
        """Step (4): copy the staged set that passed into the Active Control
        Set and report the result. Shared by both Apply triggers - only the
        Data Path transitions differ between them."""
        for i in range(8):
            if ((self._apply_mask >> i) & 1) and self._config_result[i] == 0x1:
                self._registers[0x11][0xCE + i] = self._config_staged[i]
                self._provision_si(i, self._config_staged[i] & 0x01)
        for lane in range(8):
            if not ((self._apply_mask >> lane) & 1):
                continue                     # unselected lanes keep their status
            a = 0xCA + lane // 2
            shift = 4 if lane % 2 else 0
            self._registers[0x11][a] = (
                (self._registers[0x11].get(a, 0) & ~(0x0F << shift))
                | (self._config_result[lane] << shift))

    def _start_apply(self, mask: int, hot: bool) -> None:
        # Let an Apply already under way reach its result step first.
        self._update_state_machine()
        # CMIS 8.13.3 step (1): a command arriving while any relevant lane
        # still reads ConfigInProgress is aborted "silently (without
        # feedback)" - the module does not restage anything.
        if self._apply_time > 0:
            return
        self._apply_time = time.time()
        self._apply_hot = hot
        self._apply_mask = mask
        self._config_result = self._validate_staged_appsel(mask, subset_ok=hot)
        # Table 6-4: where neither intervention-free procedure is advertised,
        # ApplyDPInit provisions without commissioning - in "Any DPSM state",
        # so the state is not part of the question. Commissioning such a path
        # is the stepwise procedure, which arrives through the DPDeinit
        # release below rather than through this trigger. Settled here rather
        # than read live, because the cycle changes the states around it.
        self._apply_provision_only = not hot and not self._regular_reconfig()
        # Validation and execution both act on the Staged Control Set as it
        # stood when the Apply arrived. Reading 10h again at the completion
        # step would commit whatever was staged since.
        self._config_staged = [self._registers[0x10].get(0x91 + i, 0x10)
                               for i in range(8)]
        # 8.14.7: DPInitPending is set by the Provision, so it stands from
        # here until a transit through DPInit clears it. ApplyImmediate
        # commits to hardware itself, so it leaves nothing pending.
        if not hot:
            pending = self._registers[0x11].get(0xEB, 0)
            for lane in range(8):
                if ((mask >> lane) & 1) and self._config_result[lane] == 0x1:
                    pending |= 1 << lane
            self._registers[0x11][0xEB] = pending
        for lane in range(8):
            if not ((mask >> lane) & 1):
                continue
            a = 0xCA + lane // 2
            shift = 4 if lane % 2 else 0
            self._registers[0x11][a] = (
                (self._registers[0x11].get(a, 0) & ~(0x0F << shift))
                | (0x0C << shift))          # ConfigInProgress

    def _write_targets(self):
        """Where an upper-memory write lands: the selected bank, or every bank
        of a lane-banked page when bank broadcast is on.

        Table 8-11: with BankBroadcastEnable set, a write to a control
        register in any bank "is executed as a bank broadcast - a virtually
        simultaneous and atomic WRITE of the same value to the same register
        and the same page, in all supported banks", and a read from any bank
        must then return what was broadcast.
        """
        page = self._current_page
        selected = ((page, self._current_bank)
                    if (page, self._current_bank) in self._registers else page)
        if not self._bank_broadcast():
            return [self._registers.setdefault(selected, {})]
        targets = self._page_dicts(page)
        return targets or [self._registers.setdefault(selected, {})]

    def _bank_broadcast(self) -> bool:
        """Lower 0x1A.7, and only where 01h:156.7 advertises it - a module
        that does not advertise the control does not act on the bit."""
        p01 = self._registers.get(0x01, {})
        if not ((p01.get(0x9C, 0) >> 7) & 1):
            return False
        return bool((self._registers.get(None, {}).get(0x1A, 0) >> 7) & 1)

    def _page_dicts(self, page: int):
        """Every bank's copy of one page, so banked lanes do not go stale."""
        return [d for key, d in self._registers.items()
                if key == page or (isinstance(key, tuple) and key[0] == page)]

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
        # 10h:132 OutputSquelchForceTx and 10h:138 OutputDisableRx are stored
        # by the plain write path, so they are read back here rather than
        # intercepted - they steer nothing but the output status.
        p10 = self._registers.get(0x10, {})
        force_squelch_tx = p10.get(0x84, 0)
        output_disable_rx = p10.get(0x8A, 0)
        out_tx, out_rx = 0, 0

        for lane in range(8):
            phase = lane * math.pi / 4
            tx_disabled = bool((self._tx_disable_mask >> lane) & 1)
            dp_active = self._dp_lane_states[lane] == 0x4
            # Table 8-95: valid means the module is really sending a signal.
            # An Activated lane whose output is disabled or force-squelched is
            # not, and no other register in the map says so.
            if (dp_active and not tx_disabled
                    and not ((force_squelch_tx >> lane) & 1)):
                out_tx |= 1 << lane
            if dp_active and not ((output_disable_rx >> lane) & 1):
                out_rx |= 1 << lane

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
            bias_val = int(bias_ma / (0.002 * _bias_scale(p))) & 0xFFFF
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

        # 8.14.2: the Rx side latches a Flag on every change of 11h:132, so a
        # squelch that came and went between two polls still leaves a trace.
        changed = out_rx ^ self._rx_output_valid
        self._rx_output_valid = out_rx
        for page_dict in self._page_dicts(0x11):
            page_dict[0x84] = out_rx
            page_dict[0x85] = out_tx
            if changed:
                page_dict[0x99] = page_dict.get(0x99, 0) | changed

        self._refresh_lane_thresholds()
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
                if not self._tuning_accepted[lane]:
                    continue        # refused: the laser has not moved
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
            if 0x80 in span:                                        # DPDeinit
                self._set_dp_deinit(data[0x80 - register])
            if 0x82 in span and self._current_bank == 0:             # OutputDisableTx
                # The dynamic model covers the eight lanes of bank 0; the
                # other banks keep the values built for them. Taking this mask
                # from whichever bank was written last let a write aimed at
                # lane 17 zero the readings of lane 1.
                self._tx_disable_mask = data[0x82 - register]
            if 0x8F in span and data[0x8F - register]:              # ApplyDPInit
                self._start_apply(data[0x8F - register], hot=False)
            if 0x90 in span and data[0x90 - register]:              # ApplyImmediate
                # "When ApplyImmediate is not supported, WRITE access to it is
                # ignored" - silently, which is why the host has to read Lower
                # 02h before offering the trigger at all.
                if self._hot_reconfig():
                    self._start_apply(data[0x90 - register], hot=True)
        elif self._current_page == 0x12:
            span = range(register, register + len(data))
            touched = {'channel': any(a in span for a in range(0x80, 0x98)),
                       'fine':    any(a in span for a in range(0x98, 0xA8)),
                       'power':   any(a in span for a in range(0xC8, 0xD8))}
            if any(touched.values()):
                # The write lands first; the module judges what it now holds.
                # A module without Page 12h has no such dict; the generic
                # write path uses setdefault for exactly this reason, and
                # indexing here raised KeyError(18) out of the backend
                # instead - which reached the caller as a 500 saying "18".
                p12 = self._registers.setdefault(0x12, {})
                for a, b in zip(span, data):
                    p12[a] = b
                self._judge_tuning(touched)
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
            'tx_bias': int(bias_ma / (0.002 * _bias_scale(self._profile))),
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

    # Advertised on Page 04h:130-165, an S16 low/high pair per grid code.
    _GRID_RANGE_BASE = 0x82

    def _judge_tuning(self, touched=None):
        """Answer a tuning request in the Page 12h Flags (Table 8-109).

        A module does not silently tune wherever it is told. A channel outside
        the advertised range for the selected grid, a target power outside the
        programmable range, or a fine-tuning offset beyond what was advertised
        each raise their own latched Flag, and the laser stays where it was.
        """
        p04 = self._registers.get(0x04, {})
        p12 = self._registers[0x12]

        def s16(page, addr):
            return struct.unpack(">h", bytes([page.get(addr, 0),
                                              page.get(addr + 1, 0)]))[0]

        pwr_lo, pwr_hi = s16(p04, 0xC6), s16(p04, 0xC8)
        fine_lo, fine_hi = s16(p04, 0xC0), s16(p04, 0xC2)

        touched = touched or {'channel': True, 'fine': True, 'power': True}
        summary = 0
        for lane in range(8):
            grid_byte = p12.get(0x80 + lane, 0x50)
            grid_code = (grid_byte >> 4) & 0x0F
            flags = 0

            if touched['channel']:
                base = self._GRID_RANGE_BASE + grid_code * 4
                ch_lo, ch_hi = s16(p04, base), s16(p04, base + 2)
                channel = s16(p12, 0x88 + lane * 2)
                if ch_lo == 0 and ch_hi == 0:
                    # A grid the module never advertised cannot be tuned to.
                    if channel:
                        flags |= 1 << 3      # TuningNotAcceptedFlagTx
                elif not (ch_lo <= channel <= ch_hi):
                    flags |= 1 << 2          # InvalidChannelNumberFlagTx

            if touched['fine'] and (grid_byte & 0x01):
                fine = s16(p12, 0x98 + lane * 2)
                if not (fine_lo <= fine <= fine_hi):
                    flags |= 1 << 4          # FineTuningOutOfRangeFlagTx

            if touched['power']:
                target = s16(p12, 0xC8 + lane * 2)
                if not (pwr_lo <= target <= pwr_hi):
                    flags |= 1 << 5          # TargetOutputPowerOORFlagTx

            if flags == 0:
                flags |= 1 << 0              # TuningCompleteFlagTx
            self._tuning_accepted[lane] = (flags & ~1) == 0

            p12[0xE7 + lane] = p12.get(0xE7 + lane, 0) | flags
            if p12[0xE7 + lane]:
                summary |= 1 << lane
        p12[0xE6] = summary

    def _set_dp_deinit(self, mask: int) -> None:
        """10h:128 (Table 8-78): 1b deinitialises the Data Path of that lane.

        The module evaluates this byte only in ModuleReady, so a host can set
        every bit while in ModuleLowPwr to stop the Data Paths auto-starting.
        A lane released from deinit walks back up through DPInit, which is why
        it borrows the Apply machinery rather than snapping to Activated.
        """
        if self._module_state != 0b011:          # not ModuleReady
            self._dp_deinit_mask = mask
            return
        was, self._dp_deinit_mask = self._dp_deinit_mask, mask
        released = was & ~mask                   # 1 -> 0: bring these back up
        taken = 0
        for lane in range(8):
            if (mask >> lane) & 1:
                if self._dp_lane_states[lane] != 0x1:
                    # Figure 6-5: a path leaves DPActivated through
                    # DPTxTurnOff and DPDeinit rather than arriving at the
                    # bottom at once. Marking the lane is all that happens
                    # here; the walk below owns the states, and setting one
                    # here as well would be overwritten before any read.
                    taken |= 1 << lane
                    self._registers[0x11][0x86] = \
                        self._registers[0x11].get(0x86, 0) | (1 << lane)
        if taken:
            self._deinit_time, self._deinit_mask = time.time(), taken
        if released:
            self._update_state_machine()         # let any Apply finish first
            self._apply_time = time.time()
            self._apply_mask = released
            self._apply_provision_only = False
            # Releasing a deinit hold is not an Apply trigger: the module
            # restarts the lanes it was holding, which is a subset of a Data
            # Path only because the host chose to hold a subset.
            self._config_result = self._validate_staged_appsel(released,
                                                               subset_ok=True)
            self._config_staged = [self._registers[0x10].get(0x91 + i, 0x10)
                                   for i in range(8)]

    def _default_app_select(self):
        """An AppSel code per host lane that the descriptors actually allow.

        Walking the lanes and taking the first Application whose
        HostLaneAssignmentOptions offers this lane as a starting point gives
        the same eight lanes of App 1 on a DR8, and lanes 1-4 of App 1 beside
        lanes 5-8 of App 2 on a module built from two 4-lane ports.
        """
        apps = self._profile['app_descriptors']
        sel, lane = [], 0
        while lane < 8:
            for n, desc in enumerate(apps, start=1):
                width = (desc[2] >> 4) & 0x0F
                if width and (desc[3] >> lane) & 1 and lane + width <= 8:
                    sel += [n] * width
                    lane += width
                    break
            else:
                sel.append(0)                # no Application can start here
                lane += 1
        return sel

    def _lane_unused(self, lane: int) -> bool:
        """Whether the Active Control Set leaves this host lane out of a Data
        Path (AppSelCode 0000b, 11h:206-213)."""
        return not ((self._registers[0x11].get(0xCE + lane, 0x10) >> 4) & 0x0F)

    def _staged_datapaths(self):
        """Split the Staged Control Set into the Data Paths it describes.

        6.2.4.3: every lane of an Application instance carries the same AppSel
        code, and the instance must be "completely allocated on lanes supported
        for that Application" - a block as wide as the descriptor's
        HostLaneCount, starting on a lane its HostLaneAssignmentOptions bitmap
        allows. A run of lanes therefore holds a whole number of instances, or
        it is not a valid allocation at all. Lanes staged AppSel 0 are unused
        and belong to no Data Path.

        Returns (lanes, code, appsel) where code is the Table 8-101 result
        the group has earned from the staged set alone.
        """
        staged = [(self._registers[0x10].get(0x91 + i, 0x10) >> 4) & 0x0F
                  for i in range(8)]
        apps = self._profile['app_descriptors']
        groups, i = [], 0
        while i < 8:
            code, j = staged[i], i
            while j < 8 and staged[j] == code:
                j += 1
            run = list(range(i, j))
            i = j
            if code == 0:
                groups += [([lane], 0x1, 0) for lane in run]
            elif code > len(apps):
                groups.append((run, 0x3, code))
            else:
                width = (apps[code - 1][2] >> 4) & 0x0F
                allowed = apps[code - 1][3]
                k = 0
                while k < len(run):
                    if (width and k + width <= len(run)
                            and (allowed >> run[k]) & 1):
                        groups.append((run[k:k + width], 0x1, code))
                        k += width
                    else:
                        # Whatever is left over cannot start an instance here,
                        # so those are the lanes to name - not the whole run,
                        # which may hold perfectly good Data Paths ahead of it.
                        groups.append((run[k:], 0x4, code))
                        break
        return groups

    def _validate_staged_appsel(self, mask, subset_ok=False):
        """Per-lane ConfigStatus nibble for the Staged Control Set (Table 8-101).

        A module only accepts an AppSelCode it actually advertises; picking one
        it never announced is ConfigRejectedInvalidAppSel (3h). AppSelCode 0
        means "no application" - deprovisioning a lane is always legal. A code
        the module does advertise still has to land on a set of lanes the
        Application can occupy, or it is ConfigRejectedInvalidDataPath (4h).

        8.14.5: configuration procedures act on entire Data Paths, "with the
        exception of hot reconfiguration of SI attributes by ApplyImmediate",
        so a regular Apply that triggers only some lanes of a Data Path is
        ConfigRejectedPartialDataPath (7h). Table 8-101 does not order the
        codes - "the reporting priority of the result status codes is not
        specified" - and one value is reported on every lane of the group.

        Every group is judged, triggered or not: which lanes actually take a
        new status is _commit_apply's business, and it already answers it from
        the same mask. Deciding it twice is how the two answers drift apart.
        """
        result = [0x1] * 8
        for lanes, code, appsel in self._staged_datapaths():
            if code == 0x1 and appsel and self._invalid_si(lanes):
                code = 0x5
            elif (code == 0x1 and not subset_ok
                    and not all((mask >> lane) & 1 for lane in lanes)):
                code = 0x7
            elif code == 0x1 and self._needs_deactivated(lanes, appsel):
                # 6.2.4.3 states the precondition twice: a lane "can be
                # reconfigured to become unused only when the Data Path is in
                # the DPDeactivated state", and "the host can change the width
                # of a Data Path only while in the DPDeactivated state".
                # Table 6-3 still allows ApplyDPInit on a running path - what
                # it does not allow is these two changes.
                if any(self._dp_lane_states[lane] != 0x1 for lane in lanes):
                    code = 0x6
            for lane in lanes:
                result[lane] = code
        return result

    def _invalid_si(self, lanes) -> bool:
        """Whether the staged signal integrity settings are ones this module
        said it could carry out.

        Table 8-53 publishes a maximum for each host-controlled target and the
        set of Rx output amplitude codes that exist; asking for more than the
        module advertises is ConfigRejectedInvalidSI (5h). Only the controls
        01h:161-162 announces are judged - a target register a module does not
        implement holds nothing it has to honour.
        """
        p01 = self._registers.get(0x01, {})
        b153, b154 = p01.get(0x99, 0), p01.get(0x9A, 0)
        # Read straight from the advertisement bytes, the way the bank
        # broadcast and hot reconfiguration gates do: a backend decoding its
        # own registers keeps this file free of cmis_registers.
        b161, b162 = p01.get(0xA1, 0), p01.get(0xA2, 0)
        eq = (b162 >> 3) & 0x03                 # 162.4-3 RxOutputEqControl
        checks = []
        if (b161 >> 2) & 1:                     # 161.2 host-controlled Tx eq
            checks.append((0x9C, b153 & 0x0F))
        if eq in (1, 3):
            checks.append((0xA2, b154 & 0x0F))          # pre-cursor max
        if eq in (2, 3):
            checks.append((0xA6, (b154 >> 4) & 0x0F))   # post-cursor max
        p10 = self._registers.get(0x10, {})
        for base, ceiling in checks:
            for lane in lanes:
                raw = p10.get(base + lane // 2, 0)
                value = (raw >> 4) & 0x0F if lane % 2 else raw & 0x0F
                if value > ceiling:
                    return True
        if (b162 >> 2) & 1:                     # 162.2 amplitude control
            levels = [i for i in range(4) if (b153 >> (4 + i)) & 1]
            for lane in lanes:
                raw = p10.get(0xAA + lane // 2, 0)
                code = (raw >> 4) & 0x0F if lane % 2 else raw & 0x0F
                if code not in levels:
                    return True
        return False

    def _needs_deactivated(self, lanes, appsel) -> bool:
        """Whether this group's change is one 6.2.4.3 allows only from
        DPDeactivated: freeing a lane that is in use, or moving a Data Path to
        an Application of a different width."""
        apps = self._profile['app_descriptors']
        active = [(self._registers[0x11].get(0xCE + lane, 0x10) >> 4) & 0x0F
                  for lane in lanes]
        if not appsel:
            return any(active)               # in use, and asked to become unused

        def width(code):
            return ((apps[code - 1][2] >> 4) & 0x0F
                    if 0 < code <= len(apps) else 0)
        # A lane that is unused today is already deactivated, so provisioning
        # one is never the case this rule is about.
        return any(a and width(a) != width(appsel) for a in active)

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
        0x11: range(0x86, 0x9A),   # 134-153, DPStateChanged..RxOutputChanged
        # Table 8-138: the Page 14h diagnostic flags are RO/COR too. A pattern
        # checker that lost lock for a moment during a long run is exactly the
        # thing a long run is for, and it is gone one read later.
        0x14: range(0x8A, 0x8C),
        0x12: range(0xE6, 0xEF),
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
            for page_dict in self._write_targets():
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


@register_backend("mock_24lane")
class Mock24LaneBackend(MockBackend):
    PROFILE = _XD24


@register_backend("mock_coherent_zr")
class MockCoherentZRBackend(MockBackend):
    PROFILE = _ZR_800G


@register_backend("mock_fr4x2")
class MockFR4x2Backend(MockBackend):
    PROFILE = _FR4X2_800G
    BACKEND_NAME = 'mock_fr4x2'
