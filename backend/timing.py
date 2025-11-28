# file: fireq_orchestrator/hardware/timing.py
"""
Timing validation logic for FIREQ experiments.

This module extracts timing validation from the backend to:
1. Enable reuse from the orchestrator
2. Simplify the backend
3. Make timing logic testable in isolation

The validation ensures that experiment parameters are within hardware
constraints and that there's enough time for all operations to complete.
"""

import logging
from typing import Dict, Any, Optional

from .exceptions import TimingError, ConfigurationError


class TimingValidator:
    """
    Validates experiment timing parameters against hardware constraints.
    
    This class encapsulates all the complex timing calculations needed
    to ensure an experiment will execute correctly on the hardware.
    
    Attributes:
        MIN_DURATION_CYCLES: Minimum experiment duration for trigger propagation
        MAX_DMA_SAMPLES: Maximum samples that fit in CMA buffer
        MAX_TOF: Maximum time-of-flight (8-bit counter limit)
        SAFETY_MARGIN: Extra cycles added for safety
    """
    
    # Hardware constraints (constants)
    MIN_DURATION_CYCLES = 50   # Minimum for trigger propagation
    MAX_DMA_SAMPLES = int(2**21)    #NOTE: Typical CMA buffer limit: 16. Set to 2^21 (8Mb) as it should be better
    MAX_TOF = 255              # 8-bit TOF counter
    SAFETY_MARGIN = 100        # Extra cycles for safety
    
    def __init__(self, trigger_adapter, hw_specs: Dict[str, Any],
                 logger: Optional[logging.Logger] = None):
        """
        Initialize the timing validator.
        
        :param trigger_adapter: TriggerAdapter instance (for hardware limits)
        :param hw_specs: Hardware specifications dict from HardwareInventory.specs
                        Must contain 'dac_sr' and 'adc_sr'
        :param logger: Optional logger for debug output
        :raises ValueError: If required specs are missing
        """
        self.trig = trigger_adapter
        
        # Validate and extract required specs
        required_specs = ['dac_sr', 'adc_sr']
        for spec in required_specs:
            if spec not in hw_specs:
                raise ConfigurationError(f"Missing required hardware spec: '{spec}' in TimingValidator")
        
        self.dac_sr = hw_specs['dac_sr']
        self.adc_sr = hw_specs['adc_sr']
        
        # Optional: extract ADC characteristics if available
        self.adc_parallelism = hw_specs.get('adc_parallelism', 8)
        self.adc_decimation_factor = hw_specs.get('adc_decimation_factor', 8)
        
        self.clock_ratio = self.dac_sr / self.adc_sr
        self.logger = logger or logging.getLogger(__name__)
    
    def validate_experiment(self, duration_cycles: int, shots: int,
                            readout_cfg: Optional[Dict[str, Any]] = None):
        """
        Full validation of experiment parameters. It checks:
        1. Duration validity : total durtaion within hardware limits
        2. Shot count validity : shots within hardware limits
        3. Readout timing validity: if a readout is requested, ensure the clock cycles fit within the experiment window
        
        :param duration_cycles: Experiment duration in clock cycles
        :param shots: Number of hardware repetitions
        :param readout_cfg: Optional readout configuration dict
        :raises TimingError: If any parameter is invalid
        """
        self._validate_duration(duration_cycles)
        self._validate_shots(shots)
        
        if readout_cfg:
            self._validate_readout(duration_cycles, readout_cfg)
    
    def estimate_acquisition_time_us(self, n_samples: int, mode: str) -> float:
        """
        Estimate acquisition time in microseconds.
        
        Useful for user feedback and experiment planning.
        
        :param n_samples: Number of samples to acquire
        :param mode: Acquisition mode
        :return: Estimated time in microseconds
        """
        cycles = self._compute_acquisition_cycles(n_samples, mode)
        # Convert to time using ADC sample rate
        # ADC SR is in Hz, so cycles / (SR / 8) gives seconds (8 parallel samples/cycle)
        time_s = cycles / (self.adc_sr / 8)
        return time_s * 1e6

    def _validate_duration(self, duration_cycles: int):
        """
        Validate experiment duration.
        
        :raises TimingError: If duration is too short or exceeds hardware max
        """
        if duration_cycles < self.MIN_DURATION_CYCLES:
            raise TimingError(
                f"Duration {duration_cycles} too short. "
                f"Minimum {self.MIN_DURATION_CYCLES} cycles needed for trigger propagation."
            )
        
        if duration_cycles > self.trig.max_duration:
            raise TimingError(
                f"Duration {duration_cycles} exceeds hardware maximum ({self.trig.max_duration})"
            )
    
    def _validate_shots(self, shots: int):
        """
        Validate shot count.
        
        :raises TimingError: If shots is invalid
        """
        if shots < 1:
            raise TimingError("Shots must be >= 1")
        
        if shots > self.trig.max_shots:
            raise TimingError(
                f"Shots {shots} exceeds hardware maximum ({self.trig.max_shots})"
            )
    
    def _validate_readout(self, duration_cycles: int, cfg: Dict[str, Any]):
        """
        Validate readout timing parameters.
        
        Checks:
        1. DMA buffer size
        2. Trigger delay sanity
        3. TOF limit
        4. Acquisition fits in experiment window
        
        :raises TimingError: If any readout parameter is invalid
        """
        # Extract parameters with defaults
        n_samples = cfg.get('num_samples', 1024)
        trig_delay = cfg.get('trigger_delay', 50)
        tof = cfg.get('time_of_flight', 1)
        mode = cfg.get('mode', 'decimated')
        
        # Check 1: DMA buffer size
        if n_samples > self.MAX_DMA_SAMPLES:
            raise TimingError(
                f"num_samples {n_samples} exceeds DMA buffer limit ({self.MAX_DMA_SAMPLES}). "
                f"Reduce sample count or use multiple acquisitions."
            )
        
        # Check 2: Trigger delay sanity
        if trig_delay >= duration_cycles:
            raise TimingError(
                f"Trigger delay ({trig_delay}) >= experiment duration ({duration_cycles}). "
                f"Trigger will never fire!"
            )
        
        # Check 3: TOF limit
        if tof > self.MAX_TOF:
            raise TimingError(
                f"Time of flight {tof} exceeds hardware limit ({self.MAX_TOF}). "
                f"This is an 8-bit counter."
            )
        
        # Check 4: Acquisition fits in experiment window
        acq_cycles = self._compute_acquisition_cycles(n_samples, mode)
        system_cycles = int(acq_cycles * self.clock_ratio)
        min_duration = trig_delay + tof + system_cycles + self.SAFETY_MARGIN
        
        if duration_cycles < min_duration:
            raise TimingError(
                f"Experiment duration {duration_cycles} too short.\n"
                f"Need at least {min_duration} cycles:\n"
                f"  - Trigger delay: {trig_delay}\n"
                f"  - Time of flight: {tof}\n"
                f"  - Acquisition: {system_cycles} (for {n_samples} samples in {mode} mode)\n"
                f"  - Safety margin: {self.SAFETY_MARGIN}\n"
                f"\nIncrease duration_cycles or reduce num_samples."
            )
        
        self.logger.debug(
            f"Timing validated: dur={duration_cycles}, delay={trig_delay}, "
            f"tof={tof}, acq={system_cycles}"
        )
    
    def _compute_acquisition_cycles(self, n_samples: int, mode: str) -> int:
        """
        Compute ADC cycles needed for given sample count and mode.
        
        The relationship between samples and cycles depends on:
        - ADC parallelism (8 samples/cycle)
        - Decimation factor (8x for decimated mode)
        
        :param n_samples: Desired number of output samples
        :param mode: 'raw', 'decimated', or 'accumulated'
        :return: Number of ADC clock cycles required
        """
        if mode == 'raw':
            # Raw: adc_parallelism samples/cycle (no decimation)
            # n_samples output needs n_samples/parallelism cycles
            return n_samples // self.adc_parallelism
        else:
            # Decimated: decimation_factor x decimation BUT adc_parallelism samples/cycle
            # Net effect: 1 output sample per cycle (when both are 8)
            # Accumulated: Same timing, different output format
            return n_samples
    
__all__ = ['TimingValidator']