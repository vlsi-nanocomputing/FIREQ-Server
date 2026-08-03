# Installation

On the RFSoC board, the setup is typically:

1. copy the server project to the board,
2. connect with SSH,
3. switch to root with `sudo -i` if needed,
4. activate the environment with `source /etc/profile.d/pynq_venv.sh`,
5. install the requirements:

```bash
pip install -r requirements.txt
```
6. start the server with `python API.py`.

The startup flow also requires the overlay files to be present on the board and the correct `.bit` and `.hwh` names to be provided when prompted.


## Note

A detailed startup procedure can be found [here](server.md).