---
name: strataqemu-add-command
description: >-
  Add a CLI subcommand or flag to strata-qemu-testing: argparse wiring in
  strataqemu/cli.py, the run_* body in its own module, the mise.toml task,
  the scripts/ shim, README command reference, and unit tests that never
  spawn QEMU. Use when extending python -m strataqemu, adding a mise task,
  or changing command-line behaviour or exit codes.
---

# Add a CLI command or flag

Entry point is `strataqemu/cli.py`: `build_parser()` defines the
subcommands, `main()` dispatches. Each non-trivial command body lives in
its own module (`image_build.run_image_build`, `run_test.run_run_test`,
`run_test.run_vm_run`, `spike.run_spike_wayland_ubuntu`) and is imported
lazily inside `main()` so `check-host` stays fast.

## Checklist

```
- [ ] Subparser in build_parser() with help text that states what the command does
- [ ] Dispatch branch in main() importing the run_* function lazily
- [ ] run_<command>() in its own module returning an int exit code
- [ ] [tasks.<command>] in mise.toml (raw_args, depends, interactive as needed)
- [ ] scripts/<command> shim (copy an existing one, change the command name)
- [ ] README.md command reference row
- [ ] tests/test_cli.py parser tests; behaviour tests in tests/test_<module>.py
- [ ] mise run test passes
```

## Conventions

**Exit codes.** `0` success; `1` runtime failure (host check, guest,
timeout); `2` usage error (missing guest id, bad flag combination);
`130` on `KeyboardInterrupt`. Usage errors print a one-line reason and a
`usage:` line to stderr.

**Output.** Machine-readable results go to stdout (a golden path, a
screenshot path). Progress, warnings, and errors go to stderr. Prefix
stderr messages with the command name: `run-test: archive not found: ...`.
Use `logging.getLogger("strataqemu")` for diagnostics; `-v` raises it to
DEBUG.

**Injectability.** `run_*` functions take keyword-only hooks with `None`
defaults so tests can substitute them: `cache_dir`, `check_host_fn`,
`run` (for `subprocess.run`), `popen`, `create_overlay_fn`, `settle_s`.
Follow the signature of `run_run_test`.

**Guest argument.** Use `nargs="?"` and fail closed with a
`<command>: guest id is required (e.g. ubuntu-2404)` message; see
`run_test._load_or_usage`. Load recipes with `guest.load_guest`; catch
`GuestError` and exit 2.

**Order of checks.** Validate arguments, then anything that needs only
the cache (golden present?), then `check_host()`. This keeps "you forgot
to build" errors fast on hosts where `check-host` is slow.

**Never build implicitly.** Commands that boot a guest must require an
existing golden and point at `image-build`; use
`tests_spec.missing_golden_message`.

**Argv normalisation.** `main()` strips a lone `--` so mise's
`mise run cmd -- args` works. Top-level `-v` is only accepted before the
subcommand.

## mise.toml

```toml
[tasks.my-command]
description = "One line, imperative."
depends = ["check-host"]     # if it needs KVM/QEMU
interactive = true           # if it spawns QEMU (TTY, Ctrl-C)
raw_args = true
run = "python -m strataqemu my-command"
```

Do not add `image-build` to any `depends`.

## Tests

`tests/test_cli.py` covers the parser: unknown flags rejected, `--` is
dropped, required guest id, exit codes. Behaviour tests live beside the
module under test and follow this pattern:

```python
with (
    redirect_stdout(buf),
    redirect_stderr(err),
    patch("subprocess.Popen", side_effect=_refuse_qemu_system),
    patch("subprocess.run", side_effect=_refuse_qemu_system),
):
    code = main(["my-command", "ubuntu-2404"])
```

Set `STRATA_QEMU_CACHE` to a `tempfile.TemporaryDirectory` for any test
that touches the cache, and pass `check_host_fn=lambda: CheckHostResult(...)`
rather than letting the real `check_host` probe the machine.

## Documentation

Add a row to the README "Command reference" table and, if the command
introduces a new flow, a short section under "Run tests" or a new heading.
Update `docs/architecture.md` "Operator surface" and the relevant
pipeline section.
