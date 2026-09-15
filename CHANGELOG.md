# Changelog

All notable changes to FIREQ-Server are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-09-15

### Added

- `trigger_manually` server command (`ip_name` field) to fire a child IP's
  `manual_trigger()`, answering `{"type": "status", "msg": "ok"}` or an `error`.
- `FIREQSystemNode.trigger_ip_manually(ip_name)`, replacing `trigger_manually()`.
  It works on any child IP and raises `ValueError` when the IP does not expose a
  manual trigger.
- `{"type": "status", "msg": "ok"}` acknowledgement once a configuration has been
  applied.
- Explicit experiment framing: sweeps now report their shot count, variable
  order and variable values up front in the `experiment_header`, and the total
  sweep time at the end in the `experiment_footer`.
- `docs/` protocol documentation for the `config_and_run` lifecycle.

### Changed

- **Breaking:** the experiment protocol is now uniform across all runs —
  `experiment_header` → `dma_package`(s) + `iteration_ended` → `experiment_footer`.
  The `sweep_experiment_header` packet and the `"experiment started"` /
  `"experiment ended"` status strings were removed.
- **Breaking:** `SweepExperiment` parses its callbacks and variables in the
  constructor (`SweepExperiment(server, sweep_callbacks, variables)`); `run()`
  takes no arguments and no longer emits anything on the network.
- **Breaking:** the `const` variable mode was removed — use
  `{"mode": "list", "values": [x]}` to sweep a single value.
- **Breaking:** `FIREQSystemNode.trigger_manually()` was renamed to
  `trigger_ip_manually()`.
- The overlay base path is resolved relative to `start_server.py` instead of a
  hardcoded `/home/xilinx/`, so the server runs from any install location.

### Fixed

- Sweeps crashed the sender thread: the `experiment_header` carried NumPy arrays,
  which cannot be msgpack-encoded. Variable values are now converted to lists.
- `AttributeError` when closing the client before any connection was accepted
  (`_server_socket` and `_client_socket` are now initialised to `None`).
- Duplicate, inconsistent sweep headers emitted by both `SweepExperiment` and the
  server; the header is now emitted once, by the server.
- Type-hint typo in `SweepExperiment._compute_variable_values`.
- Stale documentation referring to `API.py`, which was renamed to
  `start_server.py`.

### Removed

- `SweepExperiment._put_sweep_info_in_network()` and the unused `copy`, `Queue`
  and `FIREQNetworkPacket` imports.
- Commented-out deprecated command branches in the server's message dispatcher.

## [0.1.0] - 2026-08-29

- Initial public release.

[0.1.1]: https://github.com/vlsi-nanocomputing/FIREQ-Server/compare/version-0.1.0...version-0.1.1
[0.1.0]: https://github.com/vlsi-nanocomputing/FIREQ-Server/releases/tag/version-0.1.0
