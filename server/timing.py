# file: fireq_orchestrator/hardware/timing.py
"""
Timing validation logic for FIREQ experiments.
"""

import logging
from typing import Dict, Any, Optional

from .exceptions import TimingError, ConfigurationError


class TimingValidator:
    """
    Validates experiment timing parameters against hardware constraints.
    """
    
    # Hardware constraints (constants)
    MIN_DURATION_CYCLES = 50   # Minimum for trigger propagation
    MAX_DMA_SAMPLES = int(2**21)    # 8Mb buffer limit
    MAX_TOF = 255              # 8-bit TOF counter
    SAFETY_MARGIN = 5000        # Extra cycles for safety
    
    def __init__(self, trigger_adapter, _hw_specs: Dict[str, Any],
                 logger: Optional[logging.Logger] = None):
        """
        Initialize the timing validator.
        """
        self.trig = trigger_adapter
        
        # Validate and extract required specs
        required_specs = ['dac_sr', 'adc_sr', 'adc_decimation_factor', 'adc_parallelism']
        for spec in required_specs:
            if spec not in _hw_specs:
                raise ConfigurationError(f"Missing required hardware spec: '{spec}' in TimingValidator")
        
        self.dac_sr = _hw_specs['dac_sr']
        self.adc_sr = _hw_specs['adc_sr']
        
        self.adc_prl = _hw_specs.get('adc_parallelism', 8)
        self.adc_decim = _hw_specs.get('adc_decimation_factor', 8)
        self.logger = logger or logging.getLogger(__name__)
        
        # NOTA: Clock Ratio rimosso perchÃ© 1:1 (Trigger Clock == ADC Fabric Clock)

    
    def validate_experiment(self, duration_cycles: int, shots: int,
                            readout_cfg: Optional[Dict[str, Any]] = None):
        """
        Full validation of experiment parameters.
        """
        self._validate_duration(duration_cycles)
        self._validate_shots(shots)
        
        if readout_cfg:
            self._validate_readout(duration_cycles, readout_cfg)
    
    def estimate_acquisition_time_us(self, n_samples: int, mode: str) -> float:
        """
        Estimate acquisition time in microseconds.
        """
        cycles = self._compute_acquisition_cycles(n_samples, mode)
        # Convert to time using ADC sample rate
        # ADC SR is in Hz, so cycles / (SR / 8) gives seconds (8 parallel samples/cycle)
        time_s = cycles / (self.adc_sr / self.adc_prl)
        return time_s * 1e6

    def compute_auto_duration(self, readout_cfg: Dict[str, Any]) -> int:
        """
        Calculates the minimum duration required for the experiment.
        Includes trigger delay, TOF, acquisition duration, and safety margin.
        """
        # 1. Extract parameters with safe defaults
        n_samples = readout_cfg.get('num_samples', 1024)
        trig_delay = readout_cfg.get('trigger_delay', 50)
        tof = readout_cfg.get('time_of_flight', trig_delay) 
        mode = readout_cfg.get('mode', 'decimated')
        
        # 2. Compute acquisition duration (in System Cycles)
        # FIX: Usa il metodo centralizzato invece di duplicare la logica (e sbagliare variabili)
        acq_cycles = self._compute_acquisition_cycles(n_samples, mode)
        
        # 3. Sum everything + Safety Margin
        # Clock ratio implicito Ã¨ 1
        min_duration = trig_delay + tof + acq_cycles + self.SAFETY_MARGIN
        
        # 4. Align to DAC Parallelism (16) to avoid glitches
        remainder = min_duration % 16
        if remainder != 0:
            min_duration += (16 - remainder)
            
        self.logger.debug(f"Auto-Duration Calculated: {min_duration} cycles "
                          f"(Delay={trig_delay}, TOF={tof}, Acq={acq_cycles})")
        
        return int(min_duration)

    def _validate_duration(self, duration_cycles: int):
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
        if shots < 1:
            raise TimingError("Shots must be >= 1")
        
        if shots > self.trig.max_shots:
            raise TimingError(
                f"Shots {shots} exceeds hardware maximum ({self.trig.max_shots})"
            )
    

    def _validate_readout(self, duration_cycles: int, cfg: Dict[str, Any]):
        """
        Validate readout timing parameters.
        """
        n_samples = cfg.get('num_samples', 1024)
        trig_delay = cfg.get('trigger_delay', 50)
        tof = cfg.get('time_of_flight', 1)
        mode = cfg.get('mode', 'decimated')
        shots = cfg.get('shots', 1) 

        # Calculate actual buffer words needed (must match dma_engine.arm_acquisition logic)
        if mode == 'accumulated':
            # 2 words per sample (64-bit I+Q)
            total_words = n_samples * shots * 2
        elif mode == 'raw':
            # Raw mode: hardware produces full cycles of 8 IQ pairs each
            # Must account for ceiling rounding per shot
            samples_per_shot = n_samples
            acq_cycles_per_shot = (samples_per_shot + self.adc_prl - 1) // self.adc_prl
            actual_samples_per_shot = acq_cycles_per_shot * self.adc_prl
            total_words = actual_samples_per_shot * shots
        else:
            # Decimated: 1 word per sample
            total_words = n_samples * shots

        # Check 1: DMA buffer size against total words
        if total_words > self.MAX_DMA_SAMPLES:
            raise TimingError(
                f"Total DMA words {total_words} (num_samples={n_samples} x shots={shots}) "
                f"exceeds DMA buffer limit ({self.MAX_DMA_SAMPLES}). "
                f"Reduce sample count or split acquisition."
            )

        # Trigger delay sanity
        if trig_delay >= duration_cycles:
            raise TimingError(
                f"Trigger delay ({trig_delay}) >= experiment duration ({duration_cycles}). "
                f"Trigger will never fire!"
            )

        # TOF limit
        if tof > self.MAX_TOF:
            raise TimingError(
                f"Time of flight {tof} exceeds hardware limit ({self.MAX_TOF}). "
                f"This is an 8-bit counter."
            )

        # Acquisition fits in window
        acq_cycles_needed = self._compute_acquisition_cycles(n_samples, mode)
        # Clock ratio implicito 1
        min_duration = trig_delay + tof + acq_cycles_needed + self.SAFETY_MARGIN

        if duration_cycles < min_duration:
            raise TimingError(
                f"Experiment duration {duration_cycles} too short.\n"
                f"Need at least {min_duration} cycles:\n"
                f"  - Trigger delay: {trig_delay}\n"
                f"  - Time of flight: {tof}\n"
                f"  - Acquisition: {acq_cycles_needed} (for {n_samples} samples in {mode} mode)\n"
                f"  - Safety margin: {self.SAFETY_MARGIN}\n"
                f"\nIncrease duration_cycles or reduce num_samples/shots."
            )

        self.logger.debug(
            f"Timing validated: dur={duration_cycles}, delay={trig_delay}, "
            f"tof={tof}, acq={acq_cycles_needed}, total_words={total_words}"
        )

    
    def _compute_acquisition_cycles(self, n_samples: int, mode: str) -> int:
        """
        Compute ADC cycles needed. MUST match logic in AcquisitionAdapter.
        """
        if mode == 'raw':
            # Ceiling division per RAW (8 samples/cycle)
            # (num + den - 1) // den
            return (n_samples + self.adc_prl - 1) // self.adc_prl
        else:
            # 1 sample/cycle per Decimated/Accumulated
            return n_samples
    
__all__ = ['TimingValidator']