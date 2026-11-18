# OrderBOT

Phase 0 provides the Python 3.11+ paper-mode foundation for a NIFTY futures signal series and NIFTY weekly options. The command-line entry point initializes storage and can check SmartAPI authentication.

```powershell
python -m pip install -e ".[test]"
python run.py --check-config
python run.py
python -m pytest
```

`config.yaml` leaves capital and per-trade risk unset. Relative data and log paths resolve beside the YAML file. SQLite timestamps use explicit UTC offsets. The JSON logs use IST.

## SmartAPI login check

Set the following variables in the environment from which you run OrderBOT.

| Variable | Value |
| --- | --- |
| `ANGEL_ONE_API_KEY` | SmartAPI application key |
| `ANGEL_ONE_CLIENT_CODE` | Angel One client code |
| `ANGEL_ONE_MPIN` | SmartAPI login PIN, including any leading zeros |
| `ANGEL_ONE_TOTP_SECRET` | Base32 authenticator setup secret |
| `ANGEL_ONE_LOCAL_IP` | Local IP of the client machine |
| `ANGEL_ONE_PUBLIC_IP` | Public egress IP used with SmartAPI |
| `ANGEL_ONE_MAC_ADDRESS` | Colon-delimited MAC of the client machine |

Run `python run.py --broker-login` to authenticate. The command reports success or a sanitized error and exits. The [SmartAPI documentation](https://smartapi.angelone.in/docs) describes the request headers and authentication flow.
