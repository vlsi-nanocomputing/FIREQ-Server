# Server Utilities & Exceptions

Memory queues, custom exception definitions, and helper utilities.

```{eval-rst}
.. rubric:: Queues & Memory Management

.. autosummary::
   :toctree: ../_autosummary
   :nosignatures:

   FIREQ_SERVER.utils.memory_queue.MemoryBoundedQueue

.. rubric:: Custom Exceptions

.. autosummary::
   :toctree: ../_autosummary
   :nosignatures:

   FIREQ_SERVER.utils.exceptions.FireqHardwareError
   FIREQ_SERVER.utils.exceptions.DriverError
   FIREQ_SERVER.utils.exceptions.TimingError
   FIREQ_SERVER.utils.exceptions.ConfigurationError
   FIREQ_SERVER.utils.exceptions.FrequencyError
   FIREQ_SERVER.utils.exceptions.EnvelopeUploadError
   FIREQ_SERVER.utils.exceptions.WaveCompilationError
   FIREQ_SERVER.utils.exceptions.DMAError
   FIREQ_SERVER.utils.exceptions.DMATimeoutError
   FIREQ_SERVER.utils.exceptions.RecoverableDMAError
   FIREQ_SERVER.utils.exceptions.HardwareResourceError
   FIREQ_SERVER.utils.exceptions.HardwareStateError
   FIREQ_SERVER.utils.exceptions.ClientDisconnectedError
   FIREQ_SERVER.utils.exceptions.IncompleteTransferError
   FIREQ_SERVER.utils.exceptions.InvalidPayloadError
```