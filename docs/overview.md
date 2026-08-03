# Overview

FIREQ-Server provides the server-side control layer of the FIREQ platform. It exposes the interface used by FIREQ-Client and coordinates the interaction with the lower-level FIREQ API and hardware-specific components.

From an operational point of view, the server is the component that:

- loads the overlay, accepts client commands,
- executes experiments,
- streams results back to the client. 

Once the server is running on the board, the FIREQ-Client can be launched from a separate machine to connect and control it.
The client-side repository is available at [FIREQ-Client on GitHub](https://github.com/vlsi-nanocomputing/FIREQ-Client).
A detailed description of the Server API is made avaiable [here](api.md)

