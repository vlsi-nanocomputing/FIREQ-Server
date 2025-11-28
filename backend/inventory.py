# file: fireq_orchestrator/hardware/inventory.py
"""
Hardware IP Discovery and Clock Validation.

This module provides the HardwareInventory class which acts as an abstraction
layer over the raw PYNQ Overlay. It:
- Inspects the loaded bitstream to identify available resources
- Validates that RF clocks are correctly locked and synchronized
- Provides access to discovered IPs (generators, acquisitions, triggers)
"""

import logging
from typing import List, Optional, Any, Dict, TYPE_CHECKING

# Robust import handling for Low-Level Drivers with Type Checking safety
if TYPE_CHECKING:
    try:
        from ...FIREQ_LL_API.overlay_driver import FIREQ_SoC
        from ...FIREQ_LL_API import GeneratorDriver, TriggerGeneratorDriver, AcquistionDriver
    except ImportError:
        # Fallback for typing if libraries are missing in IDE/Linter
        FIREQ_SoC = Any
        GeneratorDriver = Any
        TriggerGeneratorDriver = Any
        AcquistionDriver = Any


class HardwareInventory:
    """
    Handles Hardware IP Discovery and Clock Validation.

    This class acts as an abstraction layer over the raw PYNQ Overlay.
    It inspects the loaded bitstream (via FIREQ_SoC) to identify available
    resources (Generators, ADCs, Triggers) and validates that the RF
    clocks are correctly locked and synchronized.
    
    All hardware specifications (sample rates, Nyquist frequencies, bandwidth)
    are discovered dynamically from the RF-DC configuration during initialization,
    ensuring portability across different boards and hardware revisions.
    
    The object is fully initialized upon construction or raises an exception
    if any discovery step fails.
    
    Attributes:
        gens: List of GeneratorDriver instances
        acqs: List of AcquistionDriver instances
        trig: TriggerGeneratorDriver instance
        dma: AXI DMA controller
        switch: AXI Switch (optional, may be None)
        rf: RF-DC controller
        specs: Dict containing all hardware specifications (sample rates, Nyquist
               frequencies, per-tile details)
    
    Usage:
        >>> hw = HardwareInventory(overlay, logger)  # All validation happens here
        >>> dac_nyquist = hw.specs['dac_nyquist']
        >>> adc_sr = hw.specs['adc_sr']
        >>> adc_parallelism = hw.specs['adc_parallelism']  # 8 samples/cycle
    """

    def __init__(self, overlay: Any, logger: Optional[logging.Logger] = None):
        """
        Initialize the Inventory by scanning the Overlay.

        :param overlay: The loaded PYNQ overlay (should be an instance of FIREQ_SoC)
        :param logger: Optional logger instance
        :raises RuntimeError: If critical IPs are missing from the bitstream
        """
        self.logger = logger or logging.getLogger(__name__)
        
        self.logger.debug("HardwareInventory: Scanning overlay for IPs...")

        # Runtime check for the overlay type
        type_name = type(overlay).__name__
        if type_name != 'FIREQ_SoC' and type_name != 'MockOverlay':
            self.logger.warning(
                f"Warning: Overlay type is '{type_name}', expected 'FIREQ_SoC'. "
                f"Discovery might fail."
            )

        # 1. Retrieve IPs from FIREQ_SoC pre-populated lists
        self.logger.debug("  Phase 1: Discovering custom IPs...")
        self.gens: List[Any] = getattr(overlay, '_generation_ips', [])
        self.acqs: List[Any] = getattr(overlay, '_readout_ips', [])
        triggers: List[Any] = getattr(overlay, '_trigger_ips', [])

        # 2. Critical Resource Validation
        if not self.gens:
            raise RuntimeError("Critical: No Signal Generators found in the overlay.")
        self.logger.debug(f"    [OK] Found {len(self.gens)} Signal Generator(s)")
        
        if not self.acqs:
            raise RuntimeError("Critical: No Acquisition IPs found in the overlay.")
        self.logger.debug(f"    [OK] Found {len(self.acqs)} Acquisition IP(s)")
        
        if not triggers:
            raise RuntimeError("Critical: No Trigger Generator found in the overlay.")
        self.logger.debug(f"    [OK] Found Trigger Generator")

        # We assume a single centralized trigger manager
        self.trig = triggers[0]

        # 3. Retrieve Infrastructure IPs (DMA, Switch, RF-DC)
        self.logger.debug("  Phase 2: Discovering infrastructure IPs...")
        self.dma = getattr(overlay, 'axi_dma_0', None)
        self.switch = getattr(overlay, 'axis_switch_0', None)
        self.rf = getattr(overlay, 'usp_rf_data_converter_0', None)

        if not self.dma:
            raise RuntimeError("Critical: 'axi_dma_0' (DMA Controller) not found.")
        self.logger.debug(f"    [OK] Found AXI DMA Controller")
        
        if not self.rf:
            raise RuntimeError("Critical: 'usp_rf_data_converter_0' (RF-DC) not found.")
        self.logger.debug(f"    [OK] Found RF-DC Controller")

        if not self.switch:
            self.logger.warning("AXI Switch not found. Dynamic routing might be unavailable.")
        else:
            self.logger.debug(f"    [OK] Found AXI Switch")

        # 4. Initialize hardware specifications (will be populated by _validate_clocks)
        self.specs: Dict[str, Any] = {}
        
        # 5. Discover and validate clocks, populate specs
        self._validate_clocks()
        self._populate_specs()

    def _validate_clocks(self):
        """
        Private method: Validate synchronization and detect Sample Rates of active RF Tiles.
        
        Called during __init__. Iterates through all DAC and ADC tiles to:
        - Verify PLL lock status
        - Read sample rates
        - Ensure consistency across tiles
        - Store per-tile specifications for later use
        
        Raises:
            RuntimeError: If no active tiles found or clock mismatch detected
        """
        self.logger.debug("Validating RF clocks and discovering sample rates...")
        
        # --- DAC Validation and Discovery ---
        self.logger.debug("  Scanning DAC tiles...")
        found_dac_sr = None
        self._dac_sr: Optional[float] = None
        self._dac_tile_specs: List[Dict[str, Any]] = []
        
        for i, tile in enumerate(self.rf.dac_tiles):
            try:
                # Read PLL lock status for debug
                lock_stat = getattr(tile, 'PLLLockStatus', 'Unknown')
                
                # Read Sample Rate - fails if tile is inactive
                sr = tile.PLLConfig['SampleRate'] * 1e9
                
                # Check for consistency across tiles
                if found_dac_sr and abs(sr - found_dac_sr) > 1e3:
                    raise RuntimeError(
                        f"DAC Clock Mismatch! Tile {i} ({sr/1e9:.2f}G) "
                        f"differs from others."
                    )
                found_dac_sr = sr
                
                # Store per-tile spec
                num_blocks = len(getattr(tile, 'blocks', []))
                if num_blocks == 0:
                    num_blocks = 2  # Fallback for Gen 3
                
                self._dac_tile_specs.append({
                    'index': i,
                    'sample_rate': sr,
                    'pll_lock_status': lock_stat,
                    'num_blocks': num_blocks,
                })
                
                self.logger.debug(f"    [OK] DAC Tile {i}: {sr/1e9:.3f} GSPS (Lock: {lock_stat})")
                
            except Exception as e:
                # --- FIX: Se è un errore critico (RuntimeError per Mismatch), lascialo esplodere! ---
                if isinstance(e, RuntimeError):
                    raise e
                # ------------------------------------------------------------------------------------

                # Tile not configured or not accessible
                self.logger.debug(
                    f"    [SKIP] DAC Tile {i}: Not active or PLL read failed ({type(e).__name__})"
                )

        if found_dac_sr is None:
            raise RuntimeError("No active DAC tiles found in the RF-DC.")
        self._dac_sr = found_dac_sr
        self.logger.debug(f"  [OK] DAC sample rate: {self._dac_sr/1e9:.3f} GSPS")

        # --- ADC Validation and Discovery ---
        self.logger.debug("  Scanning ADC tiles...")
        found_adc_sr = None
        self._adc_sr: Optional[float] = None
        self._adc_tile_specs: List[Dict[str, Any]] = []
        
        for i, tile in enumerate(self.rf.adc_tiles):
            try:
                lock_stat = getattr(tile, 'PLLLockStatus', 'Unknown')
                sr = tile.PLLConfig['SampleRate'] * 1e9

                if found_adc_sr and abs(sr - found_adc_sr) > 1e3:
                    raise RuntimeError(
                        f"ADC Clock Mismatch! Tile {i} ({sr/1e9:.2f}G) "
                        f"differs from others."
                    )
                found_adc_sr = sr
                
                # Store per-tile spec
                num_blocks = len(getattr(tile, 'blocks', []))
                if num_blocks == 0:
                    num_blocks = 2  # Fallback for Gen 3
                
                self._adc_tile_specs.append({
                    'index': i,
                    'sample_rate': sr,
                    'pll_lock_status': lock_stat,
                    'num_blocks': num_blocks,
                })
                
                self.logger.debug(f"    [OK] ADC Tile {i}: {sr/1e9:.3f} GSPS (Lock: {lock_stat})")

            except Exception as e:
                # --- FIX: Se è un errore critico (RuntimeError per Mismatch), lascialo esplodere! ---
                if isinstance(e, RuntimeError):
                    raise e
                # ------------------------------------------------------------------------------------

                self.logger.debug(
                    f"    [SKIP] ADC Tile {i}: Not active or PLL read failed ({type(e).__name__})"
                )

        if found_adc_sr is None:
            raise RuntimeError("No active ADC tiles found in the RF-DC.")
        self._adc_sr = found_adc_sr
        self.logger.debug(f"  [OK] ADC sample rate: {self._adc_sr/1e9:.3f} GSPS")

        self.logger.info(
            f"Clocks Validated. DAC: {self._dac_sr/1e9:.3f} GSPS, "
            f"ADC: {self._adc_sr/1e9:.3f} GSPS"
        )

    def _populate_specs(self):
        """
        Private method: Populate self.specs with all hardware specifications.
        
        Called during __init__ after _validate_clocks(). Builds a comprehensive
        dict with sample rates, Nyquist frequencies, bandwidth, and per-tile specs.
        
        This method must be called after _validate_clocks() to ensure all
        RF tile information is available.
        """
        # Compute derived values
        dac_nyquist = self._dac_sr / 2
        adc_nyquist = self._adc_sr / 2
        
        # Build specs dict
        self.specs = {
            # Primary sample rates (discovered from RF-DC)
            'dac_sr': self._dac_sr,
            'adc_sr': self._adc_sr,
            
            # Derived Nyquist frequencies
            'dac_nyquist': dac_nyquist,
            'adc_nyquist': adc_nyquist,
            
            # ADC digital characteristics (from firmware)
            # See thesis section 9.2.2: "Parallelism: 8 samples/cycle, Decimation: 8x"
            'adc_parallelism': 8,        # Samples per clock cycle
            'adc_decimation_factor': 8,  # FIR decimation ratio
            
            # ADC output format (from thesis section 9.3.3)
            # Accumulated: 64-bit (two 32-bit I/Q values)
            # Other modes: 32-bit packed
            'adc_accumulated_output_width_bits': 64,  # Bits per accumulated sample
            
            # DAC digital characteristics (from firmware)
            # See thesis section 8.3.1: "Parallelism: 16 samples/cycle"
            'dac_parallelism': 16,       # Samples per clock cycle
            
            # DAC RF-DC characteristics (from thesis section 4.2.3)
            # RFSoC Gen 3 supports Nyquist zones 1-4
            'dac_max_nyquist_zone':2,   # Maximum supported Nyquist zone
            
            # Per-tile details
            'dac_tiles': self._dac_tile_specs,
            'adc_tiles': self._adc_tile_specs,
            
            # Convenience: Nyquist zones for DAC
            'dac_nyquist_zone_1': (0, dac_nyquist),
            'dac_nyquist_zone_2': (dac_nyquist, self._dac_sr),
        }
        
        self.logger.debug(
            f"Hardware specs populated: DAC {self._dac_sr/1e9:.3f}G, "
            f"ADC {self._adc_sr/1e9:.3f}G"
        )


__all__ = ['HardwareInventory']