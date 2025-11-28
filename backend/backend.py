# file: fireq_orchestrator/hardware/backend.py
"""
High-level Hardware Facade for the FIREQ system.

Design Philosophy:
- User specifies WHAT to measure (logical labels)
- Backend handles HOW (trigger routing, hardware configuration)
- Implements Transactional Safety for DMA operations (Zombie Killer)

Architecture:
- Uses adapter pattern to isolate from driver quirks
- Delegates timing validation to TimingValidator
- Maintains HardwareInventory for resource discovery
- Manages DMA through AcquisitionEngine with atomic transactions
"""

import logging
import numpy as np
from typing import Tuple, Optional, Any, Dict, List, TYPE_CHECKING

# Import internal components
from .inventory import HardwareInventory
from .dma_engine import AcquisitionEngine
from .driver_wrappers import GeneratorAdapter, AcquisitionAdapter, TriggerAdapter
from .timing import TimingValidator

# --- NEW: Unified Exception Hierarchy ---
from .exceptions import FireqHardwareError, DMATimeoutError, TimingError

if TYPE_CHECKING:
    from ...FIREQ_LL_API.overlay_driver import FIREQ_SoC


class FireqHardwareBackend:
    """
    High-level Hardware Facade for the FIREQ system.
    
    This class provides a clean, safe interface for quantum experiments
    while handling all the complexity of hardware configuration.
    """

    # Hardware Mapping: Generator Index -> RF-DC Location (Tile/Block)
    GEN_RF_MAP = {
        0: {'tile': 0, 'block': 0}
    }

    def __init__(self, overlay: Any, ref_dac_tile: Optional[int] = None,
                 ref_adc_tile: Optional[int] = None, debug: bool = False):
        """
        Initialize the hardware backend.
        
        :param overlay: Loaded PYNQ overlay (should be FIREQ_SoC instance)
        :param debug: Enable debug logging
        """
        # Setup Logger
        self.logger = logging.getLogger("FIREQ_Backend")
        self._setup_logger(debug)
        
        self.logger.info("Initializing Backend...")

        # 1. Hardware Inventory (Resource discovery & Clock Validation)
        # Note: New Inventory handles validation internally, ref_tiles are auto-detected/validated
        self.logger.debug("Step 1: Discovering hardware via HardwareInventory...")
        self.hw = HardwareInventory(overlay, self.logger)

        # 2. Create Adapters
        self.logger.debug("Step 2: Creating GeneratorAdapters...")
        self._generator_adapters: List[GeneratorAdapter] = []
        for idx, gen in enumerate(self.hw.gens):
            # RF Block Mapping Logic (Preserved)
            rf_block = None
            if idx in self.GEN_RF_MAP and self.hw.rf is not None:
                try:
                    tile_idx = self.GEN_RF_MAP[idx]['tile']
                    block_idx = self.GEN_RF_MAP[idx]['block']
                    rf_block = self.hw.rf.dac_tiles[tile_idx].blocks[block_idx]
                except (KeyError, IndexError, AttributeError):
                    pass # Logged inside adapter if critical
            
            # Pass specs to adapter
            self._generator_adapters.append(
                GeneratorAdapter(gen, self.hw.specs, self.logger, rf_block)
            )
        
        self.logger.debug("Step 3: Creating AcquisitionAdapters...")
        self._acquisition_adapters: List[AcquisitionAdapter] = []
        for acq in self.hw.acqs:
            self._acquisition_adapters.append(
                AcquisitionAdapter(acq, self.hw.specs, self.logger)
            )
        
        self.logger.debug("Step 4: Creating TriggerAdapter...")
        self._trigger_adapter = TriggerAdapter(self.hw.trig, self.logger)

        # 3. DMA Engine
        self.logger.debug("Step 5: Creating AcquisitionEngine...")
        # Pass specs for correct buffer width calculation
        self.dma_engine = AcquisitionEngine(
            self.hw.dma, self.hw.switch, self.logger, self.hw.specs
        )

        # 4. Timing Validator
        self.logger.debug("Step 6: Creating TimingValidator...")
        self._timing = TimingValidator(
            self._trigger_adapter, self.hw.specs, self.logger
        )

        # 5. Internal State & Caching
        self.logger.debug("Step 7: Initializing internal state...")
        self.logical_map: Dict[str, Dict[str, int]] = {}
        self._envelope_cache: Dict[str, str] = {}
        self.gen_to_dac_map: Dict[int, Tuple[str, int]] = {}

        # Restore original helpers
        self._build_mapping()
        self._load_default_logical_map()

        self.logger.info(f"Ready. {len(self.hw.gens)} Gens, {len(self.hw.acqs)} Acqs detected.")

    def _setup_logger(self, debug: bool):
        if debug:
            self.logger.setLevel(logging.DEBUG)
            if not self.logger.handlers:
                h = logging.StreamHandler()
                h.setFormatter(logging.Formatter('[Backend] %(levelname)s: %(message)s'))
                self.logger.addHandler(h)
        else:
            self.logger.setLevel(logging.WARNING)
        
        if not debug:
            logging.getLogger('xrfdc').setLevel(logging.ERROR)
            logging.getLogger('pynq').setLevel(logging.ERROR)

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def DAC_SR(self) -> float:
        """DAC sample rate in Hz."""
        return self.hw.specs['dac_sr']

    @property
    def ADC_SR(self) -> float:
        """ADC sample rate in Hz."""
        return self.hw.specs['adc_sr']

    # =========================================================================
    # Internal Mapping Helpers (Preserved from Original)
    # =========================================================================

    def _build_mapping(self):
        """Map generator indices to DAC channels/purposes."""
        for idx, _ in enumerate(self.hw.gens):
            if idx == 0:
                self.gen_to_dac_map[idx] = ('drive', 0)
            elif idx == 1:
                self.gen_to_dac_map[idx] = ('readout', 1)
            else:
                self.gen_to_dac_map[idx] = ('debug', idx)

    def _load_default_logical_map(self):
        """Load default logical mappings."""
        if len(self.hw.gens) > 0 and len(self.hw.acqs) > 0:
            self.logical_map['Readout_Line'] = {'gen': 0, 'adc': 1, 'trig': 1}
            self.logical_map['Qubit_0'] = self.logical_map['Readout_Line']

    def _get_physical_index(self, label: str, index_type: str) -> int:
        if label not in self.logical_map:
            raise ValueError(f"Label '{label}' not found.")
        if index_type not in self.logical_map[label]:
            raise ValueError(f"Type '{index_type}' not defined for '{label}'.")
        return self.logical_map[label][index_type]

    def _get_gen(self, idx: int) -> GeneratorAdapter:
        if idx < 0 or idx >= len(self._generator_adapters):
            raise ValueError(f"Generator index {idx} out of range.")
        return self._generator_adapters[idx]

    def _get_acq(self, idx: int) -> AcquisitionAdapter:
        if idx < 0 or idx >= len(self._acquisition_adapters):
            raise ValueError(f"Acquisition index {idx} out of range.")
        return self._acquisition_adapters[idx]

    # =========================================================================
    # Public API - Direct Access
    # =========================================================================

    def get_generator(self, label: str) -> GeneratorAdapter:
        return self._get_gen(self._get_physical_index(label, 'gen'))

    def get_acquisition(self, label: str) -> AcquisitionAdapter:
        return self._get_acq(self._get_physical_index(label, 'adc'))

    # =========================================================================
    # Public API - Configuration & Waveforms
    # =========================================================================

    def configure_drive_carrier(self, freq_mhz: float, target_label: str = 'Qubit_0'):
        gen = self.get_generator(target_label)
        gen.set_drive_frequency(freq_mhz)
        self.logger.debug(f"Drive carrier: {freq_mhz} MHz")

    def upload_envelope(self, name: str, samples: np.ndarray, 
                        target_label: str = 'Qubit_0') -> str:
        if name in self._envelope_cache:
            return self._envelope_cache[name]

        is_rect = name.lower() in ['rectangular', '_rectangular']
        hw_name = "_RECTANGULAR" if is_rect else name

        if not is_rect:
            gen = self.get_generator(target_label)
            gen.upload_envelope(name, samples)
            hw_name = name # Use the user name as handle if uploaded
            self.logger.debug(f"Envelope '{hw_name}' uploaded")

        self._envelope_cache[name] = hw_name
        return hw_name

    def add_drive_gate(self, shape: str, duration_samples: int, gain: float,
                       step_idx: int, wave_name: str, target_label: str = 'Qubit_0'):
        gen = self.get_generator(target_label)
        
        # Translate shape (logic preserved)
        hw_name = "_RECTANGULAR" if shape == "rectangular" else shape
        
        wdw = gen.create_waveform(hw_name, duration_samples, gain, False)
        gen.add_drive_gate(wdw, wave_name, step_idx)
        self.logger.debug(f"Gate '{wave_name}' added at step {step_idx}")

    def configure_readout_pulse(self, freq: float, phase: float, duration_samples: int,
                          amp: float, shape: str, target_label: str = 'Qubit_0'):
        gen = self.get_generator(target_label)
        gen.set_readout_frequency(freq, phase)

        hw_name = "_RECTANGULAR" if shape == "rectangular" else shape
        wdw = gen.create_waveform(hw_name, duration_samples, amp, False)
        gen.set_readout_waveform(wdw)
        self.logger.debug(f"Readout: {freq}MHz, {duration_samples}smp")

    def configure_drive_sequence(self, delays: List[Tuple[int, int]], channel: int = 1):
        if not delays:
            raise ValueError("Delays list cannot be empty")

        for gate_index, delay_cycles in delays:
            self._trigger_adapter.insert_drive_delay(channel, gate_index, delay_cycles, True)

        # Padding (logic preserved)
        last_index = max(idx for idx, _ in delays)
        self._trigger_adapter.insert_drive_delay(channel, last_index + 1, 2**31 - 1, False)

    def fire_trigger(self, duration_cycles: int, shots: int = 1):
        self._trigger_adapter.configure_experiment(duration_cycles, shots)
        self._trigger_adapter.start()
        self.logger.debug(f"Trigger fired: Dur={duration_cycles}, Shots={shots}")

    def check_hardware_health(self) -> Dict[str, Any]:
        health = {
            'dma': 'unknown', 'clocks': 'unknown',
            'generators': [], 'acquisitions': [], 'trigger': 'unknown'
        }
        try:
            health['dma'] = 'idle' if self.dma_engine.dma.recvchannel.idle else 'busy'
        except: health['dma'] = 'error'

        try:
            if self.DAC_SR > 0 and self.ADC_SR > 0: health['clocks'] = 'ok'
            else: health['clocks'] = 'not_locked'
        except: health['clocks'] = 'error'

        for idx, gen in enumerate(self._generator_adapters):
            health['generators'].append({'index': idx, **gen.get_health()})
        for idx, acq in enumerate(self._acquisition_adapters):
            health['acquisitions'].append({'index': idx, **acq.get_health()})
        
        health['trigger'] = self._trigger_adapter.get_health()['status']
        return health

    # =========================================================================
    # Main Execution (The "Zombie Killer" Logic)
    # =========================================================================

    def start_experiment(self,
                         duration_cycles: int = 2000,
                         shots: int = 1,
                         readout_cfg: Optional[Dict[str, Any]] = None,
                         skip_validation: bool = False) -> Optional[np.ndarray]:
        """
        Execute experiment with Transactional Safety.
        """
        # 1. Validation
        if not skip_validation:
            self._timing.validate_experiment(duration_cycles, shots, readout_cfg)

        dma_buffer = None
        mode = 'decimated'
        timeout = 2

        # 2. Preparation (Readout)
        if readout_cfg:
            # Resolve Hardware
            if 'target_label' in readout_cfg:
                target_label = readout_cfg['target_label']
                mapping = self.logical_map.get(target_label)
                if not mapping:
                    raise ValueError(f"Label '{target_label}' not found.")
                gen_idx, adc_idx, trig_ch = mapping['gen'], mapping['adc'], mapping['trig']
            else:
                gen_idx = readout_cfg.get('gen_index', 0)
                adc_idx = readout_cfg.get('adc_index', 0)
                trig_ch = readout_cfg.get('trigger_channel', 1)
                target_label = f"Physical[Gen{gen_idx}->ADC{adc_idx}]"

            # Extract Parameters
            n_samples = readout_cfg.get('num_samples', 1024)
            freq = readout_cfg.get('freq', 0.0)
            mode = readout_cfg.get('mode', 'decimated')
            timeout = readout_cfg.get('timeout', 2)
            trig_delay = readout_cfg.get('trigger_delay', 50)
            tof = readout_cfg.get('time_of_flight', trig_delay)

            # Configure IPs
            gen = self._get_gen(gen_idx)
            gen.set_readout_frequency(freq, 0.0)
            gen.set_trigger_channel(trig_ch, 'readout')

            acq = self._get_acq(adc_idx)
            
            # --- CRITICAL: Duration Calculation (Restored from Original) ---
            adc_parallelism = self.hw.specs['adc_parallelism']
            # adc_decimation unused in logic but kept for reference
            
            if mode == 'raw':
                acq_duration_cycles = n_samples // adc_parallelism
            elif mode == 'decimated':
                acq_duration_cycles = n_samples
            else:  # accumulated
                acq_duration_cycles = n_samples

            acq.configure(freq, 0.0, acq_duration_cycles, 
                         'decimated' if mode == 'raw' else mode)
            acq.set_time_of_flight(tof)
            acq.set_trigger_channel(trig_ch)

            self._trigger_adapter.set_readout_delay(trig_delay, trig_ch)

            # ARM DMA
            n_samples = n_samples * shots
            dma_buffer = self.dma_engine.arm_acquisition(n_samples, mode, adc_idx)
            
            self.logger.info(
                f"Armed: '{target_label}' [Gen{gen_idx}->ADC{adc_idx}] "
                f"(Trig Ch{trig_ch})"
            )
        else:
            self.logger.info("Drive-only experiment")

        # 3. Transactional Execution
        try:
            # FIRE!
            self.fire_trigger(duration_cycles, shots)

            # RETRIEVE
            if dma_buffer is not None:
                data = self.dma_engine.retrieve_acquisition(dma_buffer, mode, timeout)
                self.logger.info(f"Acquired: {data.shape} samples")
                return data
            return None

        except Exception as e:
            # --- ZOMBIE KILLER LOGIC ---
            if dma_buffer is not None:
                self.logger.error("Experiment Failed with armed DMA. Aborting Engine.")
                self.dma_engine.abort()
                try:
                    if hasattr(dma_buffer, 'freebuffer'):
                        dma_buffer.freebuffer()
                except Exception: pass
            
            raise e

__all__ = ['FireqHardwareBackend']