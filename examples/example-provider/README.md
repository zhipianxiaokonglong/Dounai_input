# Example provider plugin

This optional package shows the supported way to add a vendor/model to Dounai
Input Gateway without touching core code.

```bash
pip install -e examples/example-provider
```

Add configuration to `dounai.toml`:

```toml
[providers.example-http]
enabled = true
endpoint = "https://asr.example.com/transcribe"
token_env = "EXAMPLE_ASR_TOKEN"
model = "example-asr"
timeout_seconds = 30
```

Set the token and restart the gateway:

```bash
export EXAMPLE_ASR_TOKEN="..."
dounai-input serve
dounai-input providers activate example-http
```

The entry point group is `dounai_input.providers`; the entry point **name** must
match the `[providers.<name>]` table in `dounai.toml`. Uninstall with:

```bash
pip uninstall dounai-provider-example
```

Uninstalling is clean: core has no import of this package and no stored weights.

See `docs/developer-guide.md` for the full contract and testing advice.
