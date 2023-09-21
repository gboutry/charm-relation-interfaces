"""Runner script to execute all interface tests."""
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import os
import shutil
import subprocess
from collections import namedtuple
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, Literal, Tuple

from interface_tester.collector import collect_tests

if TYPE_CHECKING:
    from interface_tester.collector import _CharmTestConfig, _RoleTestSpec

    _Role = Literal["provider", "requirer"]

    # mapping from charm name (e.g. 'traefik-k8s') to whether the tests are passing or not
    _ResultsPerCharm = Dict[str, bool]
    # mapping from role ('provider'|'requirer) to results per charm
    _ResultsPerRole = Dict[_Role, _ResultsPerCharm]
    # mapping from version names (e.g. 'v1') to results per role
    _ResultsPerVersion = Dict[str, _ResultsPerRole]
    # mapping from interface names (e.g. 'ingress') to results per version
    _ResultsPerInterface = Dict[str, _ResultsPerVersion]

# it is "python -m venv" on some platforms/python versions
MKVENV_CMD = os.getenv("MKVENV_CMD", "python -m virtualenv")

FIXTURE_PATH = "tests/interface/conftest.py"
FIXTURE_IDENTIFIER = "interface_tester"
logging.getLogger().setLevel(logging.INFO)

FixtureSpec = namedtuple("FixtureSpec", "path id")


class SetupError(Exception):
    pass


class InterfaceTestError(Exception):
    pass


def _clone_charm_repo(charm_config: "_CharmTestConfig", charm_path: Path):
    """Clones a charm repository to a local path."""
    logging.info(
        f"Cloning: {charm_config.name} from ({charm_config.url}@{charm_config.branch or 'main'})"
    )
    branch_option = ""
    if charm_config.branch:
        branch_option = f"--branch {charm_config.branch}"
        logging.warning(
            f"custom branch provided for {charm_config.name}; "
            f"this should only be done in staging"
        )
    retcode = subprocess.call(
        f"git clone --quiet --depth 1 {branch_option} {charm_config.url} {charm_path}",
        shell=True,
        stdout=subprocess.DEVNULL,
    )
    if retcode > 0:
        raise SetupError(
            f"Failed to clone repo for {charm_config.name}; "
            "check the charms.yaml config."
        )


def _prepare_repo(
    charm_config: "_CharmTestConfig",
    interface: str,
    version: int,
    root: Path = Path("/tmp/charm-relation-interfaces-tests/"),
) -> Tuple[Path, Path]:
    """Clone the charm repository and create the venv if it hasn't been done already."""
    logging.info(f"Preparing testing environment for: {charm_config.name}")
    charm_path = root / Path(charm_config.name)
    if not charm_path.exists():
        _clone_charm_repo(charm_config, charm_path)
        _setup_venv(charm_path)
    try:
        fixture_spec = _get_fixture(charm_config, charm_path)
    except FileNotFoundError as e:
        raise SetupError(f"unable to get fixture spec from {charm_path}") from e
    if not fixture_spec.path.is_file():
        # NOTE: In the future we could probably run the tests without a fixture, assuming
        # that the charm needs no patching at all to work with scenario
        raise SetupError(f"fixture missing for charm {charm_config.name}")
    test_path = _generate_test(
        interface, fixture_spec.path.parent, fixture_spec.id, version
    )
    return charm_path, test_path


def _clean(root: Path = Path("/tmp/charm-relation-interfaces-tests/")):
    """Clean the directory used to store repos for the tests."""
    if root.is_dir():
        shutil.rmtree(root)


_TEST_CONTENT = """
# file generated by run_matrix.py
from interface_tester import InterfaceTester
def test_{interface}_interface({fixture_id}: InterfaceTester):
    {fixture_id}.configure(
        interface_name="{interface}",
        interface_version={version},
    )
    {fixture_id}.run()
"""


def _generate_test(
    interface: str, test_path: Path, fixture_id: str, version: int
) -> Path:
    """Generate a pytest file for a given charm and interface."""
    logging.info(f"Generating test file for {interface} at {test_path}")
    test_content = _TEST_CONTENT.format(
        interface=interface, fixture_id=fixture_id, version=version
    )
    test_filename = f"interface-test-{interface}.py"
    with open(test_path / test_filename, "w") as file:
        file.write(test_content)
    return test_path / test_filename


def _get_fixture(charm_config: "_CharmTestConfig", charm_path: Path) -> FixtureSpec:
    """Get the tester fixture from a charm."""
    fixture_path = charm_path / FIXTURE_PATH
    fixture_id = FIXTURE_IDENTIFIER
    if charm_config.test_setup:
        if charm_config.test_setup["location"]:
            fixture_path = charm_path / Path(charm_config.test_setup["location"])
        if charm_config.test_setup["identifier"]:
            fixture_id = charm_config.test_setup["identifier"]
    return FixtureSpec(fixture_path, fixture_id)


def _setup_venv(charm_path: Path) -> None:
    """Create the venv for a charm and return the path to its python."""
    logging.info(f"Preparing venv for {charm_path}")

    original_wd = os.getcwd()
    os.chdir(charm_path)
    # Create the venv and install the requirements
    try:
        subprocess.check_call(
            f"{MKVENV_CMD} ./.interface-venv", shell=True, stdout=subprocess.DEVNULL
        )
        logging.info(f"Installing dependencies in venv for {charm_path}")

        subprocess.check_call(
            ".interface-venv/bin/python -m pip install setuptools pytest pytest-interface-tester",
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.check_call(
            ".interface-venv/bin/python -m pip install -r requirements.txt",
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as e:
        raise SetupError("venv setup failed") from e
    os.chdir(original_wd)


def _run_test_with_pytest(root: Path, test_path: Path):
    """Run a test file with pytest."""
    logging.info(f"Running tests for {root}")
    original_wd = os.getcwd()
    os.chdir(root)
    try:
        subprocess.check_call(
            f"PYTHONPATH=src:lib .interface-venv/bin/python -m pytest {test_path}",
            shell=True,
        )
    except subprocess.CalledProcessError as e:
        raise InterfaceTestError from e
    os.chdir(original_wd)


def _test_charm(
    charm_config: "_CharmTestConfig", interface: str, version: int, role: str
) -> bool:
    """Run interface tests for a charm."""
    logging.info(f"Running tests for charm: {charm_config.name}")
    try:
        charm_path, test_path = _prepare_repo(charm_config, interface, version)
    except SetupError:
        logging.warning(
            f"test setup failed for {charm_config.name} {interface} {role}",
            exc_info=True,
        )
        return False

    try:
        _run_test_with_pytest(charm_path, test_path)
    except InterfaceTestError:
        logging.warning(
            f"interface tests for {charm_config.name} {interface} {role} failed",
            exc_info=True,
        )
        return False
    return True


def _test_charms(
    charm_configs: Iterable["_CharmTestConfig"], interface: str, version: int, role: str
) -> "_ResultsPerCharm":
    """Test all charms against this interface and role."""
    logging.info(f"Running tests for {interface}")
    out = {}
    for charm_config in charm_configs:
        success = _test_charm(charm_config, interface, version, role)
        out[charm_config.name] = success
        logging.info(f"Result: {'PASSED' if success else 'FAILED'}")
    return out


def _test_roles(
    tests_per_role: Dict["_Role", "_RoleTestSpec"], interface: str, version: int
) -> "_ResultsPerRole":
    """Run the tests for each role of this interface."""
    results_per_role: _ResultsPerRole = {}
    role: "_Role"
    for role in ["provider", "requirer"]:
        logging.info(f"Running tests for role: {role}")
        interface_tests = tests_per_role[role]["tests"]
        charm_configs = tests_per_role[role]["charms"]

        if not interface_tests:
            logging.info(f"No tests specified for {interface}/{role}; skipping...")
            results_per_role[role] = {}
        elif not charm_configs:
            logging.info(f"No charms registered for {interface}/{role}; skipping...")
            results_per_role[role] = {}
        else:
            logging.info(
                f"Running {len(interface_tests)} {interface} interface tests on: "
                f"{[charm.name for charm in charm_configs]}..."
            )
            results_per_role[role] = _test_charms(
                charm_configs, interface, version, role
            )
    return results_per_role


def _test_interface_version(tests_per_version, interface: str) -> "_ResultsPerVersion":
    """Run the tests for each version of this interface."""
    logging.info(f"Running tests for interface: {interface}")
    results_per_version: _ResultsPerVersion = {}

    for version, tests_per_role in tests_per_version.items():
        logging.info(f"Running tests for version: {version}")

        version_int = int(version[1:])
        results_per_version[version] = _test_roles(
            tests_per_role, interface, version_int
        )

    return results_per_version


def run_interface_tests(path: Path, include: str = "*") -> "_ResultsPerInterface":
    """Run the tests for the specified interfaces, defaulting to all."""
    _clean()
    test_results = {}
    collected = collect_tests(path=path, include=include)
    for interface, version_to_roles in collected.items():
        results_per_version = _test_interface_version(version_to_roles, interface)
        test_results[interface] = results_per_version

    if not collected:
        logging.warning("No tests collected.")

    return test_results


def pprint_interface_test_results(test_results: dict):
    """Pretty print the results of interface tests."""
    print("+++ Results +++")
    print(json.dumps(test_results, indent=2))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--include",
        default="*",
        help="Glob to filter what interfaces to include in the test matrix.",
    )
    args = parser.parse_args()

    result = run_interface_tests(Path("."), args.include)
    pprint_interface_test_results(result)
