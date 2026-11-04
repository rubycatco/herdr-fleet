"""The helper as the tests load it: the same script, with a long limit for each git call that it makes.

No test result may depend on how fast git answers. The helper gives some git calls a short limit (1 s for each
call in a hook, HOOK_GIT_TIMEOUT; 3 s in git_toplevel), and on a loaded machine git can take longer: the hook
then gives other text, or none. While the tests run, each git call of the helper that has a limit gets at least
GIT_TEST_LIMIT seconds. The helper's own limits do not change, and test_hooks_pass_their_git_limit checks that
the hooks still pass HOOK_GIT_TIMEOUT.

test_fleet.py loads the helper with load(). A test that runs the helper in a child process runs this file:
    python3 patient_fleet.py <path of scripts/fleet> <fleet arguments...>
"""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys

GIT_TEST_LIMIT = 300  # seconds


class PatientSubprocess:
    """The subprocess module, with at least GIT_TEST_LIMIT seconds for a git call that has a limit."""

    def __getattr__(self, name):
        return getattr(subprocess, name)

    @staticmethod
    def run(cmd, *args, **kwargs):
        if kwargs.get("timeout") is not None and cmd and os.path.basename(str(cmd[0])) == "git":
            kwargs["timeout"] = max(kwargs["timeout"], GIT_TEST_LIMIT)
        return subprocess.run(cmd, *args, **kwargs)


def load(script):
    """The helper at `script` as a module named fleet_cli, whose git calls get GIT_TEST_LIMIT."""
    loader = importlib.machinery.SourceFileLoader("fleet_cli", str(script))
    spec = importlib.util.spec_from_loader("fleet_cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    module.subprocess = PatientSubprocess()
    return module


if __name__ == "__main__":
    sys.exit(load(sys.argv[1]).main(sys.argv[2:]))
