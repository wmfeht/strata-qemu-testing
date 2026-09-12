# Test SSH key

`check-host` generates an ed25519 keypair into `$CACHE/keys/` on first
success (default `$XDG_CACHE_HOME/strata-qemu-testing/keys/`, override
`STRATA_QEMU_CACHE`):

```bash
ssh-keygen -t ed25519 -f "$CACHE/keys/id_ed25519" -N ""
```

Only this README is committed. The private key is a local fixture, not a
production secret. Goldens are per-machine cache and are rebuilt from the
recipe if the key changes.
