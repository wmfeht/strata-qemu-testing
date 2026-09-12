---
name: strataqemu-host-tests
description: >-
  Write and run host-side unit tests for strata-qemu-testing (tests/, stdlib
  unittest, mise run test). Covers the fixtures for faking subprocess.run,
  SSH responders, QMP/qemu-ga sockets, loginctl/hyprctl shell fakes, and the
  rule that tests never start qemu-system-*. Use when adding tests, fixing a
  failing test, or deciding how to test code that talks to a guest.
---

# Host unit tests

Run with `mise run test` (`python -m unittest discover -s tests -t . -v`).
No KVM, no network, no QEMU. Tests must pass on a laptop without
`/dev/kvm`.

## Hard rules

- Never invoke `qemu-system-*`. Patch `subprocess.run` and
  `subprocess.Popen` with `_refuse_qemu_system` (raises `AssertionError`
  naming the argv) whenever the code under test could reach a spawn.
- Never call the real `check_host()`. Pass
  `check_host_fn=lambda: CheckHostResult(ok=True, errors=(), ovmf_code=..., ssh_key=...)`
  or `patch("strataqemu.<module>.check_host", return_value=...)`.
  Note the patch target is the module that imported the name, not `cli`.
- Never touch the user's cache. Set `os.environ[config.CACHE_ENV]` to a
  `tempfile.TemporaryDirectory` (restore it in `finally`) or pass
  `cache_dir=` explicitly.
- Skip, do not fail, when an optional host binary is absent
  (`shutil.which("xorriso")`, `mkfs.vfat`, OVMF files). Use
  `self.skipTest(...)`.

## Fixtures by layer

| Code under test | Fixture | Where to copy from |
| --- | --- | --- |
| `check_host` | `CheckHostEnv` with injected `kvm_accessible`, `firmware_share_roots`, `mem_available_mib`, `keygen` | `tests/test_cli.py` |
| argv builders (`build_qemu_argv`, `create_overlay_argv`, ...) | pure functions; assert on tokens with `_after(argv, flag)` | `tests/test_qemu_argv.py`, `tests/test_run_test.py` |
| `Guest.load`, digests | copy a real recipe into a temp dir and mutate `image.toml` | `tests/test_guest_toml.py` |
| seed writers | temp dirs; skip if `xorriso`/`mkfs.vfat` missing | `tests/test_cloudinit.py` |
| QMP / qemu-ga / VNC clients | a `socket.AF_UNIX` (or TCP loopback) server thread speaking the protocol | `tests/test_ports_shutdown.py` |
| shutdown cascade | `ShutdownHooks` with lambdas recording calls | `tests/test_ports_shutdown.py` |
| `wait_ssh`, `wait_qga`, `wait_iso_autoinstall` | `run=` fake returning scripted `CompletedProcess` objects; `sleep=lambda s: None`; `max_attempts=` | `tests/test_image_build.py` |
| `run_run_test`, `run_vm_run` | `_FakeRun` responder + `_DummyProc` via `popen=` and `settle_s=0` | `tests/test_run_test.py` |
| step functions in `tests_spec.py` | `Machine` with a temp run dir, `_FakeRun`, `commands=[]` | `tests/test_tests_spec.py` |
| `guest-tests/*.sh` | fake `loginctl`, `busctl`, `pgrep`, `gdbus`, `hyprctl`, `grim`, `gnome-screenshot` scripts on a temp `PATH`; fixtures in `SMOKE_FIXTURE` | `tests/test_guest_smokes.py` |

## The SSH responder pattern

`_FakeRun.__call__(argv, **kwargs)` receives the full `ssh`/`scp` argv.
The remote command is `argv[-1]`. Match on substrings and return
`subprocess.CompletedProcess(argv, returncode, stdout, stderr)`. For
`scp` downloads, write the destination file (a PNG header for
screenshots). Record every argv in `self.calls` and assert on it
afterwards.

Simulate failure by returning non-zero for the relevant command, or
`raise subprocess.TimeoutExpired(argv, timeout)` for hangs.

## Machine for step tests

```python
machine = Machine(
    overlay=tmp / "overlay.qcow2",
    run_dir=tmp / "run",
    ssh_port=22022,
    vnc_port=5901,
    identity=tmp / "id_ed25519",
    user="tester",
    process=_DummyProc(),
)
```

`machine.artifacts.root / "ssh.log"` receives a transcript of every
command; assert on it when a test cares about ordering.

## Time and retries

Wait loops take `sleep=` and `max_attempts=`/`timeout=` so tests run in
milliseconds. Never `time.sleep` in a test. When a loop uses
`time.monotonic()` deadlines, pass a small `timeout` and a `sleep` that
does nothing; the attempt cap terminates the loop.

## Asserting on messages

User-facing strings that tests pin are defined once as constants
(`missing_golden_message`, `LOCAL_ARCHIVE_MISSING_PATH`,
`SCREENSHOT_TOOL_MISSING`, `INSTALL_FROM_FAIL_CLOSED`). Import the
constant in the test rather than duplicating the text. If you reword a
message, change the constant and let the tests follow.

## Adding a recipe test

New guests get a `<Id>RecipeTests` class in `tests/test_guest_toml.py`.
Copy the nearest sibling; the digest and `/latest/` tests are mechanical.
The `setup.sh` content tests are `assertIn` on the lines that
`run-test` depends on (autologin, NOPASSWD, linger, screenshot tool,
inventory files).

## Running a subset

```bash
mise exec -- python -m unittest tests.test_run_test -v
mise exec -- python -m unittest tests.test_run_test.SessionOnlyWiringTests.test_name -v
```

Test names read as behaviour statements
(`test_missing_golden_fails_without_building`), one behaviour per test,
`assertEqual(code, 0, err.getvalue())` so a failing exit code shows the
stderr.
