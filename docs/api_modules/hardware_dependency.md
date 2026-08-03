# 4. Hardware Dependency Resolution

Experiment configurations contain interdependent variables. For instance, altering the acquisition duration changes the payload size, which in turn alters the maximum number of hardware shots that can fit into the acquisition FIFO. 

The `_DependencyOrchestrator` resolves these relationships using a **Directed Acyclic Graph (DAG)** built with `networkx`.

## Key Execution Features

1. **Topological Order Execution**: Whenever a parameter changes, update callbacks execute in strict topological order to guarantee zero stale states.
2. **Short-Circuit Pruning**: Every update function returns a boolean `did_change`. If a parameter's evaluated state remains identical to its previous value, evaluation of all downstream dependencies is immediately skipped.
3. **Automatic Shot Batching**: If the requested number of total experiment shots exceeds the capacity of the hardware FIFO buffers, the orchestrator calculates a safe maximum chunk size (`hw_shots`) and automatically splits the execution into multiple software repetitions (`sw_shots`).
