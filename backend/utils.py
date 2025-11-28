# file: fireq_orchestrator/hardware/utils.py
"""
Utility classes for the hardware layer.

Refactored to integrate with the unified exception hierarchy.
"""

import signal
import logging
from typing import Optional

# FIX CRITICO: Importiamo l'errore dalla gerarchia centrale, non lo ridefiniamo qui!
from .exceptions import DMATimeoutError

class Timeout:
    """
    Context manager to handle execution timeouts using UNIX signals (SIGALRM).
    
    Example:
        >>> with Timeout(5, "Operation timed out"):
        ...     do_something_slow()
    """

    def __init__(self, seconds: int = 1, error_message: str = "Timeout"):
        self.seconds = seconds
        self.error_message = error_message
        self.original_handler = None
        self._use_sigalrm = hasattr(signal, 'SIGALRM')
        self._logger = logging.getLogger(__name__)

    def handle_timeout(self, signum, frame):
        """Signal handler callback - raises DMATimeoutError."""
        # Solleva l'eccezione definita in exceptions.py
        raise DMATimeoutError(self.error_message)

    def __enter__(self):
        """Set up the alarm signal on context entry."""
        if not self._use_sigalrm:
            self._logger.warning("[Timeout] SIGALRM not available on this OS (Windows?). Timeout disabled.")
            return self

        # Salva il vecchio handler per ripristinarlo dopo
        self.original_handler = signal.signal(signal.SIGALRM, self.handle_timeout)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, type, value, traceback):
        """Clean up the alarm signal on context exit."""
        if not self._use_sigalrm:
            return

        # Robust signal cleanup
        try:
            signal.alarm(0) # Disabilita il timer
            if self.original_handler:
                signal.signal(signal.SIGALRM, self.original_handler)
        except Exception as e:
            # Non usare raise qui, altrimenti mascheri l'errore originale se c'è stato!
            self._logger.error(f"Failed to cleanup alarm signal: {e}")

__all__ = ['Timeout']