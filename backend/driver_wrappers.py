# file: fireq_orchestrator/hardware/driver_wrappers.py
"""
Adapter layer for FIREQ_LL_API drivers.

This module implements the Adapter pattern to provide a safe, pythonic interface
to the low-level C-like drivers. It handles:
- Error code translation (from integer codes to Python exceptions).
- Type enforcement (ensuring register values are integers).
- Hardware constraint validation (Nyquist zones, buffer limits).

The architecture ensures that the backend logic is isolated from driver quirks.
"""

import logging
from typing import Optional, Literal, Any, Union, List
import numpy as np

from .exceptions import DriverError, ConfigurationError

class GeneratorAdapter:
    """
    Safe wrapper around the low-level GeneratorDriver.

    Encapsulates signal generation logic, waveform memory management, and 
    frequency configuration, ensuring operations remain within hardware limits.
    """
    
    def __init__(self, driver: Any, hw_specs: dict, 
                 logger: Optional[logging.Logger] = None,
                 rf_block: Optional[Any] = None):
        """
        Initialize the Generator Adapter.

        :param driver: Instance of the low-level GeneratorDriver
        :param hw_specs: Dictionary containing hardware specifications (SR, Nyquist limits)
        :param logger: Logger instance
        :param rf_block: Reference to the RF-DC block for mix-mode configuration
        :raises ConfigurationError: If required hardware specs are missing
        :raises DriverError: If driver initialization fails or attributes are missing
        """
        self._drv = driver
        self._hw_specs = hw_specs
        self._log = logger or logging.getLogger(__name__)
        self._rf_block = rf_block
        
        # Validate and extract required specifications
        required_specs = ['dac_sr', 'dac_nyquist', 'dac_max_nyquist_zone']
        for spec in required_specs:
            if spec not in hw_specs:
                raise ConfigurationError(f"Missing required hardware spec: '{spec}' in GeneratorAdapter")
        
        self._dac_sr = float(hw_specs['dac_sr'])
        self._dac_nyquist = float(hw_specs['dac_nyquist'])
        self._max_nyquist_zone = int(hw_specs['dac_max_nyquist_zone'])
        
        # Cache hardware limits with explicit exception chaining
        try:
            self.max_duration = int(driver.MaximumDuration)
            self.num_channels = int(driver.NumberOfChannels)
            self.trigger_channels = int(driver.TriggerChannels)
            self.sample_size = int(driver.SampleSize)
            self.max_sequence_len = int(driver.MemoryMappedFifoSegmentDepth // 4)
        except AttributeError as e:
            raise DriverError(
                f"GeneratorDriver missing required attribute: {e}",
                driver_name="GeneratorDriver",
                operation="__init__"
            ) from e
        
        # Cache della zona Nyquist corrente - inizializzato dal valore hardware
        # Evita configurazioni ridondanti dell'RF-DC quando la zona non cambia
        self._cached_amd_nyquist_zone: Optional[int] = None
        
        # Leggi lo stato attuale dell'RF-DC al primo avvio
        if self._rf_block is not None:
            try:
                self._cached_amd_nyquist_zone = self._rf_block.NyquistZone
                self._log.debug(f"RF-DC initial NyquistZone read: {self._cached_amd_nyquist_zone}")
            except Exception as e:
                self._log.warning(f"Could not read initial RF-DC NyquistZone: {e}")
                self._cached_amd_nyquist_zone = None
    
    @property
    def driver(self) -> Any:
        """Return the underlying low-level driver instance."""
        return self._drv
    
    def _check_result(self, result: Any, operation: str):
        """
        Validate driver return codes and raise appropriate exceptions.

        :param result: Return code from driver call
        :param operation: Name of the operation performed
        :raises DriverError: If the return code indicates failure (< 0)
        """
        if isinstance(result, int) and result < 0:
            msg = f"{operation} failed."
            if result == -3:
                msg += " (Generic Driver Error: Invalid Parameters)"
            elif result == -4:
                msg += " (Out of Memory)"
            else:
                msg += f" (Unknown Code: {result})"

            self._log.error(f"Driver Error in {operation}: Code {result}")
            raise DriverError(
                msg,
                driver_name="GeneratorDriver",
                operation=operation,
                return_code=result
            )
    
    def set_drive_frequency(self, freq_mhz: float):
        """
        Set the NCO frequency for the drive channel.

        Validates that the requested frequency lies within the supported
        Nyquist zones defined by the hardware inventory. Configures RF-DC
        NyquistZone only if it differs from the cached value to avoid
        redundant hardware operations.

        :param freq_mhz: Frequency in MHz
        :raises ConfigurationError: If frequency is negative or exceeds hardware capabilities
        """
        if freq_mhz < 0:
            raise ConfigurationError(f"Frequency must be positive, got {freq_mhz} MHz")
        
        # --- Guardrail: Max Nyquist Zone Check ---
        freq_hz = freq_mhz * 1e6
        nyquist_zone = int(freq_hz / self._dac_nyquist) + 1
        
        if nyquist_zone < 1: 
            nyquist_zone = 1
        
        if nyquist_zone > self._max_nyquist_zone:
            max_freq_mhz = (self._dac_nyquist * self._max_nyquist_zone) / 1e6
            self._log.error(f"Frequency {freq_mhz} MHz is in Zone {nyquist_zone}, max allowed is Zone {self._max_nyquist_zone}.")
            raise ConfigurationError(
                f"HARDWARE LIMIT: Requested Frequency {freq_mhz} MHz falls into Nyquist Zone {nyquist_zone}. "
                f"Operation is strictly limited to Zone {self._max_nyquist_zone} "
                f"(Max Freq approx {max_freq_mhz:.1f} MHz). Signal would be attenuated/aliased."
            )

        # --- Configure RF-DC NyquistZone with intelligent caching ---
        if self._rf_block is not None:
            try:
                # Converti zona numerica a formato AMD xrfdc (Odd/Even)
                # Zone 1,3,5... (Odd) → amd_zone = 1 (Normal Mode)
                # Zone 2,4,6... (Even) → amd_zone = 2 (Mixing Mode, auto-enabled)
                amd_zone = 1 if nyquist_zone % 2 == 1 else 2
                
                # Configura solo se è diversa dalla zona cachata
                if amd_zone != self._cached_amd_nyquist_zone:
                    self._rf_block.NyquistZone = amd_zone
                    self._cached_amd_nyquist_zone = amd_zone
                    self._log.info(
                        f"RF-DC NyquistZone changed to {amd_zone} "
                        f"({'Normal (Odd)' if amd_zone == 1 else 'Mixing (Even)'}) "
                        f"for {freq_mhz} MHz (Zone {nyquist_zone})"
                    )
                else:
                    self._log.debug(
                        f"RF-DC NyquistZone already {amd_zone}, skipping reconfiguration for {freq_mhz} MHz"
                    )
            except Exception as e:
                self._log.warning(f"Failed to configure RF-DC Nyquist Zone: {e}")
        
        # Call driver
        result = self._drv.set_drive_dds_parameters(float(freq_mhz), int(self._dac_sr))
        self._check_result(result, f"set_drive_frequency({freq_mhz} MHz)")
        self._log.debug(f"Drive frequency set to {freq_mhz} MHz (Zone {nyquist_zone})")
    
    def set_readout_frequency(self, freq_mhz: float, phase_rad: float = 0.0):
        """
        Set the NCO frequency and phase for the readout channel.

        Validates that the requested frequency lies within the supported
        Nyquist zones. Configures RF-DC NyquistZone only if it differs from
        the cached value to avoid redundant hardware operations.

        :param freq_mhz: Frequency in MHz
        :param phase_rad: Phase offset in radians
        :raises ConfigurationError: If frequency is negative or exceeds hardware capabilities
        """
        if freq_mhz < 0:
            raise ConfigurationError(f"Frequency must be positive, got {freq_mhz} MHz")
        
        # --- Guardrail: Max Nyquist Zone Check ---
        freq_hz = freq_mhz * 1e6
        nyquist_zone = int(freq_hz / self._dac_nyquist) + 1
        
        if nyquist_zone > self._max_nyquist_zone:
            max_freq_mhz = (self._dac_nyquist * self._max_nyquist_zone) / 1e6
            self._log.error(f"Readout Frequency {freq_mhz} MHz exceeds hardware limit (Zone {nyquist_zone}).")
            raise ConfigurationError(
                f"HARDWARE LIMIT: Readout Frequency {freq_mhz} MHz falls into Nyquist Zone {nyquist_zone}. "
                f"Operation is limited to Zone {self._max_nyquist_zone} "
                f"(Max Freq approx {max_freq_mhz:.1f} MHz)."
            )
        
        # --- Configure RF-DC NyquistZone with intelligent caching ---
        if self._rf_block is not None:
            try:
                # Converti zona numerica a formato AMD xrfdc (Odd/Even)
                # Zone 1,3,5... (Odd) → amd_zone = 1 (Normal Mode)
                # Zone 2,4,6... (Even) → amd_zone = 2 (Mixing Mode, auto-enabled)
                amd_zone = 1 if nyquist_zone % 2 == 1 else 2
                
                # Configura solo se è diversa dalla zona cachata
                if amd_zone != self._cached_amd_nyquist_zone:
                    self._rf_block.NyquistZone = amd_zone
                    self._cached_amd_nyquist_zone = amd_zone
                    self._log.info(
                        f"RF-DC NyquistZone changed to {amd_zone} "
                        f"({'Normal (Odd)' if amd_zone == 1 else 'Mixing (Even)'}) "
                        f"for readout {freq_mhz} MHz (Zone {nyquist_zone})"
                    )
                else:
                    self._log.debug(
                        f"RF-DC NyquistZone already {amd_zone}, skipping reconfiguration for readout {freq_mhz} MHz"
                    )
            except Exception as e:
                self._log.warning(f"Failed to configure RF-DC Nyquist Zone: {e}")
        
        result = self._drv.set_readout_dds_parameters(float(freq_mhz), float(phase_rad), int(self._dac_sr))
        self._check_result(result, f"set_readout_frequency({freq_mhz} MHz)")
        self._log.debug(f"Readout frequency set to {freq_mhz} MHz (Zone {nyquist_zone})")
    
    
    def set_trigger_channel(self, channel: int, ttype: Literal['drive', 'readout']):
        """
        Set the input trigger channel for a specific engine.

        :param channel: Channel index (0 to disable)
        :param ttype: Engine type ('drive' or 'readout')
        """
        channel = int(channel) # Type safety
        if channel < 0 or channel > self.trigger_channels:
            raise ConfigurationError(f"Channel {channel} out of range [0, {self.trigger_channels}]")
        
        result = self._drv.set_trigger_channel(channel, ttype)
        self._check_result(result, f"set_trigger_channel({channel}, {ttype})")
    
    def upload_envelope(self, name: str, samples: Union[np.ndarray, list],
                        interpolate: bool = True, symmetric: bool = False,
                        i_even: bool = False, q_even: bool = False) -> str:
        """
        Upload a complex envelope to the generator memory.

        :param name: Unique name for the envelope
        :param samples: Array of complex samples
        :return: Normalized hardware name of the envelope
        """
        if name.lower() in ['rectangular', '_rectangular']:
            return '_RECTANGULAR'
            
        # Input Sanitization
        if not isinstance(samples, np.ndarray):
            try:
                samples = np.array(samples)
            except Exception as e:
                raise ConfigurationError(f"Invalid sample format for '{name}': {e}")

        if samples.size == 0:
            raise ConfigurationError(f"Envelope '{name}' cannot be empty.")       
        if samples.size < 2:
            raise ConfigurationError(f"Envelope '{name}' must have at least 2 samples.")
        
        if not np.iscomplexobj(samples):
            self._log.warning(f"Envelope '{name}' is not complex. Casting to complex128.")
            samples = samples.astype(complex)

        # Call driver
        result = self._drv.add_envelope_to_envelope_memory(
            samples, interpolate, symmetric, i_even, q_even, name
        )

        if result == -4:
            self._log.error(f"OOM: Failed to upload '{name}'")
            raise DriverError(
                f"Not enough memory to store envelope '{name}'", 
                driver_name="GeneratorDriver", 
                operation="add_envelope_to_envelope_memory",
                return_code=-4
            )

        self._check_result(result, f"upload_envelope({name})")
        self._log.debug(f"Envelope '{name}' uploaded successfully.")
        return name
    
    def create_waveform(self, envelope_name: str, duration_samples: int,
                        gain: float, switch_iq: bool = False) -> int:
        """
        Create a Wave Definition Word (WDW) linking an envelope to execution parameters.

        :param duration_samples: Duration in clock cycles (must be int)
        :param gain: Gain factor [-1.0, 1.0]
        :return: Integer WDW
        """
        if not -1.0 <= gain <= 1.0:
            raise ConfigurationError(f"Gain {gain} out of range [-1, 1]")
        
        duration_samples = int(duration_samples) # Type enforcement
        if duration_samples != 0:
            if duration_samples < 2 or duration_samples > self.max_duration:
                raise ConfigurationError(f"Duration {duration_samples} out of range [2, {self.max_duration}]")
        
        hw_name = '_RECTANGULAR' if envelope_name == 'rectangular' else envelope_name
        
        wdw = self._drv.create_wave_definition_word(hw_name, duration_samples, float(gain), switch_iq)
        
        if wdw == -3:
            raise DriverError(
                f"Failed to create waveform. Envelope '{envelope_name}' likely not found.",
                driver_name="GeneratorDriver",
                operation="create_wave_definition_word",
                return_code=-3
            )
        
        self._check_result(wdw, f"create_waveform({envelope_name})")
        return wdw
    
    def add_drive_gate(self, wdw: int, name: str, sequence_index: int):
        """Add a defined waveform to the execution sequencer."""
        
        sequence_index = int(sequence_index) # Type enforcement
        wdw = int(wdw)

        if sequence_index < 1:
            raise ConfigurationError(f"Sequence index must be >= 1 (got {sequence_index}).")
        if sequence_index > self.max_sequence_len:
            raise ConfigurationError(f"Sequence index {sequence_index} exceeds FIFO depth.")
        if wdw < 0:
             raise ConfigurationError("Invalid Wave Definition Word.")

        # Step 1: Add to Wave Memory
        result = self._drv.add_wave_in_wave_memory(wdw, name)
        if result == -3:
             raise DriverError(
                 f"Failed to add gate '{name}'. Name duplicate or Wave Memory full.",
                 return_code=-3
             )
        self._check_result(result, f"add_wave_in_wave_memory({name})")
        
        # Step 2: Add to Sequencer
        result = self._drv.add_wave_to_drive_wave_sequence(sequence_index, name)
        self._check_result(result, f"add_wave_to_drive_wave_sequence({sequence_index})")

    def set_readout_waveform(self, wdw: int):
        """Set the waveform used for the readout pulse."""
        if wdw < 0:
            raise ConfigurationError("Invalid Wave Definition Word.")
        result = self._drv.write_readout_wave(int(wdw))
        self._check_result(result, "write_readout_wave")

    def get_health(self) -> dict:
        """Check IP status register."""
        try:
            ctrl = self._drv.AxiLiteInterfaceMMIO.read(0x00)
            return {'status': 'ok', 'control_reg': f"0x{ctrl:08X}"}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def reset_memories(self):
        """Clear envelope and wave caches."""
        self._drv.reset_envelope_dict()


class AcquisitionAdapter:
    """
    Safe wrapper around the low-level AcquisitionDriver.
    
    Manages ADC configuration, down-conversion settings, and acquisition windows.
    """
    
    def __init__(self, driver: Any, hw_specs: dict, logger: Optional[logging.Logger] = None):
        self._drv = driver
        self._hw_specs = hw_specs
        self._log = logger or logging.getLogger(__name__)
        
        required_specs = ['adc_sr', 'adc_nyquist']
        for spec in required_specs:
            if spec not in hw_specs:
                raise ConfigurationError(f"Missing required hardware spec: '{spec}'")
        
        self._adc_sr = float(hw_specs['adc_sr'])
        self._adc_nyquist = float(hw_specs['adc_nyquist'])
        
        try:
            self.max_duration = int(driver.MaximumDuration)
            self.num_channels = int(driver.NumberOfChannels)
            self.max_tof = int(driver.TimeOfFlightMax)
            self.trigger_channels = int(driver.TriggerChannels)
        except AttributeError as e:
            raise DriverError(
                f"AcquistionDriver missing required attribute: {e}",
                driver_name="AcquistionDriver",
                operation="__init__"
            ) from e
    
    @property
    def driver(self) -> Any:
        return self._drv
    
    def _check_result(self, result: Any, operation: str):
        if isinstance(result, int) and result < 0:
            self._log.error(f"Driver Error in {operation}: Code {result}")
            raise DriverError(
                f"{operation} failed with code {result}",
                driver_name="AcquistionDriver",
                operation=operation,
                return_code=result
            )
    
    def configure(self, freq_mhz: float, phase_rad: float, duration_cycles: int,
                  mode: Literal['decimated', 'accumulated'] = 'decimated'):
        """
        Configure the acquisition engine parameters.

        :param freq_mhz: Demodulation frequency in MHz
        :param duration_cycles: Window length in clock cycles
        """
        if freq_mhz < 0:
            raise ConfigurationError(f"Frequency must be positive, got {freq_mhz} MHz")
        
        # --- Guardrail: ADC Nyquist Check ---
        # Assuming Max Zone 2 for ADC as well, consistent with DAC
        freq_hz = freq_mhz * 1e6
        nyquist_zone = int(freq_hz / self._adc_nyquist) + 1

        if nyquist_zone > 2:
            max_freq_mhz = (self._adc_nyquist * 2) / 1e6
            raise ConfigurationError(
                f"HARDWARE LIMIT: Acquisition Frequency {freq_mhz} MHz falls into Zone {nyquist_zone}. "
                f"Operation is limited to Zone 2 (Max Freq approx {max_freq_mhz:.1f} MHz). "
                f"Signal would be invalid."
            )
        
        duration_cycles = int(duration_cycles)
        if duration_cycles < 1 or duration_cycles > self.max_duration:
            raise ConfigurationError(f"Duration {duration_cycles} out of range [1, {self.max_duration}].")
        
        result = self._drv.set_acquistion_parameters(
            float(freq_mhz), float(phase_rad), duration_cycles, int(self._adc_sr)
        )
        self._check_result(result, "set_acquistion_parameters")
        
        result = self._drv.set_decimated_output_type(mode)
        self._check_result(result, f"set_decimated_output_type({mode})")
    
    def set_trigger_channel(self, channel: int):
        """Set the trigger channel for the acquisition start."""
        channel = int(channel)
        if channel < 0 or channel > self.trigger_channels:
            raise ConfigurationError(f"Channel {channel} out of range.")
        result = self._drv.set_trigger_channel(channel)
        self._check_result(result, f"set_trigger_channel({channel})")
    
    def set_time_of_flight(self, tof_cycles: int):
        """Set the delay between trigger and acquisition start."""
        tof_cycles = int(tof_cycles)
        if tof_cycles < 1 or tof_cycles > self.max_tof:
            raise ConfigurationError(f"TOF {tof_cycles} out of range.")
        result = self._drv.set_time_of_flight(tof_cycles)
        self._check_result(result, f"set_time_of_flight({tof_cycles})")

    def get_health(self) -> dict:
        try:
            ctrl = self._drv.AxiLiteInterfaceMMIO.read(0x00)
            return {'status': 'ok', 'control_reg': f"0x{ctrl:08X}"}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}


class TriggerAdapter:
    """
    Safe wrapper around the low-level TriggerGeneratorDriver.
    
    Manages the master timing and trigger distribution.
    """
    
    def __init__(self, driver: Any, logger: Optional[logging.Logger] = None):
        self._drv = driver
        self._log = logger or logging.getLogger(__name__)
        
        try:
            self.max_duration = int(driver.ExperimentTimerMax)
            self.max_shots = int(driver.MaxHWRepetitions)
            self.max_delay = int(driver.DriveDelayMax)
            self.fifo_depth = int(driver.ChannelFifoDepth)
            self.trigger_channels = int(driver.TriggerChannels)
        except AttributeError as e:
            raise DriverError(f"TriggerDriver missing attribute: {e}", operation="__init__") from e
    
    @property
    def driver(self) -> Any:
        return self._drv
    
    def _check_result(self, result: Any, operation: str):
        if isinstance(result, int) and result < 0:
             self._log.error(f"Trigger Driver Error in {operation}: Code {result}")
             raise DriverError(
                f"{operation} failed with code {result}",
                driver_name="TriggerGeneratorDriver",
                operation=operation,
                return_code=result
            )
    
    def configure_experiment(self, duration_cycles: int, shots: int = 1):
        """Configure the global experiment timer and repetition count."""
        duration_cycles = int(duration_cycles)
        shots = int(shots)

        if duration_cycles > self.max_duration:
            raise ConfigurationError(f"Duration {duration_cycles} exceeds max {self.max_duration}")
        if shots < 1 or shots > self.max_shots:
            raise ConfigurationError(f"Shots {shots} out of range [1, {self.max_shots}]")
        
        self._drv.set_experiment_duration(duration_cycles)
        self._drv.set_number_of_shots(shots)
    
    def set_readout_delay(self, delay_cycles: int, channel: int):
        """Set the delay for the readout trigger pulse."""
        channel = int(channel)
        delay_cycles = int(delay_cycles)

        if channel < 1 or channel > self.trigger_channels:
            raise ConfigurationError(f"Trigger channel {channel} out of range.")
        
        result = self._drv.set_readout_delay(delay_cycles, channel)
        self._check_result(result, "set_readout_delay")
    
    def insert_drive_delay(self, channel: int, index: int, delay_cycles: int, generate_trigger: bool = True):
        """Insert a delay into the drive trigger FIFO sequence."""
        result = self._drv.insert_drive_delay(
            int(channel), 
            int(index), 
            int(delay_cycles), 
            1 if generate_trigger else 0
        )
        self._check_result(result, "insert_drive_delay")
    
    def start(self):
        """Start the experiment execution."""
        self._drv.start_experiment()
    
    def is_done(self) -> bool:
        """Check if the experiment execution is finished."""
        return bool(self._drv.is_done())
    
    def get_health(self) -> dict:
        try:
            return {'status': 'done' if self.is_done() else 'ready'}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

__all__ = ['GeneratorAdapter', 'AcquisitionAdapter', 'TriggerAdapter']